"""Trade Finder — independent trade origination via LLM reasoning.

Unlike the mechanical signal engines (pivot, BB, IB, etc.), this scans
coins with full context — news, macro, calendar, funding, CVD, walls,
KB learnings — and asks Claude: "is there a trade here the mechanical
engines are missing?"

Two modes:

  SCAN  (read-only): produces candidate trade proposals, returns them.
        Called via /postmortem/find endpoint. No auto-fire.

  AUTO  (daemon):    runs every N minutes, proposes trades, pushes them
        into the existing webhook pipeline so they flow through the
        entry_gate + risk gate just like any other signal.
        Enabled only when POSTMORTEM_FINDER_AUTO=1.

Every proposal is routed through the full safety stack:
  trade_finder → webhook → apply_ticker_gate → entry_gate → process → place()

The finder never bypasses any gate. It only originates signals the
mechanical engines didn't produce.
"""
import os
import time
import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

from . import db, kb, params_api, context as ctx_mod

_CLIENT = None
MODEL = os.environ.get('POSTMORTEM_FINDER_MODEL', 'claude-sonnet-4-5')
AUTO_ENABLED = os.environ.get('POSTMORTEM_FINDER_AUTO', '0') == '1'
AUTO_INTERVAL_SEC = int(os.environ.get('POSTMORTEM_FINDER_INTERVAL', '300'))
MAX_PROPOSALS_PER_SCAN = int(os.environ.get('POSTMORTEM_FINDER_MAX_PROPOSALS', '3'))
MAX_WORKERS = int(os.environ.get('POSTMORTEM_FINDER_WORKERS', '4'))
TIMEOUT_SEC = int(os.environ.get('POSTMORTEM_FINDER_TIMEOUT', '30'))
MIN_CONFIDENCE = float(os.environ.get('POSTMORTEM_FINDER_MIN_CONF', '0.65'))

# Proposal webhook: the finder POSTs to localhost:<port>/webhook with a
# SCANNER payload. The existing webhook handler validates with WEBHOOK_SECRET
# and routes through apply_ticker_gate → process().
WEBHOOK_URL = os.environ.get('POSTMORTEM_FINDER_WEBHOOK',
                             'http://127.0.0.1:10000/webhook')


def _client():
    global _CLIENT
    if _CLIENT is None:
        if Anthropic is None:
            raise RuntimeError('anthropic SDK not installed')
        _CLIENT = Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    return _CLIENT


def _coin_universe():
    """Ask percoin_configs for the active coin list. Falls back to a curated list."""
    try:
        import percoin_configs
        if hasattr(percoin_configs, 'ELITE_COINS'):
            coins = list(percoin_configs.ELITE_COINS)
            if coins: return coins
        if hasattr(percoin_configs, 'all_elite_coins'):
            return list(percoin_configs.all_elite_coins())
    except Exception: pass
    # Fallback
    return ['BTC','ETH','SOL','XRP','DOGE','ADA','AVAX','LINK','BNB','HYPE']


def _coin_snapshot(coin):
    """Gather everything known about a coin: candles, walls, CVD, funding, OI."""
    snap = {'coin': coin}
    try:
        import bybit_ws
        if hasattr(bybit_ws, 'get_candles'):
            candles = bybit_ws.get_candles(coin, '5m', 30) or []
            if candles:
                cl = [c[4] for c in candles]
                h = [c[2] for c in candles]
                l = [c[3] for c in candles]
                snap['price'] = cl[-1]
                snap['last_30_close_change_pct'] = round((cl[-1] - cl[0]) / cl[0] * 100, 2) if cl[0] else None
                snap['hl_pct'] = round((max(h) - min(l)) / cl[-1] * 100, 2) if cl[-1] else None
    except Exception: pass
    try:
        import cvd_ws
        if hasattr(cvd_ws, 'get_cvd'): snap['cvd'] = cvd_ws.get_cvd(coin)
    except Exception: pass
    try:
        import funding_filter
        if hasattr(funding_filter, 'get_funding'): snap['funding_bps'] = funding_filter.get_funding(coin)
    except Exception: pass
    try:
        import oi_tracker
        if hasattr(oi_tracker, 'get_oi'): snap['oi'] = oi_tracker.get_oi(coin)
    except Exception: pass
    try:
        import wall_confluence
        if hasattr(wall_confluence, 'get_walls'): snap['walls'] = wall_confluence.get_walls(coin)
    except Exception: pass
    try:
        import liquidation_ws
        if hasattr(liquidation_ws, 'get_recent'): snap['recent_liqs'] = liquidation_ws.get_recent(coin)
    except Exception: pass
    return snap


_FINDER_SYSTEM = '''You are an independent trade finder for a live crypto perps trading system.

You see the full market context the mechanical signal engines do not unify:
macro snapshot, news, calendar events, per-coin funding/CVD/walls/liqs,
and knowledge-base learnings from prior trades.

Your job: identify trades the mechanical engines are missing OR explicitly
refuse to propose one if nothing clean is present.

Constraints:
- Only propose trades with clear asymmetric setup (news catalyst, macro
  alignment, divergent positioning, clean structural break, squeeze imminent).
- Do NOT propose trades that contradict:
  - Recent reinforced KB entries
  - Hostile near-term calendar events (high impact within 15min same direction)
  - Macro regime (e.g. short into a squeeze + DXY dump)
- Confidence must be >= 0.65 to propose.
- Output 0, 1, or 2 proposals max per coin. Prefer refusing over forcing.

Output JSON only. No markdown. Shape:
{
  "proposals": [
    {
      "coin": "BTC",
      "side": "BUY" | "SELL",
      "confidence": 0.0,
      "thesis": "one sentence",
      "invalidation": "one sentence describing what would invalidate this",
      "catalysts": ["news or event references"],
      "time_horizon_min": 60,
      "urgency": "now" | "within_5min" | "within_30min"
    }
  ]
}

If nothing clean: return {"proposals": []}.'''


def _call_claude(system, user, timeout_sec=TIMEOUT_SEC, max_tokens=1200):
    result = {'text': None, 'err': None}
    def _go():
        try:
            client = _client()
            resp = client.messages.create(
                model=MODEL, max_tokens=max_tokens, system=system,
                messages=[{'role': 'user', 'content': user}],
                timeout=timeout_sec,
            )
            txt = ''
            for b in resp.content:
                if getattr(b, 'type', None) == 'text':
                    txt += b.text
            result['text'] = txt
        except Exception as e:
            result['err'] = e
    t = threading.Thread(target=_go, daemon=True)
    t.start()
    t.join(timeout=timeout_sec + 1.0)
    return result['text']


def _parse(text):
    try:
        t = (text or '').replace('```json', '').replace('```', '').strip()
        i = t.find('{'); j = t.rfind('}')
        if i < 0 or j <= i: return None
        return json.loads(t[i:j+1])
    except Exception:
        return None


def _scan_one_coin(coin, global_ctx_block):
    snap = _coin_snapshot(coin)
    coin_news = ctx_mod.for_coin(coin).get('news_for_coin', [])
    from . import news as news_mod
    news_block = news_mod.format_for_prompt(coin_news, max_chars=800)
    kb_block = kb.format_for_prompt(kb.read_relevant(coin, 'BUY', max_entries=3)
                                    + kb.read_relevant(coin, 'SELL', max_entries=3),
                                    max_chars=700)
    tuned = params_api.params_summary(coin=coin) or []
    tuned_block = ('\n'.join(f'  {p["component"]}.{p["param_name"]}={p["param_value"]}'
                             for p in tuned[:8])) or 'none'
    active_vetos = [v for v in db.list_vetos(active_only=True) if v['coin'] == coin]
    veto_block = ('\n'.join(f'  {v["component"]}: {v.get("reason","")[:100]}'
                            for v in active_vetos)) or 'none'

    prompt = f'''COIN: {coin}

COIN SNAPSHOT:
{json.dumps(snap, indent=2, default=str)}

COIN NEWS (last 60min):
{news_block}

KB ENTRIES for this coin:
{kb_block}

ACTIVE VETOS:
{veto_block}

TUNED PARAMS (recent learned adjustments):
{tuned_block}

{global_ctx_block}

Decide if there is a clean trade here. JSON only.'''

    try:
        text = _call_claude(_FINDER_SYSTEM, prompt, timeout_sec=TIMEOUT_SEC)
        parsed = _parse(text) or {'proposals': []}
        props = parsed.get('proposals') or []
        # Enforce min confidence + sanity
        clean = []
        for p in props:
            if p.get('side') not in ('BUY', 'SELL'): continue
            if float(p.get('confidence', 0)) < MIN_CONFIDENCE: continue
            p['coin'] = p.get('coin', coin)
            p['_scanned_at'] = time.time()
            clean.append(p)
        return clean
    except Exception as e:
        return []


def scan(coins=None, max_proposals=None):
    """Scan coin universe, return proposals. Read-only. Never fires."""
    if not os.environ.get('ANTHROPIC_API_KEY'):
        return {'ok': False, 'err': 'no ANTHROPIC_API_KEY', 'proposals': []}
    coins = coins or _coin_universe()
    max_props = max_proposals or MAX_PROPOSALS_PER_SCAN
    try:
        global_ctx = ctx_mod.global_context()
        global_block = ctx_mod.format_for_prompt(
            {**global_ctx, 'news_for_coin': global_ctx.get('news_latest', [])[:8]},
            max_total_chars=2400)
    except Exception as e:
        global_block = f'(global ctx err: {e})'

    all_props = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_scan_one_coin, c, global_block): c for c in coins}
        for fut in as_completed(futures, timeout=TIMEOUT_SEC * 3):
            try:
                all_props.extend(fut.result(timeout=TIMEOUT_SEC))
            except Exception:
                pass

    # Rank by confidence, keep top N
    all_props.sort(key=lambda p: p.get('confidence', 0), reverse=True)
    final = all_props[:max_props]
    return {'ok': True, 'scanned_at': time.time(), 'coins_scanned': len(coins),
            'proposals': final}


def fire_proposal(proposal):
    """Send a proposal through the webhook so it traverses the full pipeline.

    The webhook handler validates WEBHOOK_SECRET and treats this as an external
    signal. It will:
      → apply_ticker_gate
      → confidence scoring
      → entry_gate.evaluate_entry (may BLOCK / SIZE_DOWN)
      → risk checks (margin, tier, DD)
      → place()
    """
    import urllib.request
    secret = os.environ.get('WEBHOOK_SECRET', '')
    if not secret:
        return {'ok': False, 'err': 'no WEBHOOK_SECRET'}
    coin = proposal.get('coin'); side = proposal.get('side')
    if not coin or side not in ('BUY', 'SELL'):
        return {'ok': False, 'err': 'bad proposal'}
    # Use the FINDER engine tag so downstream can distinguish
    payload = f'FINDER: {coin} {side}'
    try:
        req = urllib.request.Request(
            WEBHOOK_URL, data=payload.encode(), method='POST',
            headers={'Content-Type': 'text/plain',
                     'X-Webhook-Secret': secret,
                     'X-Finder-Source': 'postmortem',
                     'X-Finder-Confidence': str(proposal.get('confidence', 0)),
                     'X-Finder-Thesis': (proposal.get('thesis', '')[:200]).replace('\n', ' ')}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode()[:300]
            return {'ok': True, 'http_status': r.status, 'body': body,
                    'coin': coin, 'side': side}
    except Exception as e:
        return {'ok': False, 'err': f'{type(e).__name__}: {e}', 'coin': coin, 'side': side}


# ─────────────────────────────────────────────────────
# Auto daemon
# ─────────────────────────────────────────────────────
_auto_thread = None
_auto_stop = threading.Event()
_last_run = None
_last_result = None


def _auto_loop():
    global _last_run, _last_result
    while not _auto_stop.is_set():
        try:
            res = scan()
            _last_run = time.time()
            _last_result = res
            for p in (res.get('proposals') or []):
                try:
                    fire_proposal(p)
                except Exception:
                    pass
        except Exception as e:
            print(f'[finder] auto err: {e}', flush=True)
        # Sleep in small chunks so shutdown is snappy
        for _ in range(AUTO_INTERVAL_SEC):
            if _auto_stop.is_set(): return
            time.sleep(1)


def start_auto():
    global _auto_thread
    if not AUTO_ENABLED:
        return False
    if _auto_thread and _auto_thread.is_alive():
        return True
    _auto_stop.clear()
    _auto_thread = threading.Thread(target=_auto_loop, name='postmortem-finder',
                                    daemon=True)
    _auto_thread.start()
    return True


def stop_auto():
    _auto_stop.set()


def status():
    return {
        'enabled_in_env': AUTO_ENABLED,
        'running': bool(_auto_thread and _auto_thread.is_alive()),
        'last_run_ts': _last_run,
        'last_run_age_sec': int(time.time() - _last_run) if _last_run else None,
        'interval_sec': AUTO_INTERVAL_SEC,
        'model': MODEL,
        'min_confidence': MIN_CONFIDENCE,
        'last_proposals': (_last_result or {}).get('proposals', []),
    }
