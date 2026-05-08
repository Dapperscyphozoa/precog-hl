#!/usr/bin/env python3
"""V10 runner — full SMC framework engine.

Separate Render service from V9. DRY_RUN. Reads same wallet for sizing math.
Polls production /orderbook/<coin> for wall confluence (Stage 5).
Polls HL public API for 4H, 1H, 5m candles.

Flow per coin per tick (subject to tier cadence):
  1. Fetch 4H bars → detect unmitigated OBs (Stage 1)
  2. Check if current price near unmitigated OB (Stage 2)
  3. Fetch 1H bars → MTF structure check (Stage 3)
  4. Fetch 5m bars → sweep + MSS + LTF OB (Stage 4)
  5. Fetch /orderbook/<coin> → verify wall at LTF OB body (Stage 5)
  6. Build setup with limit at OB body, SL past OB wick + 1 tick (Stage 6)
  7. Place orders (DRY) or log only

State at /var/data/pole_state_v10.json. Production untouched.
"""
import json, os, sys, time, traceback, urllib.request
from datetime import datetime, timezone
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from coin_tiers import (DepthBaseline, get_tier, coins_for_tick, ALL_COINS,
                          refresh_hl_volumes, get_volume_threshold, get_volume)
from pole_engine_v10 import (
    detect_consolidation_obs, get_unmitigated_zone_at, mtf_structure_intact,
    detect_ltf_setup, build_setup, OBZone, Setup, atr,
)
# V9 wall logic for Stage 5 confluence
from pole_engine_v9 import cluster_walls, WallTracker

PRECOG_URL       = os.environ.get('PRECOG_URL', 'https://precog-i8c3.onrender.com')
HL_API           = 'https://api.hyperliquid.xyz/info'
DRY_RUN          = os.environ.get('DRY_RUN', '1') == '1'
LIVE             = os.environ.get('LIVE_TRADING', '0') == '1' and not DRY_RUN
PRIVATE_KEY      = os.environ.get('HL_PRIVATE_KEY', '')
WALLET           = os.environ.get('HL_ADDRESS') or os.environ.get('HYPERLIQUID_ACCOUNT', '')
RISK_PCT         = float(os.environ.get('RISK_PCT', '0.0025'))
LEVERAGE         = int(os.environ.get('LEVERAGE', '5'))
MAX_POSITIONS    = int(os.environ.get('MAX_POSITIONS', '6'))
MAX_PENDING      = int(os.environ.get('MAX_PENDING_LIMITS', '12'))
MAX_NOTIONAL_PCT = float(os.environ.get('MAX_NOTIONAL_PCT', '0.20'))
POLL_INTERVAL_S  = int(os.environ.get('POLL_INTERVAL_S', '60'))  # slower than V9, framework needs less frequency
SCAN_WORKERS     = int(os.environ.get('SCAN_WORKERS', '6'))
CANDLE_CACHE_S   = int(os.environ.get('CANDLE_CACHE_S', '300'))  # 5min cache for 4h/1h candles
SETUP_EXPIRE_S   = int(os.environ.get('SETUP_EXPIRE_S', '14400'))  # 4h to fill limit
WALL_CONFLUENCE  = os.environ.get('WALL_CONFLUENCE', '1') == '1'
MIN_WALL_USD     = float(os.environ.get('MIN_WALL_USD', '100000'))  # min wall at LTF OB
COINS_OVERRIDE   = os.environ.get('COINS', '').strip()
COINS            = ([c.strip().upper() for c in COINS_OVERRIDE.split(',') if c.strip()]
                    if COINS_OVERRIDE else list(ALL_COINS))
STATE_FILE       = os.environ.get('STATE_FILE', '/var/data/pole_state_v10.json')

state = {
    'balance': 0.0, 'positions': {}, 'pending': {},
    'tick_count': 0, 'last_tick_t': 0,
    'fires_total': 0,
    'qualified_setups': 0, 'wall_confluence_blocked': 0,
    'stage_drops': {'no_4h_data':0,'no_zones':0,'no_5m_recent':0,'not_at_zone':0,
                    'no_1h_data':0,'mtf_block':0,'no_5m_data':0,'no_ltf':0,
                    'already_fired':0,'build_setup_fail':0},
    'log': [],
}

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

def fetch_candles(coin, interval, days):
    end = int(time.time()*1000); start = end - days*86400000
    raw = http_post(HL_API, {'type':'candleSnapshot','req':{'coin':coin,'interval':interval,'startTime':start,'endTime':end}})
    if not raw: return []
    bars = [{'t':b['t'],'o':float(b['o']),'h':float(b['h']),'l':float(b['l']),'c':float(b['c']),'v':float(b['v'])} for b in raw]
    bars.sort(key=lambda x: x['t'])
    return bars

# Cache: (coin, interval) → (timestamp, bars)
CANDLE_CACHE = {}
# HL coin tick sizes (absolute price increment per coin)
COIN_TICKS = {}

def fetch_tick_sizes():
    """Pull HL meta + asset contexts. Derive tick_size from markPx decimal precision."""
    raw = http_post(HL_API, {'type':'metaAndAssetCtxs'})
    if not raw or len(raw) < 2: return
    universe, ctxs = raw[0]['universe'], raw[1]
    for u, c in zip(universe, ctxs):
        name = u['name']
        mark = c.get('markPx', '')
        if not mark: continue
        if '.' in mark:
            px_decimals = len(mark.split('.')[1])
        else:
            px_decimals = 0
        COIN_TICKS[name] = 10 ** -px_decimals
    log(f"Loaded tick sizes for {len(COIN_TICKS)} coins")

def get_candles_cached(coin, interval, days):
    key = (coin, interval)
    cached = CANDLE_CACHE.get(key)
    if cached and time.time() - cached[0] < CANDLE_CACHE_S:
        return cached[1]
    bars = fetch_candles(coin, interval, days)
    CANDLE_CACHE[key] = (time.time(), bars)
    return bars

# Per-coin setup deduplication: don't re-fire same OB
COIN_FIRED_OBS = {}  # coin -> set of (body_top, body_bottom) tuples
def already_fired(coin, ob_body_top, ob_body_bottom, tolerance=0.001):
    fired = COIN_FIRED_OBS.get(coin, set())
    for (t, b) in fired:
        if abs(t - ob_body_top) / ob_body_top < tolerance and abs(b - ob_body_bottom) / ob_body_bottom < tolerance:
            return True
    return False

def mark_fired(coin, ob_body_top, ob_body_bottom):
    COIN_FIRED_OBS.setdefault(coin, set()).add((ob_body_top, ob_body_bottom))
    if len(COIN_FIRED_OBS[coin]) > 50:
        # Trim oldest
        COIN_FIRED_OBS[coin] = set(list(COIN_FIRED_OBS[coin])[-50:])

def calc_size(balance, entry, sl):
    if balance <= 0: return 0, 0
    risk_amt = balance * RISK_PCT
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
                log(f"Loaded state: pos={len(state['positions'])} pend={len(state['pending'])}")
    except Exception as e: log(f"load_state err: {e}")

def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w') as f: json.dump(state, f, default=str)
    except Exception as e: log(f"save_state err: {e}")

EXCHANGE = None
WALL_TRACKER = WallTracker(max_history=30)
DEPTH_BASE = DepthBaseline(window=20, multiplier=8.0)

def init_sdk():
    if DRY_RUN:
        log("SDK skipped (DRY_RUN)"); return None
    try:
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        from eth_account import Account
        wallet = Account.from_key(PRIVATE_KEY)
        ex = Exchange(wallet, constants.MAINNET_API_URL, account_address=WALLET)
        log(f"SDK initialized (LIVE)")
        return ex
    except Exception as e:
        log(f"SDK init failed: {e}"); return None

def place_limit(coin, is_buy, size, price, reduce_only=False, label=''):
    if DRY_RUN or not LIVE or EXCHANGE is None:
        log(f"  [DRY] LIMIT {coin} {'BUY' if is_buy else 'SELL'} sz={size:.6f} px={price:.6f} {label}")
        return {'status':'ok','response':{'data':{'statuses':[{'resting':{'oid':int(time.time()*1000000)}}]}}}
    try:
        return EXCHANGE.order(coin, is_buy, size, price, {'limit':{'tif':'Gtc'}}, reduce_only=reduce_only)
    except Exception as e:
        log(f"  limit err {coin}: {e}"); return None

def fetch_hl_l2(coin):
    """Direct HL L2Book fetch for HL-native tokens not on multi-venue feed."""
    raw = http_post(HL_API, {'type': 'l2Book', 'coin': coin})
    if not raw or 'levels' not in raw: return None
    levels = raw['levels']
    if len(levels) < 2: return None
    bids_raw, asks_raw = levels[0], levels[1]
    # HL format: each level = {'px': '...', 'sz': '...', 'n': N}
    bids = [{'price': float(l['px']), 'size': float(l['sz']),
             'usd': float(l['px'])*float(l['sz'])} for l in bids_raw]
    asks = [{'price': float(l['px']), 'size': float(l['sz']),
             'usd': float(l['px'])*float(l['sz'])} for l in asks_raw]
    mid = (bids[0]['price'] + asks[0]['price']) / 2 if bids and asks else 0
    return {'mid': mid, 'bids': bids, 'asks': asks}


def wall_confluence_check(coin: str, ob_body_top: float, ob_body_bottom: float, side: str) -> tuple:
    """Stage 5: verify there's a wall at the LTF OB (production aggregator OR HL L2 fallback).

    Threshold is volume-driven per coin: K × sqrt(24h_notional_volume).
    Falls back to MIN_WALL_USD env var if volume cache is empty.
    HL-L2 fallback uses 40% of computed threshold (single-venue depth).
    """
    if not WALL_CONFLUENCE:
        return True, 0, 'wall confluence disabled'
    ob = fetch_orderbook(coin)
    used_hl_fallback = False
    if not ob or not ob.get('mid'):
        ob = fetch_hl_l2(coin)
        used_hl_fallback = True
        if not ob or not ob.get('mid'):
            return False, 0, 'no orderbook data (prod + HL both empty)'
    bids = ob.get('bids', [])
    asks = ob.get('asks', [])
    target_price = ob_body_bottom if side == 'BUY' else ob_body_top
    relevant_orders = bids if side == 'BUY' else asks
    cluster_usd = sum(o['usd'] for o in relevant_orders
                       if abs(o['price'] - target_price) / target_price < 0.003)
    # Volume-driven base threshold; fall back to env-var if no volume cached
    base_thr = get_volume_threshold(coin)
    if base_thr <= 5000.0:  # floor returned — volume not yet loaded
        base_thr = MIN_WALL_USD
    # HL single-venue gets 40% of the multi-venue threshold
    threshold = base_thr * 0.4 if used_hl_fallback else base_thr
    passed = cluster_usd >= threshold
    src = 'HL-L2' if used_hl_fallback else 'multi-venue'
    vol = get_volume(coin)
    vol_str = f"vol=${(vol or 0)/1e6:.1f}M" if vol else "vol=?"
    return passed, cluster_usd, f"{src} {vol_str} target={target_price:.6f} cluster=${cluster_usd/1000:.0f}k thr=${threshold/1000:.0f}k"

def evaluate_coin(coin: str) -> Optional[Setup]:
    """Run full framework evaluation for one coin. Returns Setup or None."""
    sd = state['stage_drops']
    bars_4h = get_candles_cached(coin, '4h', 60)
    if len(bars_4h) < 30: sd['no_4h_data'] += 1; return None

    zones_4h = detect_consolidation_obs(bars_4h, '4h', displacement_atr_mult=1.5,
                                          min_consol_bars=2, max_consol_bars=8)
    if not zones_4h: sd['no_zones'] += 1; return None

    bars_5m_recent = get_candles_cached(coin, '5m', 1)
    if len(bars_5m_recent) < 5: sd['no_5m_recent'] += 1; return None
    current_price = bars_5m_recent[-1]['c']
    cur_4h_idx = len(bars_4h) - 1
    zone, bias = get_unmitigated_zone_at(zones_4h, cur_4h_idx, current_price, proximity_pct=0.005)
    if zone is None: sd['not_at_zone'] += 1; return None

    log(f"  STAGE2-PASS {coin} {bias} @ {zone.type} zone [{zone.body_bottom:.6f}-{zone.body_top:.6f}]")

    bars_1h = get_candles_cached(coin, '1h', 14)
    if len(bars_1h) < 30: sd['no_1h_data'] += 1; return None
    if not mtf_structure_intact(bars_1h, bias): sd['mtf_block'] += 1; return None

    log(f"  STAGE3-PASS {coin} MTF intact")

    bars_5m = get_candles_cached(coin, '5m', 7)
    if len(bars_5m) < 30: sd['no_5m_data'] += 1; return None
    bars_5m_window = bars_5m[-50:]
    ltf = detect_ltf_setup(bars_5m_window, bias)
    if ltf is None: sd['no_ltf'] += 1; return None

    log(f"  STAGE4-PASS {coin} {ltf['side']} ob_body=[{ltf['ob_body_bottom']:.6f}-{ltf['ob_body_top']:.6f}]")

    if already_fired(coin, ltf['ob_body_top'], ltf['ob_body_bottom']):
        sd['already_fired'] += 1; return None

    # Stage 6: build setup (computes SL past wick + 1 tick, R:R checks)
    tick = COIN_TICKS.get(coin, 0.0001)
    setup = build_setup(coin, ltf, zone, bars_1h, bars_4h, zones_4h,
                          tick_size=tick, sl_buffer_ticks=2, sl_min_buffer_pct=0.0005, min_rr_to_tp1=1.5)
    if setup is None:
        sd['build_setup_fail'] += 1
        log(f"  BUILD-FAIL {coin} {ltf['side']} (R:R<2 or no TPs)")
        return None

    # Stage 5: wall confluence check (after setup so we know body edges)
    state['qualified_setups'] += 1
    passed, wall_usd, wall_notes = wall_confluence_check(
        coin, setup.ltf_ob_body_top, setup.ltf_ob_body_bottom, setup.side
    )
    if not passed:
        state['wall_confluence_blocked'] += 1
        log(f"  WALL-BLOCK {coin} {setup.side} ({wall_notes})")
        return None
    setup.notes += f" | wall ${wall_usd/1000:.0f}k confluence ✓"

    return setup

def place_setup(setup: Setup):
    """Place the bracket: entry limit + SL + TP1 (50%) + TP2 (50%)."""
    coin = setup.coin
    if coin in state['positions']:
        log(f"  skip {coin}: position exists"); return
    if any(p['coin']==coin for p in state['pending'].values()):
        log(f"  skip {coin}: pending exists"); return
    if len(state['pending']) >= MAX_PENDING:
        log(f"  skip {coin}: max pending"); return

    size, notional = calc_size(state['balance'], setup.entry_price, setup.sl_price)
    if size <= 0:
        log(f"  size=0 skip"); return

    half = size / 2.0
    log(f"PLACE {setup.side} {coin} entry={setup.entry_price:.6f} sl={setup.sl_price:.6f} "
        f"tp1={setup.tp1_price:.6f} tp2={setup.tp2_price:.6f} "
        f"R:R1={setup.rr_to_tp1:.2f} R:R2={setup.rr_to_tp2:.2f} "
        f"sl_dist={setup.sl_distance_pct*100:.2f}% sz={size:.6f} ${notional:.2f}")
    log(f"  {setup.notes}")

    is_buy = (setup.side == 'BUY')
    or_res = place_limit(coin, is_buy, size, setup.entry_price, reduce_only=False, label='ENTRY')
    if not or_res: return
    entry_oid = or_res.get('response',{}).get('data',{}).get('statuses',[{}])[0].get('resting',{}).get('oid')

    sl_res = place_limit(coin, not is_buy, size, setup.sl_price, reduce_only=True, label='SL')
    tp1_res = place_limit(coin, not is_buy, half, setup.tp1_price, reduce_only=True, label='TP1')
    tp2_res = place_limit(coin, not is_buy, half, setup.tp2_price, reduce_only=True, label='TP2')

    pkey = f"{coin}|{setup.side}|{setup.entry_price:.8f}"
    state['pending'][pkey] = {
        'coin': coin, 'side': setup.side,
        'entry': setup.entry_price, 'sl': setup.sl_price,
        'tp1': setup.tp1_price, 'tp2': setup.tp2_price,
        'rr1': setup.rr_to_tp1, 'rr2': setup.rr_to_tp2,
        'sl_dist_pct': setup.sl_distance_pct,
        'size': size, 'notional': notional,
        'entry_oid': entry_oid,
        'placed_t': int(time.time()*1000),
        'ob_body_top': setup.ltf_ob_body_top, 'ob_body_bottom': setup.ltf_ob_body_bottom,
        'ob_wick_top': setup.ltf_ob_wick_top, 'ob_wick_bottom': setup.ltf_ob_wick_bottom,
        'sweep_wick': setup.sweep_wick,
    }
    state['fires_total'] += 1
    mark_fired(coin, setup.ltf_ob_body_top, setup.ltf_ob_body_bottom)

def reconcile():
    acct = fetch_account()
    if acct:
        state['balance'] = float(acct['marginSummary']['accountValue'])
    # Expire stale pending
    now_ms = int(time.time()*1000)
    expired = []
    for pkey, p in list(state['pending'].items()):
        age_s = (now_ms - p.get('placed_t', now_ms)) / 1000
        if age_s > SETUP_EXPIRE_S:
            expired.append(pkey)
    for k in expired:
        log(f"  EXPIRE-PENDING {k}")
        del state['pending'][k]

def tick():
    reconcile()
    refresh_hl_volumes()  # volume-driven wall threshold; rate-limited internally
    state['tick_count'] += 1
    state['last_tick_t'] = int(time.time()*1000)
    log(f"━━ TICK #{state['tick_count']} ━━")
    log(f"Bal:${state['balance']:.2f} Pos:{len(state['positions'])} Pend:{len(state['pending'])} | "
        f"Total fires:{state['fires_total']} Qualified:{state['qualified_setups']} WallBlocked:{state['wall_confluence_blocked']}")
    sd = state['stage_drops']
    log(f"  Drops: zones={sd['no_zones']} not_at_zone={sd['not_at_zone']} mtf_block={sd['mtf_block']} no_ltf={sd['no_ltf']} build_fail={sd['build_setup_fail']}")

    coins_this_tick = coins_for_tick(state['tick_count'], COINS)
    log(f"Scanning {len(coins_this_tick)}/{len(COINS)} coins this tick")

    setups = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        for setup in ex.map(evaluate_coin, coins_this_tick):
            if setup is not None: setups.append(setup)

    log(f"Setups generated: {len(setups)}")
    for s in setups:
        place_setup(s)

    save_state()

def main():
    log("=== POLE RUNNER V10 (FRAMEWORK) START ===")
    log(f"  PRECOG_URL: {PRECOG_URL}")
    log(f"  COINS: {len(COINS)}")
    log(f"  DRY_RUN: {DRY_RUN}, LIVE: {LIVE}")
    log(f"  RISK_PCT: {RISK_PCT}, MAX_NOT_PCT: {MAX_NOTIONAL_PCT}, LEVERAGE: {LEVERAGE}")
    log(f"  MAX_POS: {MAX_POSITIONS}, MAX_PENDING: {MAX_PENDING}")
    log(f"  POLL_INTERVAL_S: {POLL_INTERVAL_S}, SCAN_WORKERS: {SCAN_WORKERS}")
    log(f"  WALL_CONFLUENCE: {WALL_CONFLUENCE}, MIN_WALL_USD: ${MIN_WALL_USD}")
    log(f"  SETUP_EXPIRE_S: {SETUP_EXPIRE_S}")
    log(f"  WALLET: {WALLET[:10]+'...' if WALLET else 'NONE'}")
    fetch_tick_sizes()
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
