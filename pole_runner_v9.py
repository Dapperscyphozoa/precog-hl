#!/usr/bin/env python3
"""V9 runner — paired bounce + breakout orders per fresh wall.

Each fresh wall produces TWO orders:
  - Bounce limit AT wall (placed immediately as resting order)
  - Breakout trigger PAST wall (held in internal watchlist; fires market when
    triggered by 5min candle BODY close beyond the trigger price)

When either fills/triggers, cancel the sibling immediately.

State:
  state['pending']    — bounce limits placed on exchange
  state['triggers']   — armed breakout triggers (internal watchlist)
  state['positions']  — open positions OPENED BY V9 ONLY
  state['v9_oids']    — list of order ids placed by V9 (for cloid attribution)

Multi-engine wallet safety:
  - Every order is tagged with cloid prefixed ENGINE_PREFIX (default 'V9').
  - reconcile() does NOT adopt foreign positions. If HL shows a coin
    position V9 didn't open, it is logged as FOREIGN and ignored.
  - Pruning still applies: if V9 had a position and HL no longer shows it
    (TP/SL/manual close hit), V9 prunes its state.
"""
import json, os, sys, time, traceback, urllib.request
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict, deque
from coin_tiers import (DepthBaseline, get_tier, coins_for_tick, ALL_COINS,
                          get_cadence)
from concurrent.futures import ThreadPoolExecutor
from pole_engine_v9 import (PoleEngineV9, SpoofBreakoutEngine, WallTracker,
                              cluster_walls, Wall, BounceSetup, BreakoutTrigger)
import trend_v9
import funding_v9

PRECOG_URL       = os.environ.get('PRECOG_URL', 'https://precog-i8c3.onrender.com')
HL_API           = 'https://api.hyperliquid.xyz/info'
PRIVATE_KEY      = os.environ.get('HL_PRIVATE_KEY', '')
WALLET           = os.environ.get('HL_ADDRESS') or os.environ.get('HYPERLIQUID_ACCOUNT', '')
RISK_PCT         = float(os.environ.get('RISK_PCT', '0.0025'))
LEVERAGE         = int(os.environ.get('LEVERAGE', '5'))
MAX_POSITIONS    = int(os.environ.get('MAX_POSITIONS', '6'))
MAX_PENDING      = int(os.environ.get('MAX_PENDING_LIMITS', '12'))
MAX_TRIGGERS     = int(os.environ.get('MAX_TRIGGERS', '20'))
MAX_NOTIONAL_PCT = float(os.environ.get('MAX_NOTIONAL_PCT', '0.20'))
POLL_INTERVAL_S  = int(os.environ.get('POLL_INTERVAL_S', '30'))
COIN_PACE_MS     = int(os.environ.get('COIN_PACE_MS', '400'))
TRIGGER_EXPIRE_S = int(os.environ.get('TRIGGER_EXPIRE_S', '14400'))
BOUNCE_EXPIRE_S  = int(os.environ.get('BOUNCE_EXPIRE_S', '14400'))
STATE_FILE       = os.environ.get('STATE_FILE', '/var/data/pole_state_v9.json')
ENGINE_PREFIX    = os.environ.get('ENGINE_PREFIX', 'V9')
COINS_OVERRIDE   = os.environ.get('COINS', '').strip()
COINS            = ([c.strip().upper() for c in COINS_OVERRIDE.split(',') if c.strip()]
                    if COINS_OVERRIDE else list(ALL_COINS))
SCAN_WORKERS     = int(os.environ.get('SCAN_WORKERS', '8'))

TREND_FILTER_ON  = os.environ.get('TREND_FILTER_ON', '1') == '1'
FUNDING_KILL_ON  = os.environ.get('FUNDING_KILL_ON', '1') == '1'

state = {
    'balance': 0.0, 'positions': {}, 'pending': {}, 'triggers': {},
    'v9_oids': [],
    'tick_count': 0, 'last_tick_t': 0,
    'fires_bounce': 0, 'fires_breakout_armed': 0, 'fires_breakout_triggered': 0,
    'fires_spoof': 0, 'funding_kills': 0, 'foreign_logged': 0,
    'log': [],
}

# Per-coin recent mid history for first-touch detection (last ~120s)
MID_HIST = defaultdict(lambda: deque(maxlen=4))


def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%SZ')}] {msg}"
    print(line, flush=True)
    state['log'].append(line)
    if len(state['log']) > 500: state['log'] = state['log'][-500:]

def http_get(url, timeout=10):
    try: return json.loads(urllib.request.urlopen(url, timeout=timeout).read())
    except: return None

def http_post(url, body, timeout=10):
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                       headers={'Content-Type':'application/json'})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except: return None

def fetch_orderbook(coin): return http_get(f"{PRECOG_URL}/orderbook/{coin}")
def fetch_account(): return http_post(HL_API, {'type':'clearinghouseState','user':WALLET}) if WALLET else None
def fetch_open_orders(): return (http_post(HL_API, {'type':'openOrders','user':WALLET}) or []) if WALLET else []

def fetch_candles(coin, interval='15m', days=2):
    end = int(time.time()*1000); start = end - days*86400000
    raw = http_post(HL_API, {'type':'candleSnapshot','req':{'coin':coin,'interval':interval,'startTime':start,'endTime':end}})
    if not raw: return []
    bars = [{'t':b['t'],'o':float(b['o']),'h':float(b['h']),'l':float(b['l']),'c':float(b['c']),'v':float(b['v'])} for b in raw]
    bars.sort(key=lambda x: x['t'])
    return bars

def atr(bars, period=14):
    if len(bars) < period+1: return 0.0
    trs = []
    for i in range(len(bars)-period, len(bars)):
        if i == 0: continue
        tr = max(bars[i]['h']-bars[i]['l'], abs(bars[i]['h']-bars[i-1]['c']), abs(bars[i]['l']-bars[i-1]['c']))
        trs.append(tr)
    return sum(trs)/len(trs) if trs else 0.0

def calc_size(balance, risk_pct, entry, sl):
    if balance <= 0: return 0, 0
    risk_amt = balance * risk_pct
    sl_dist = abs(entry - sl) / entry
    if sl_dist <= 0: return 0, 0
    notional = min(risk_amt / sl_dist, balance * LEVERAGE, balance * MAX_NOTIONAL_PCT * LEVERAGE)
    return notional / entry, notional

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                loaded = json.load(f)
                state.update({k: v for k, v in loaded.items() if k in state})
                log(f"Loaded state: pos={len(state['positions'])} pend={len(state['pending'])} trig={len(state['triggers'])} v9_oids={len(state.get('v9_oids',[]))}")
    except Exception as e: log(f"load_state err: {e}")

def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE,'w') as f: json.dump(state, f, default=str)
    except Exception as e: log(f"save_state err: {e}")

EXCHANGE = None
TRACKER = WallTracker(max_history=30)
POLE = PoleEngineV9()
SPOOF = SpoofBreakoutEngine()
ATR_CACHE = {}
DEPTH_BASE = DepthBaseline(window=20, multiplier=8.0)


# ============================================================================
# CLOID — multi-engine attribution
# ============================================================================

def _build_cloid_str(base: str, suffix: str) -> str:
    """32-hex-char cloid suitable for HL Cloid.from_str."""
    raw = (base + suffix).encode().hex()[:32].ljust(32, '0')
    return '0x' + raw

def _make_cloid(trade_id: str, suffix: str):
    try:
        from hyperliquid.utils.types import Cloid
        return Cloid.from_str(_build_cloid_str(trade_id, suffix))
    except Exception:
        return None

def _trade_id(coin: str, side: str) -> str:
    """{ENGINE_PREFIX}{coin}{side}{ms_ts}"""
    return f"{ENGINE_PREFIX}{coin}{side}{int(time.time()*1000)}"


def get_atr(coin):
    c = ATR_CACHE.get(coin)
    if c and time.time() - c[0] < 600: return c[1]
    bars = fetch_candles(coin, '15m', 2)
    a = atr(bars, 14)
    ATR_CACHE[coin] = (time.time(), a)
    return a

def fetch_1h_for_trend(coin):
    """30 days of 1h bars for pivot trend detection."""
    return fetch_candles(coin, '1h', 30)

def get_5m_close(coin):
    bars = fetch_candles(coin, '5m', 1)
    return bars[-1] if bars else None

def init_sdk():
    if not PRIVATE_KEY or not WALLET:
        raise SystemExit("FATAL: HL_PRIVATE_KEY and HL_ADDRESS env vars required")
    try:
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        from eth_account import Account
        wallet = Account.from_key(PRIVATE_KEY)
        ex = Exchange(wallet, constants.MAINNET_API_URL, account_address=WALLET)
        log(f"SDK initialized, wallet {wallet.address[:10]}... prefix={ENGINE_PREFIX}")
        return ex
    except Exception as e:
        log(f"FATAL SDK init failed: {e}")
        raise SystemExit(f"Cannot start without SDK: {e}")


def _record_oid(oid):
    if oid is None: return
    state['v9_oids'].append(oid)
    if len(state['v9_oids']) > 5000: state['v9_oids'] = state['v9_oids'][-5000:]


def place_limit(coin, is_buy, size, price, reduce_only=False, label='', cloid=None):
    if EXCHANGE is None:
        log(f"  ERR no SDK, skipping {coin} {label}"); return None
    try:
        if cloid is not None:
            res = EXCHANGE.order(coin, is_buy, size, price, {'limit':{'tif':'Gtc'}},
                                  reduce_only=reduce_only, cloid=cloid)
        else:
            res = EXCHANGE.order(coin, is_buy, size, price, {'limit':{'tif':'Gtc'}},
                                  reduce_only=reduce_only)
        try:
            oid = res.get('response',{}).get('data',{}).get('statuses',[{}])[0].get('resting',{}).get('oid')
            _record_oid(oid)
        except Exception: pass
        return res
    except Exception as e:
        log(f"  limit err {coin}: {e}"); return None

def place_market(coin, is_buy, size, slippage=0.005, label='', cloid=None):
    if EXCHANGE is None:
        log(f"  ERR no SDK, skipping {coin} {label}"); return None
    try:
        if cloid is not None:
            res = EXCHANGE.market_open(coin, is_buy, size, slippage=slippage, cloid=cloid)
        else:
            res = EXCHANGE.market_open(coin, is_buy, size, slippage=slippage)
        try:
            oid = res.get('response',{}).get('data',{}).get('statuses',[{}])[0].get('filled',{}).get('oid')
            _record_oid(oid)
        except Exception: pass
        return res
    except Exception as e:
        log(f"  market err {coin}: {e}"); return None

def cancel_order(coin, oid):
    if EXCHANGE is None: return False
    try: EXCHANGE.cancel(coin, oid); return True
    except Exception as e: log(f"  cancel err: {e}"); return False

def market_close(coin, is_buy_to_close, size, label='CLOSE'):
    """Reduce-only market exit."""
    if EXCHANGE is None: return None
    try:
        return EXCHANGE.market_open(coin, is_buy_to_close, size, slippage=0.01,
                                     reduce_only=True)
    except Exception as e:
        log(f"  close err {coin}: {e}"); return None


def place_bounce(s: BounceSetup, bias: str = 'neutral', wall_usd: float = 0.0,
                  threshold_usd: float = 0.0) -> Optional[str]:
    """Place bounce limit. Returns pkey if placed, None if skipped.

    Tier-specific filters (in addition to base checks):
      LOW  : wall must be >= 2x threshold AND counter-trend setups SKIPPED entirely
      MED  : counter-trend gets 0.5x size (size mult)
      HIGH : counter-trend gets 0.5x size (size mult)
    """
    for p in state['pending'].values():
        if p['coin'] == s.coin and p['side'] == s.side: return None
    if s.coin in state['positions']: return None
    if len(state['pending']) >= MAX_PENDING:
        log(f"  skip bounce {s.coin} {s.side}: max pending"); return None

    tier = getattr(s, 'tier', 'MED')

    # LOW-tier extra filter: wall must be 2x threshold
    if tier == 'LOW' and threshold_usd > 0 and wall_usd < threshold_usd * POLE.low_rr_size_multiplier:
        log(f"  skip bounce {s.coin} {s.side} [LOW] wall=${wall_usd/1000:.0f}k < 2x threshold ${threshold_usd*2/1000:.0f}k")
        return None

    # Trend handling: LOW counter-trend = SKIP. MED/HIGH counter-trend = 0.5x size.
    is_counter_trend = TREND_FILTER_ON and bias != 'neutral' and (
        (s.side == 'BUY' and bias == 'down') or (s.side == 'SELL' and bias == 'up'))
    if tier == 'LOW' and is_counter_trend:
        log(f"  skip bounce {s.coin} {s.side} [LOW] counter-trend (bias={bias}) — skipped entirely")
        return None
    size_mult = 0.5 if (is_counter_trend and tier in ('MED', 'HIGH')) else 1.0

    size, notional = calc_size(state['balance'], RISK_PCT, s.entry_price, s.sl_price)
    if size <= 0: return None
    size *= size_mult
    notional *= size_mult
    if size <= 0: return None
    log(f"PLACE-BOUNCE [{tier}] {s.coin} {s.side} entry={s.entry_price:.6f} sl={s.sl_price:.6f} tp={s.tp_price:.6f} rr={s.rr:.2f} bias={bias}×{size_mult:.1f} sz={size:.6f} ${notional:.2f}")
    log(f"  notes: {s.notes}")
    is_buy = (s.side == 'BUY')
    tid = _trade_id(s.coin, s.side)
    or_res = place_limit(s.coin, is_buy, size, s.entry_price, reduce_only=False,
                          label='ENTRY', cloid=_make_cloid(tid, 'E'))
    if not or_res: return None
    entry_oid = or_res.get('response',{}).get('data',{}).get('statuses',[{}])[0].get('resting',{}).get('oid')
    place_limit(s.coin, not is_buy, size, s.sl_price, reduce_only=True,
                 label='SL', cloid=_make_cloid(tid, 'S'))
    place_limit(s.coin, not is_buy, size, s.tp_price, reduce_only=True,
                 label='TP', cloid=_make_cloid(tid, 'T'))
    pkey = f"{s.coin}|BOUNCE|{s.wall_id}"
    state['pending'][pkey] = {
        'coin': s.coin, 'side': s.side, 'kind': 'BOUNCE', 'wall_id': s.wall_id,
        'entry_price': s.entry_price, 'sl': s.sl_price, 'tp': s.tp_price,
        'rr': s.rr, 'tier': tier, 'size': size, 'entry_oid': entry_oid,
        'trade_id': tid, 'placed_t': int(time.time()*1000),
        'sibling_breakout_id': s.sibling_breakout_id,
    }
    state['fires_bounce'] += 1
    return pkey


def arm_breakout(t: BreakoutTrigger, bias: str = 'neutral', wall_usd: float = 0.0,
                  threshold_usd: float = 0.0):
    tier = getattr(t, 'tier', 'MED')

    # LOW-tier wall-size filter
    if tier == 'LOW' and threshold_usd > 0 and wall_usd < threshold_usd * POLE.low_rr_size_multiplier:
        log(f"  skip arm-breakout {t.coin} {t.side} [LOW] wall=${wall_usd/1000:.0f}k < 2x threshold ${threshold_usd*2/1000:.0f}k")
        return

    # LOW counter-trend = SKIP
    is_counter_trend = TREND_FILTER_ON and bias != 'neutral' and (
        (t.side == 'BUY' and bias == 'down') or (t.side == 'SELL' and bias == 'up'))
    if tier == 'LOW' and is_counter_trend:
        log(f"  skip arm-breakout {t.coin} {t.side} [LOW] counter-trend (bias={bias})")
        return

    # Queue full -> evict lowest-RR if this beats it
    if len(state['triggers']) >= MAX_TRIGGERS:
        worst_key = None; worst_rr = float('inf')
        for tk, tv in state['triggers'].items():
            r = tv.get('rr', 0)
            if r < worst_rr:
                worst_rr = r; worst_key = tk
        if t.rr > worst_rr and worst_key:
            log(f"  EVICT-TRIGGER {worst_key} (rr={worst_rr:.2f}) for {t.coin} {t.side} (rr={t.rr:.2f})")
            del state['triggers'][worst_key]
        else:
            log(f"  skip arm-breakout {t.coin}: queue full, rr {t.rr:.2f} <= worst {worst_rr:.2f}")
            return
    tkey = f"{t.coin}|BREAKOUT|{t.wall_id}"
    if tkey in state['triggers']: return
    log(f"ARM-BREAKOUT [{tier}] {t.coin} {t.side} trigger@{t.trigger_price:.6f} sl={t.sl_price:.6f} tp={t.tp_price:.6f} rr={t.rr:.2f} bias={bias}")
    log(f"  notes: {t.notes}")
    state['triggers'][tkey] = {
        'coin': t.coin, 'side': t.side, 'wall_id': t.wall_id,
        'trigger_price': t.trigger_price, 'sl': t.sl_price, 'tp': t.tp_price,
        'rr': t.rr, 'tier': tier, 'armed_t': int(time.time()*1000),
        'sibling_bounce_id': t.sibling_bounce_id,
        'bias': bias,
    }
    state['fires_breakout_armed'] += 1


def check_triggers(coin: str, last_5m: Optional[dict], atr_v: float):
    """ATR-relative body-close check.

    BUY  fires when c >= trigger AND body_bot >= trigger - 0.05 × ATR.
    SELL fires when c <= trigger AND body_top <= trigger + 0.05 × ATR.

    ATR-relative tolerance gives the same noise budget regardless of coin price.
    """
    if not last_5m: return
    o = last_5m['o']; c = last_5m['c']
    body_top = max(o, c)
    body_bot = min(o, c)
    eps = 0.05 * atr_v if atr_v and atr_v > 0 else 0
    fired = []
    for tkey, t in list(state['triggers'].items()):
        if t['coin'] != coin: continue
        trigger_px = t['trigger_price']
        if t['side'] == 'BUY':
            if c >= trigger_px and body_bot >= trigger_px - eps:
                fired.append((tkey, t, last_5m))
        elif t['side'] == 'SELL':
            if c <= trigger_px and body_top <= trigger_px + eps:
                fired.append((tkey, t, last_5m))
    for tkey, t, bar in fired:
        size, notional = calc_size(state['balance'], RISK_PCT, t['trigger_price'], t['sl'])
        if size <= 0: del state['triggers'][tkey]; continue
        # Trend-aware size on breakout fire
        bias = t.get('bias', 'neutral')
        size_mult = trend_v9.size_multiplier(t['side'], bias) if TREND_FILTER_ON else 1.0
        size *= size_mult
        notional *= size_mult
        if size <= 0: del state['triggers'][tkey]; continue
        log(f"TRIGGER-FIRE {t['coin']} {t['side']} 5m_close={bar['c']:.6f} trigger={t['trigger_price']:.6f} bias={bias}×{size_mult:.1f}")
        is_buy = (t['side'] == 'BUY')
        tid = _trade_id(t['coin'], t['side'])
        place_market(t['coin'], is_buy, size, label='BREAKOUT', cloid=_make_cloid(tid, 'E'))
        place_limit(t['coin'], not is_buy, size, t['sl'], reduce_only=True,
                     label='BREAKOUT-SL', cloid=_make_cloid(tid, 'S'))
        place_limit(t['coin'], not is_buy, size, t['tp'], reduce_only=True,
                     label='BREAKOUT-TP', cloid=_make_cloid(tid, 'T'))
        state['positions'][t['coin']] = {
            'side': t['side'], 'kind': 'BREAKOUT', 'wall_id': t['wall_id'],
            'entry': t['trigger_price'], 'sl': t['sl'], 'tp': t['tp'],
            'size': size, 'trade_id': tid,
            'opened_t': int(time.time()*1000),
            'filled_t': int(time.time()*1000),
        }
        state['fires_breakout_triggered'] += 1
        sibling = t.get('sibling_bounce_id')
        if sibling:
            for pkey in list(state['pending'].keys()):
                if state['pending'][pkey].get('wall_id') == sibling:
                    p = state['pending'][pkey]
                    if p.get('entry_oid'): cancel_order(p['coin'], p['entry_oid'])
                    log(f"  CANCEL-SIBLING bounce {pkey}")
                    del state['pending'][pkey]
        del state['triggers'][tkey]


def reconcile():
    """Sync V9 pending limits + V9-managed positions vs exchange.
    Foreign positions are LOGGED, never adopted.
    """
    open_orders = fetch_open_orders()
    open_oids = {o['oid'] for o in open_orders}
    acct = fetch_account()
    ex_pos = {}
    asset_positions_raw = []
    if acct:
        state['balance'] = float(acct['marginSummary']['accountValue'])
        asset_positions_raw = acct.get('assetPositions', [])
        for ap in asset_positions_raw:
            p = ap['position']
            sz = float(p['szi'])
            if abs(sz) > 1e-9:
                ex_pos[p['coin']] = sz

    to_remove = []
    for pkey, p in list(state['pending'].items()):
        oid = p.get('entry_oid')
        if oid and oid not in open_oids:
            coin = p['coin']
            if coin in ex_pos and abs(ex_pos[coin]) > 1e-9:
                state['positions'][coin] = {**p, 'filled_t': int(time.time()*1000),
                                              'opened_t': int(time.time()*1000)}
                log(f"  FILLED bounce {coin} {p['side']} @ {p['entry_price']}")
                sibling = p.get('sibling_breakout_id')
                if sibling:
                    for tkey in list(state['triggers'].keys()):
                        if state['triggers'][tkey].get('wall_id') == sibling:
                            del state['triggers'][tkey]
                            log(f"  CANCEL-SIBLING breakout {tkey}")
            else:
                log(f"  UNFILLED bounce {coin} removed")
            to_remove.append(pkey)
    for k in to_remove: del state['pending'][k]

    # V9-only position sync; foreign positions LOGGED, NEVER adopted
    state_coins = set(state['positions'].keys())
    ex_coins = set(ex_pos.keys())
    pruned = state_coins - ex_coins
    foreign = ex_coins - state_coins
    if pruned:
        for coin in pruned:
            log(f"  PRUNE {coin} — V9 closed externally (TP/SL/manual)")
            del state['positions'][coin]
    if foreign:
        for coin in foreign:
            sz = ex_pos[coin]
            log(f"  FOREIGN {coin} sz={sz:+.4f} — not V9, ignoring")
        state['foreign_logged'] += len(foreign)

    # Funding kill — exit V9 positions where 4h+ held AND funding has eaten unrealized PnL
    if FUNDING_KILL_ON and asset_positions_raw:
        kills = funding_v9.positions_to_close(state['positions'], asset_positions_raw,
                                                engine_prefix=ENGINE_PREFIX)
        for k in kills:
            log(f"FUNDING-KILL {k['coin']} {k['side']} {k['reason']}")
            is_buy_to_close = (k['side'] == 'SELL')
            market_close(k['coin'], is_buy_to_close, k['size'], label='FUNDING-KILL')
            state['funding_kills'] += 1
            # V9 prune happens next reconcile when HL shows position gone


def expire_stale():
    now_ms = int(time.time()*1000)
    for pkey in list(state['pending'].keys()):
        p = state['pending'][pkey]
        age = (now_ms - p.get('placed_t', now_ms)) / 1000
        if age > BOUNCE_EXPIRE_S:
            if p.get('entry_oid'): cancel_order(p['coin'], p['entry_oid'])
            log(f"  EXPIRE-BOUNCE {pkey} (age={age/3600:.1f}h)")
            del state['pending'][pkey]
    for tkey in list(state['triggers'].keys()):
        t = state['triggers'][tkey]
        age = (now_ms - t.get('armed_t', now_ms)) / 1000
        if age > TRIGGER_EXPIRE_S:
            log(f"  EXPIRE-TRIGGER {tkey} (age={age/3600:.1f}h)")
            del state['triggers'][tkey]


def tick():
    expire_stale()
    reconcile()
    state['tick_count'] += 1
    state['last_tick_t'] = int(time.time()*1000)
    log(f"━━ TICK #{state['tick_count']} ━━")
    log(f"Bal:${state['balance']:.2f} Pos:{len(state['positions'])} Pend:{len(state['pending'])} Trig:{len(state['triggers'])} | "
        f"BO_armed:{state['fires_breakout_armed']} BO_fired:{state['fires_breakout_triggered']} Bounce:{state['fires_bounce']} Spoof:{state['fires_spoof']} FundKill:{state['funding_kills']} Foreign:{state['foreign_logged']}")

    now_ts = time.time()
    summary = {}
    coins_this_tick = coins_for_tick(state['tick_count'], COINS)
    log(f"Scanning {len(coins_this_tick)}/{len(COINS)} coins this tick (tiered cadence)")

    def scan_one(coin):
        try:
            ob = fetch_orderbook(coin)
            if not ob or not ob.get('mid'): return None
            mid = ob['mid']
            tier_usd, tier_persistence = get_tier(coin)
            min_usd, min_persistence = DEPTH_BASE.threshold(coin)
            bid_res = cluster_walls(ob.get('bids', []), mid, 'bid', min_usd=min_usd, return_all_buckets=True)
            ask_res = cluster_walls(ob.get('asks', []), mid, 'ask', min_usd=min_usd, return_all_buckets=True)
            bid_walls, bid_bucket_usds = bid_res
            ask_walls, ask_bucket_usds = ask_res
            DEPTH_BASE.record(coin, bid_bucket_usds + ask_bucket_usds)

            # First-touch detection: last ~120s of mid history (4 polls × 30s),
            # not full 5m candle. Stale wicks no longer kill the wall.
            MID_HIST[coin].append(mid)
            mids = list(MID_HIST[coin])
            if len(mids) >= 2:
                last_low = min(mids); last_high = max(mids)
            else:
                last_low = mid; last_high = mid

            # Cadence-aware decay: walls polled less frequently must persist longer.
            cad = get_cadence(coin)
            decay_s = max(180.0, cad * POLL_INTERVAL_S * 1.5 + 60.0)

            tracked = TRACKER.update(coin, bid_walls + ask_walls, mid, now_ts,
                                       last_low, last_high, decay_s=decay_s)
            verified_b = [w for w in tracked if w.side=='bid' and w.persistence_polls >= min_persistence and w.times_tested == 0]
            verified_a = [w for w in tracked if w.side=='ask' and w.persistence_polls >= min_persistence and w.times_tested == 0]
            nb = min(verified_b, key=lambda w: w.distance_pct, default=None)
            na = min(verified_a, key=lambda w: w.distance_pct, default=None)
            sm = {
                'mid': mid, 'vb': len(verified_b), 'va': len(verified_a), 'nb': nb, 'na': na,
                'min_usd': min_usd, 'tier_usd': tier_usd, 'decay_s': decay_s,
                'all_buckets': len(bid_bucket_usds) + len(ask_bucket_usds),
                'near_miss_b': len([w for w in tracked if w.side=='bid' and w.persistence_polls >= max(1, min_persistence-1)]),
                'near_miss_a': len([w for w in tracked if w.side=='ask' and w.persistence_polls >= max(1, min_persistence-1)]),
            }
            atr_v = get_atr(coin)
            if atr_v <= 0: return (coin, sm, [], [], [], None, 'neutral', 0.0)
            existing_armed = [t for t in state['triggers'].values() if t.get('coin') == coin]
            bounces, breakouts = POLE.evaluate(coin, tracked, TRACKER, mid, atr_v, now_ts,
                                                  min_persistence_polls=min_persistence,
                                                  existing_armed_triggers=existing_armed)
            sps = SPOOF.evaluate(coin, tracked, TRACKER, mid, atr_v, now_ts)
            last_5m = get_5m_close(coin)
            bias = trend_v9.get_bias(coin, fetch_1h_for_trend) if TREND_FILTER_ON else 'neutral'
            return (coin, sm, bounces, breakouts, sps, last_5m, bias, atr_v)
        except Exception as e:
            log(f"  scan {coin} err: {e}")
            return None

    scan_results = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        for r in ex.map(scan_one, coins_this_tick):
            if r is not None: scan_results.append(r)

    for tup in scan_results:
        coin, sm, bounces, breakouts, sps, last_5m, bias, atr_v = tup
        summary[coin] = sm
        threshold_usd = sm.get('min_usd', 0)
        check_triggers(coin, last_5m, atr_v)
        for b in bounces:
            # wall_usd: lookup by wall_id in summary (nb/na)
            wall_usd = 0.0
            if sm.get('nb') and sm['nb'].wall_id == b.wall_id: wall_usd = sm['nb'].usd
            elif sm.get('na') and sm['na'].wall_id == b.wall_id: wall_usd = sm['na'].usd
            place_bounce(b, bias=bias, wall_usd=wall_usd, threshold_usd=threshold_usd)
        for bk in breakouts:
            wall_usd = 0.0
            base_wid = bk.wall_id.replace('|BO', '')
            if sm.get('nb') and sm['nb'].wall_id == base_wid: wall_usd = sm['nb'].usd
            elif sm.get('na') and sm['na'].wall_id == base_wid: wall_usd = sm['na'].usd
            arm_breakout(bk, bias=bias, wall_usd=wall_usd, threshold_usd=threshold_usd)
        for sp in sps:
            if sp['coin'] in state['positions']: continue
            if len(state['positions']) >= MAX_POSITIONS: continue
            size, notional = calc_size(state['balance'], RISK_PCT, sp['entry_price'], sp['sl_price'])
            if size <= 0: continue
            size_mult = trend_v9.size_multiplier(sp['side'], bias) if TREND_FILTER_ON else 1.0
            size *= size_mult
            notional *= size_mult
            if size <= 0: continue
            log(f"PLACE-SPOOF {sp['coin']} {sp['side']} entry={sp['entry_price']:.6f} sl={sp['sl_price']:.6f} tp={sp['tp_price']:.6f} rr={sp['rr']:.2f} bias={bias}×{size_mult:.1f} sz={size:.6f} ${notional:.2f}")
            log(f"  notes: {sp['notes']}")
            is_buy = (sp['side'] == 'BUY')
            tid = _trade_id(sp['coin'], sp['side'])
            place_market(sp['coin'], is_buy, size, label='SPOOF', cloid=_make_cloid(tid, 'E'))
            place_limit(sp['coin'], not is_buy, size, sp['sl_price'], reduce_only=True,
                         label='SPOOF-SL', cloid=_make_cloid(tid, 'S'))
            place_limit(sp['coin'], not is_buy, size, sp['tp_price'], reduce_only=True,
                         label='SPOOF-TP', cloid=_make_cloid(tid, 'T'))
            state['positions'][sp['coin']] = {
                'side': sp['side'], 'kind': 'SPOOF', 'entry': sp['entry_price'],
                'sl': sp['sl_price'], 'tp': sp['tp_price'], 'size': size,
                'trade_id': tid,
                'opened_t': int(time.time()*1000),
                'filled_t': int(time.time()*1000),
            }
            state['fires_spoof'] += 1

    coins_with_zones = [c for c, d in summary.items() if d['nb'] and d['na']]
    log(f"Wall map: {len(coins_with_zones)} coins with both fresh bid+ask verified walls")
    for c in coins_with_zones[:12]:
        d = summary[c]; nb = d['nb']; na = d['na']
        log(f"  {c:6s} mid={d['mid']:>11.4f} thr=${d['min_usd']/1000:.0f}k decay={d['decay_s']:.0f}s | BID ${nb.usd/1000:>5.0f}k @{nb.price:>11.4f} -{nb.distance_pct*100:.2f}% ({nb.persistence_polls}p) | ASK ${na.usd/1000:>5.0f}k @{na.price:>11.4f} +{na.distance_pct*100:.2f}% ({na.persistence_polls}p)")
    near_misses = [(c, d) for c, d in summary.items() if c not in coins_with_zones and (d.get('near_miss_b', 0) > 0 or d.get('near_miss_a', 0) > 0)]
    if near_misses:
        nm_log = ', '.join(f"{c}(b={d['near_miss_b']}/a={d['near_miss_a']},thr=${d['min_usd']/1000:.0f}k)" for c, d in near_misses[:8])
        log(f"Near-miss: {nm_log}")

    save_state()


def main():
    log("=== POLE RUNNER V9 START ===")
    log(f"  ENGINE_PREFIX: {ENGINE_PREFIX}")
    log(f"  PRECOG_URL: {PRECOG_URL}")
    log(f"  COINS: {len(COINS)}")
    log(f"  RISK_PCT: {RISK_PCT}, MAX_NOT_PCT: {MAX_NOTIONAL_PCT}, LEVERAGE: {LEVERAGE}")
    log(f"  MAX_POS: {MAX_POSITIONS}, MAX_PENDING: {MAX_PENDING}, MAX_TRIGGERS: {MAX_TRIGGERS}")
    log(f"  POLL_INTERVAL_S: {POLL_INTERVAL_S}, COIN_PACE_MS: {COIN_PACE_MS}")
    log(f"  EXPIRE: bounce={BOUNCE_EXPIRE_S/3600:.1f}h trigger={TRIGGER_EXPIRE_S/3600:.1f}h")
    log(f"  TREND_FILTER_ON: {TREND_FILTER_ON}, FUNDING_KILL_ON: {FUNDING_KILL_ON}")
    log(f"  WALLET: {WALLET[:10]+'...' if WALLET else 'NONE'}")
    load_state()
    global EXCHANGE
    EXCHANGE = init_sdk()
    while True:
        try: tick()
        except Exception as e:
            log(f"tick err: {e}"); traceback.print_exc()
        log(f"sleeping {POLL_INTERVAL_S}s")
        time.sleep(POLL_INTERVAL_S)

if __name__ == '__main__':
    main()
