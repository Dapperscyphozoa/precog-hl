"""Entry LLM Gate.

Pre-trade semantic check. Called from precog.py before place().

Input:
    coin, side, signal_ctx {
        engine, conf_score, conf_breakdown, risk_mult,
        price, regime_state, session, funding_rate_bps,
        candles (recent), ...
    }

Output:
    {
        'decision': 'ALLOW' | 'SIZE_DOWN' | 'BLOCK',
        'size_mult': float (0.0 to 1.0),
        'reason': str,
        'kb_matches': int,
        'vetoed_components': list[str],
    }

Safety:
    - 5s cache per (coin, side) to absorb retest storms
    - 6s Claude timeout → fail-open (ALLOW, neutral mult)
    - If ANTHROPIC_API_KEY missing → ALLOW with no Claude call
    - If POSTMORTEM_ENTRY_GATE=0 → ALLOW always (toggle off)
    - Never raises
"""
import os
import time
import json
import threading

from . import db, bounds, kb, params_api

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

_CLIENT = None
_CACHE = {}               # (coin, side) -> (verdict, expires_at)
_CACHE_TTL = 5.0
_LOCK = threading.Lock()

ENABLED = os.environ.get('POSTMORTEM_ENTRY_GATE', '1') == '1'
MODEL = os.environ.get('POSTMORTEM_GATE_MODEL', 'claude-sonnet-4-5')
TIMEOUT_SEC = float(os.environ.get('POSTMORTEM_GATE_TIMEOUT', '6.0'))
SIZE_DOWN_MIN = float(os.environ.get('POSTMORTEM_GATE_SIZE_DOWN_MIN', '0.3'))
SIZE_DOWN_MAX = float(os.environ.get('POSTMORTEM_GATE_SIZE_DOWN_MAX', '0.7'))


def _client():
    global _CLIENT
    if _CLIENT is None:
        if Anthropic is None:
            raise RuntimeError('anthropic SDK not installed')
        _CLIENT = Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    return _CLIENT


_GATE_SYSTEM = '''You are the pre-trade gate for a live HL crypto perps trading system.

You receive:
- The proposed trade (coin, side, engine, confidence)
- Current tuned parameters for this coin
- Active component vetos for this coin
- Top relevant knowledge-base entries from post-mortem analysis of past trades
- Live market context (regime, funding, session)

You decide ONE of:
- ALLOW       — trade is aligned with learned patterns, fire at full size
- SIZE_DOWN   — trade has warning signs but not disqualifying; reduce size
- BLOCK       — trade matches a failing pattern or contradicts hard-learned rules

Rules:
- BLOCK only when KB evidence is strong (weight >= 2.5 from multiple reinforcements)
  OR an active veto applies to the primary signal component.
- SIZE_DOWN when confluence is marginal: moderate KB warnings, conflicting regime,
  wrong session, or component param tuned aggressively away from default.
- ALLOW by default when no contrary signal exists. Do not block on vibes.
- When SIZE_DOWN, choose a specific size_mult between 0.3 and 0.7.
  0.3 = heavy warning, minimum size. 0.7 = mild warning.
- ALLOW uses size_mult 1.0. BLOCK uses size_mult 0.0.
- Reason must be one sentence, ≤ 160 chars. Cite specific evidence.

Output JSON only. No markdown. Exact shape:
{
  "decision": "ALLOW" | "SIZE_DOWN" | "BLOCK",
  "size_mult": 0.0,
  "reason": "string",
  "citations": ["pattern_key_or_veto_or_param_change", ...]
}'''


def _build_prompt(coin, side, signal_ctx, tuned_params, vetos, kb_block):
    ctx_summary = {
        'coin': coin,
        'side': side,
        'engine': signal_ctx.get('engine'),
        'conf_score': signal_ctx.get('conf_score'),
        'conf_breakdown': signal_ctx.get('conf_breakdown'),
        'price': signal_ctx.get('price'),
        'session': signal_ctx.get('session'),
        'funding_rate_bps': signal_ctx.get('funding_rate_bps'),
        'regime_state': signal_ctx.get('regime_state'),
        'btc_dir': signal_ctx.get('btc_dir'),
        'equity': signal_ctx.get('equity'),
        'open_positions': signal_ctx.get('open_positions'),
    }

    tp_block = 'none'
    if tuned_params:
        tp_block = '\n'.join(
            f'  - {p["component"]}.{p["param_name"]} = {p["param_value"]} '
            f'(default {p["default_value"]}, tuned {int((time.time() - (p.get("last_tuned_at") or time.time()))/3600)}h ago)'
            for p in tuned_params[:15]
        )

    veto_block = 'none'
    if vetos:
        veto_block = '\n'.join(
            f'  - {v["component"]}: {v.get("reason","")[:120]}'
            for v in vetos
        )

    return f'''PROPOSED TRADE:
{json.dumps(ctx_summary, indent=2, default=str)}

TUNED PARAMETERS FOR THIS COIN:
{tp_block}

ACTIVE COMPONENT VETOS:
{veto_block}

RELEVANT KB ENTRIES (post-mortem learnings for this coin/side):
{kb_block}

Decide now. JSON only.'''


def _fail_open(reason='fail-open'):
    return {
        'decision': 'ALLOW', 'size_mult': 1.0, 'reason': reason,
        'kb_matches': 0, 'vetoed_components': [], 'citations': [],
    }


def _call_claude_with_timeout(system, user, timeout_sec):
    """Run Claude call in a thread, enforce hard timeout. Returns text or None."""
    result = {'text': None, 'err': None}
    def _go():
        try:
            client = _client()
            resp = client.messages.create(
                model=MODEL, max_tokens=600, system=system,
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
    if t.is_alive():
        return None  # hard timeout
    return result['text']


def _parse(text):
    try:
        t = (text or '').replace('```json', '').replace('```', '').strip()
        i = t.find('{'); j = t.rfind('}')
        if i < 0 or j <= i: return None
        obj = json.loads(t[i:j+1])
        dec = obj.get('decision', 'ALLOW')
        if dec not in ('ALLOW', 'SIZE_DOWN', 'BLOCK'): dec = 'ALLOW'
        sm = float(obj.get('size_mult', 1.0))
        if dec == 'ALLOW': sm = 1.0
        elif dec == 'BLOCK': sm = 0.0
        else:
            # SIZE_DOWN: clamp to configured range
            sm = max(SIZE_DOWN_MIN, min(SIZE_DOWN_MAX, sm))
        return {
            'decision': dec,
            'size_mult': sm,
            'reason': str(obj.get('reason', ''))[:200],
            'citations': obj.get('citations', []) or [],
        }
    except Exception:
        return None


def _resolve_regime_state(signal_ctx):
    """Build pattern_key suffixes from context for KB lookup."""
    extras = []
    coin = signal_ctx.get('coin')
    side = signal_ctx.get('side')
    if not coin or not side: return extras
    prefix = f'{coin}:{side}'

    regime = signal_ctx.get('regime_state')
    if regime: extras.append(f'{prefix}:regime={regime}')

    session = signal_ctx.get('session')
    if session: extras.append(f'{prefix}:session={session}')

    engine = signal_ctx.get('engine')
    if engine: extras.append(f'{prefix}:engine={engine}')

    funding = signal_ctx.get('funding_rate_bps')
    if funding is not None:
        fb = 'neg' if funding < -1 else ('pos' if funding > 1 else 'flat')
        extras.append(f'{prefix}:funding={fb}')

    return extras


def evaluate_entry(coin, side, signal_ctx):
    """Main entry point. Returns verdict dict. Never raises."""
    if not ENABLED:
        return _fail_open('gate disabled')
    if not coin or side not in ('BUY', 'SELL'):
        return _fail_open('bad args')

    # Cache check
    ckey = (coin, side)
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(ckey)
        if cached and cached[1] > now:
            return cached[0]

    try:
        signal_ctx = dict(signal_ctx or {})
        signal_ctx['coin'] = coin
        signal_ctx['side'] = side

        tuned = params_api.params_summary(coin=coin) or []
        vetos = [v for v in db.list_vetos(active_only=True) if v['coin'] == coin]

        # Primary component veto = hard BLOCK, no need to call Claude
        primary_components = _primary_components_for_engine(signal_ctx.get('engine'))
        blocking_vetos = [v for v in vetos if v['component'] in primary_components]
        if blocking_vetos:
            verdict = {
                'decision': 'BLOCK',
                'size_mult': 0.0,
                'reason': f'veto active: {blocking_vetos[0]["component"]} ({blocking_vetos[0].get("reason","")[:80]})',
                'kb_matches': 0,
                'vetoed_components': [v['component'] for v in blocking_vetos],
                'citations': [f'veto:{v["component"]}' for v in blocking_vetos],
            }
            with _LOCK: _CACHE[ckey] = (verdict, now + _CACHE_TTL)
            return verdict

        # If no API key, pass through with SIZE_DOWN if any non-primary veto exists
        if not os.environ.get('ANTHROPIC_API_KEY'):
            if vetos:
                verdict = _fail_open('no api key, non-primary vetos present → size down')
                verdict['decision'] = 'SIZE_DOWN'
                verdict['size_mult'] = 0.7
                verdict['vetoed_components'] = [v['component'] for v in vetos]
            else:
                verdict = _fail_open('no api key')
            with _LOCK: _CACHE[ckey] = (verdict, now + _CACHE_TTL)
            return verdict

        # Build KB context
        extras = _resolve_regime_state(signal_ctx)
        kb_entries = kb.read_relevant(coin, side, extra_pattern_keys=extras, max_entries=6)
        kb_block = kb.format_for_prompt(kb_entries, max_chars=1200)

        prompt = _build_prompt(coin, side, signal_ctx, tuned, vetos, kb_block)
        text = _call_claude_with_timeout(_GATE_SYSTEM, prompt, TIMEOUT_SEC)
        if not text:
            verdict = _fail_open('claude timeout')
        else:
            parsed = _parse(text)
            verdict = parsed or _fail_open('parse failed')
            verdict['kb_matches'] = len(kb_entries)
            verdict['vetoed_components'] = [v['component'] for v in vetos]

        with _LOCK: _CACHE[ckey] = (verdict, now + _CACHE_TTL)
        return verdict
    except Exception as e:
        return _fail_open(f'error: {type(e).__name__}')


def _primary_components_for_engine(engine):
    """Which component vetos should hard-block based on signal engine."""
    if not engine: return set()
    e = engine.upper()
    mapping = {
        'PIVOT':     {'rsi', 'pivot', 'structure'},
        'BB_REJ':    {'rsi', 'bollinger'},
        'INSIDE_BAR': {'structure'},
        'PULLBACK':  {'rsi', 'structure', 'ema'},
        'WALL_BNC':  {'wall', 'ob'},
        'LIQ_CSCD':  {'liq'},
    }
    return mapping.get(e, set())


def clear_cache(coin=None):
    with _LOCK:
        if coin is None:
            _CACHE.clear()
        else:
            for k in list(_CACHE.keys()):
                if k[0] == coin: del _CACHE[k]
