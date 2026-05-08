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
    # Stage 4 (LTF) sub-drop instrumentation
    'ltf_drops': {'no_pivots':0,'no_atr':0,'no_sweep':0,'no_local_window':0,
                   'no_mss':0,'no_ob_window':0},
    # Outcome tracker (paper P&L)
    'wins_tp1_tp2': 0,        # both TPs hit
    'wins_tp1_be': 0,         # TP1 hit, runner stopped at breakeven (still net +)
    'wins_tp1_betimeout': 0,  # TP1 hit, runner force-closed at BE after RUNNER_BE_TIMEOUT_S
    'losses_sl': 0,           # full loss before TP1
    'expired_unfilled': 0,    # entry never filled within SETUP_EXPIRE_S
    'closed_open_eow': 0,     # filled but neither SL nor TP1 hit before tracker timeout
    'pnl_pct_total': 0.0,     # cumulative paper PnL %, fee-adjusted
    'recent_trades': [],      # last 20 closed trades for inspection
    # Persistent OB-fired dedup (replaces in-memory COIN_FIRED_OBS)
    'fired_obs': {},          # coin -> list of [body_top, body_bottom]
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
def already_fired(coin, ob_body_top, ob_body_bottom, tolerance=0.001):
    fired = state['fired_obs'].get(coin, [])
    for entry in fired:
        t, b = entry[0], entry[1]
        if abs(t - ob_body_top) / ob_body_top < tolerance and abs(b - ob_body_bottom) / ob_body_bottom < tolerance:
            return True
    return False

def mark_fired(coin, ob_body_top, ob_body_bottom):
    lst = state['fired_obs'].setdefault(coin, [])
    lst.append([ob_body_top, ob_body_bottom])
    if len(lst) > 50:
        # Trim oldest, keep last 50
        state['fired_obs'][coin] = lst[-50:]

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
    ltf, ltf_drop = detect_ltf_setup(bars_5m_window, bias)
    if ltf is None:
        sd['no_ltf'] += 1
        if ltf_drop:
            state['ltf_drops'][ltf_drop] = state['ltf_drops'].get(ltf_drop, 0) + 1
        return None

    log(f"  STAGE4-PASS {coin} {ltf['side']} ob_body=[{ltf['ob_body_bottom']:.6f}-{ltf['ob_body_top']:.6f}]")

    if already_fired(coin, ltf['ob_body_top'], ltf['ob_body_bottom']):
        sd['already_fired'] += 1; return None

    # Stage 5 (moved earlier): wall confluence on the LTF OB body before paying for build_setup
    state['qualified_setups'] += 1
    side_for_wall = ltf['side']
    passed, wall_usd, wall_notes = wall_confluence_check(
        coin, ltf['ob_body_top'], ltf['ob_body_bottom'], side_for_wall
    )
    if not passed:
        state['wall_confluence_blocked'] += 1
        log(f"  WALL-BLOCK {coin} {side_for_wall} ({wall_notes})")
        return None

    # Stage 6: build setup (computes SL past wick + 1 tick, R:R checks)
    tick = COIN_TICKS.get(coin, 0.0001)
    setup = build_setup(coin, ltf, zone, bars_1h, bars_4h, zones_4h,
                          tick_size=tick, sl_buffer_ticks=2, sl_min_buffer_pct=0.0005, min_rr_to_tp1=1.5)
    if setup is None:
        sd['build_setup_fail'] += 1
        log(f"  BUILD-FAIL {coin} {ltf['side']} (R:R<2 or no TPs)")
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

    sl_res  = place_limit(coin, not is_buy, size, setup.sl_price,  reduce_only=True, label='SL')
    tp1_res = place_limit(coin, not is_buy, half, setup.tp1_price, reduce_only=True, label='TP1')
    tp2_res = place_limit(coin, not is_buy, half, setup.tp2_price, reduce_only=True, label='TP2')

    # Atomic bracket guarantee — only meaningful in LIVE mode
    if LIVE and EXCHANGE is not None:
        bracket_failed = (not sl_res) or (not tp1_res) or (not tp2_res)
        if bracket_failed:
            log(f"  BRACKET-FAIL {coin} sl={bool(sl_res)} tp1={bool(tp1_res)} tp2={bool(tp2_res)} — rolling back")
            # 1) Cancel any of the bracket legs that DID land
            for leg_res, leg_name in ((sl_res,'SL'), (tp1_res,'TP1'), (tp2_res,'TP2')):
                if not leg_res: continue
                leg_oid = leg_res.get('response',{}).get('data',{}).get('statuses',[{}])[0].get('resting',{}).get('oid')
                if leg_oid:
                    try: EXCHANGE.cancel(coin, leg_oid)
                    except Exception as e: log(f"    cancel {leg_name} err: {e}")
            # 2) Cancel the entry if still resting
            if entry_oid:
                try: EXCHANGE.cancel(coin, entry_oid)
                except Exception as e: log(f"    cancel ENTRY err: {e}")
            # 3) If entry already filled (race), force a market close to avoid naked exposure
            try:
                acct = fetch_account()
                pos_sz = 0.0
                for ap in (acct or {}).get('assetPositions', []):
                    if ap.get('position', {}).get('coin') == coin:
                        pos_sz = float(ap['position'].get('szi') or 0.0); break
                if abs(pos_sz) > 0:
                    log(f"    entry filled before rollback — emergency market close sz={pos_sz}")
                    EXCHANGE.market_close(coin)
            except Exception as e:
                log(f"    emergency-close err: {e}")
            return  # do not record this as a fire

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
        'last_scanned_t': int(time.time()*1000),  # bookmark for fetch_1m_since
        'ob_body_top': setup.ltf_ob_body_top, 'ob_body_bottom': setup.ltf_ob_body_bottom,
        'ob_wick_top': setup.ltf_ob_wick_top, 'ob_wick_bottom': setup.ltf_ob_wick_bottom,
        'sweep_wick': setup.sweep_wick,
    }
    state['fires_total'] += 1
    mark_fired(coin, setup.ltf_ob_body_top, setup.ltf_ob_body_bottom)
    save_state()  # persist immediately — a crash now would otherwise lose the fire + dedup

FEE_RT = 0.0006   # round-trip fee assumption (3bp each side)
TRACKER_TIMEOUT_S    = int(os.environ.get('TRACKER_TIMEOUT_S', '86400'))     # close as 'open_eow' after 24h
RUNNER_BE_TIMEOUT_S  = int(os.environ.get('RUNNER_BE_TIMEOUT_S', '7200'))    # force runner to BE after 2h post-TP1


def fetch_1m_since(coin, since_ms):
    """Pull 1m candles from since_ms to now."""
    raw = http_post(HL_API, {'type':'candleSnapshot','req':{
        'coin':coin,'interval':'1m','startTime':since_ms,
        'endTime':int(time.time()*1000)
    }})
    if not raw: return []
    bars = [{'t':b['t'],'o':float(b['o']),'h':float(b['h']),
             'l':float(b['l']),'c':float(b['c']),'v':float(b['v'])} for b in raw]
    bars.sort(key=lambda x: x['t'])
    return bars


def record_outcome(p, outcome, pnl_pct):
    """Append closed trade to tracker + bump counters."""
    counter_map = {
        'TP1_TP2': 'wins_tp1_tp2',
        'TP1_BE':  'wins_tp1_be',
        'TP1_BETIMEOUT': 'wins_tp1_betimeout',
        'SL':      'losses_sl',
        'EXPIRED': 'expired_unfilled',
        'OPEN_EOW': 'closed_open_eow',
    }
    state[counter_map[outcome]] += 1
    state['pnl_pct_total'] += pnl_pct
    rec = {
        'coin': p['coin'], 'side': p['side'],
        'entry': p.get('entry'), 'sl': p.get('sl'),
        'tp1': p.get('tp1'), 'tp2': p.get('tp2'),
        'rr1': p.get('rr1'), 'sl_dist_pct': p.get('sl_dist_pct'),
        'placed_t': p.get('placed_t'), 'fill_t': p.get('fill_t'),
        'closed_t': int(time.time()*1000),
        'outcome': outcome, 'pnl_pct': pnl_pct,
    }
    state['recent_trades'].append(rec)
    if len(state['recent_trades']) > 20:
        state['recent_trades'] = state['recent_trades'][-20:]
    save_state()  # persist immediately so a mid-tick crash doesn't lose the outcome


def reconcile():
    """Track paper trades: fills, outcomes, expiries.

    Pending → Position when entry price is traversed (limit fill).
    Position → Closed when SL/TP1/TP2 hit (walks forward in 1m candles).
    Pending expires after SETUP_EXPIRE_S without fill.
    Position closes as OPEN_EOW after TRACKER_TIMEOUT_S without resolution.
    """
    acct = fetch_account()
    if acct:
        state['balance'] = float(acct['marginSummary']['accountValue'])

    now_ms = int(time.time()*1000)

    # --- Step 1: pending → fill detection ---
    pending_filled = []
    pending_expired = []
    for pkey, p in list(state['pending'].items()):
        age_s = (now_ms - p.get('placed_t', now_ms)) / 1000
        if age_s > SETUP_EXPIRE_S:
            pending_expired.append(pkey)
            continue
        # Fetch 1m bars only since last scan (bookmark) — falls back to placed_t for legacy entries
        since_ms = p.get('last_scanned_t') or p.get('placed_t', now_ms)
        bars = fetch_1m_since(p['coin'], since_ms)
        if not bars:
            p['last_scanned_t'] = now_ms
            continue
        entry = p['entry']; side = p['side']
        for b in bars:
            if side == 'BUY' and b['l'] <= entry:
                p['fill_t'] = b['t']; p['fill_price'] = entry
                pending_filled.append((pkey, p)); break
            if side == 'SELL' and b['h'] >= entry:
                p['fill_t'] = b['t']; p['fill_price'] = entry
                pending_filled.append((pkey, p)); break
        # Advance bookmark to last bar we examined (whether filled or not)
        p['last_scanned_t'] = bars[-1]['t']

    for pkey in pending_expired:
        p = state['pending'][pkey]
        log(f"  EXPIRE-PENDING {p['coin']} {p['side']} (no fill within {SETUP_EXPIRE_S}s)")
        record_outcome(p, 'EXPIRED', 0.0)
        del state['pending'][pkey]

    for pkey, p in pending_filled:
        log(f"  FILLED {p['coin']} {p['side']} @ {p['fill_price']:.6f}")
        p['last_scanned_t'] = p['fill_t']  # reset bookmark to fill_t so position scan starts there
        state['positions'][p['coin']] = p
        del state['pending'][pkey]

    # --- Step 2: position outcome detection ---
    closed = []
    for coin, p in list(state['positions'].items()):
        age_s = (now_ms - p.get('fill_t', now_ms)) / 1000
        # Once TP1 has hit, scan from tp1_t forward (cheaper); else scan from last bookmark
        scan_since = p.get('tp1_t') or p.get('last_scanned_t') or p.get('fill_t', now_ms)
        bars = fetch_1m_since(coin, scan_since)
        if not bars:
            if age_s > TRACKER_TIMEOUT_S:
                closed.append((coin, 'OPEN_EOW', 0.0))
            continue
        side = p['side']; entry = p['fill_price']; sl = p['sl']
        tp1 = p['tp1']; tp2 = p['tp2']

        # If TP1 already hit on a previous tick, skip the pre-TP1 search and go straight to runner watch
        if p.get('tp1_t'):
            tp1_idx = -1  # sentinel: TP1 already in the past
            sl_idx = None
        else:
            sl_idx = tp1_idx = None
            for i, b in enumerate(bars):
                if side == 'BUY':
                    if b['l'] <= sl and sl_idx is None: sl_idx = i
                    if b['h'] >= tp1 and tp1_idx is None: tp1_idx = i
                else:
                    if b['h'] >= sl and sl_idx is None: sl_idx = i
                    if b['l'] <= tp1 and tp1_idx is None: tp1_idx = i
                if sl_idx is not None or tp1_idx is not None: break
        # SL hit first (or simultaneously) → full loss
        if sl_idx is not None and (tp1_idx is None or sl_idx <= tp1_idx):
            sl_pct = (sl - entry) / entry if side == 'BUY' else (entry - sl) / entry
            closed.append((coin, 'SL', sl_pct - FEE_RT))
            continue
        # TP1 hit (now or earlier) → 50% banked, watch runner for TP2 / BE-stop / time-BE
        if tp1_idx is not None:
            # Stamp tp1_t once
            if not p.get('tp1_t'):
                p['tp1_t'] = bars[tp1_idx]['t']
                p['tp1_partial_pnl'] = 0.5 * (((tp1 - entry)/entry if side == 'BUY' else (entry - tp1)/entry) - FEE_RT/2)
            tp1_pct = (tp1 - entry) / entry if side == 'BUY' else (entry - tp1) / entry
            partial = p.get('tp1_partial_pnl', 0.5 * (tp1_pct - FEE_RT/2))
            outcome = None; runner_pnl = 0.0
            # Walk runner candles — start either after tp1_idx (if hit this tick) or from the very start of bars (subsequent ticks, scan_since == tp1_t)
            start_j = tp1_idx + 1 if tp1_idx >= 0 else 0
            for j in range(start_j, len(bars)):
                b = bars[j]
                if side == 'BUY':
                    if b['l'] <= entry:
                        outcome = 'TP1_BE'; runner_pnl = 0.5 * (0.0 - FEE_RT/2); break
                    if b['h'] >= tp2:
                        tp2_pct = (tp2 - entry) / entry
                        outcome = 'TP1_TP2'; runner_pnl = 0.5 * (tp2_pct - FEE_RT/2); break
                else:
                    if b['h'] >= entry:
                        outcome = 'TP1_BE'; runner_pnl = 0.5 * (0.0 - FEE_RT/2); break
                    if b['l'] <= tp2:
                        tp2_pct = (entry - tp2) / entry
                        outcome = 'TP1_TP2'; runner_pnl = 0.5 * (tp2_pct - FEE_RT/2); break
            if outcome is None:
                # Runner still open — apply hard-BE timeout if too long since TP1
                runner_age_s = (now_ms - p.get('tp1_t', now_ms)) / 1000
                if runner_age_s > RUNNER_BE_TIMEOUT_S:
                    outcome = 'TP1_BETIMEOUT'; runner_pnl = 0.5 * (0.0 - FEE_RT/2)
                else:
                    # leave position in state, advance bookmark to last bar
                    p['last_scanned_t'] = bars[-1]['t']
                    continue
            closed.append((coin, outcome, partial + runner_pnl))
            continue
        # Neither SL nor TP yet — advance bookmark, then check tracker timeout
        p['last_scanned_t'] = bars[-1]['t']
        if age_s > TRACKER_TIMEOUT_S:
            last = bars[-1]['c']
            unr = (last - entry) / entry if side == 'BUY' else (entry - last) / entry
            closed.append((coin, 'OPEN_EOW', unr - FEE_RT))

    for coin, outcome, pnl_pct in closed:
        p = state['positions'][coin]
        record_outcome(p, outcome, pnl_pct)
        log(f"  CLOSE {coin} {p['side']} → {outcome} pnl={pnl_pct*100:+.2f}%")
        del state['positions'][coin]

def tick():
    reconcile()
    refresh_hl_volumes()  # volume-driven wall threshold; rate-limited internally
    state['tick_count'] += 1
    state['last_tick_t'] = int(time.time()*1000)
    log(f"━━ TICK #{state['tick_count']} ━━")
    log(f"Bal:${state['balance']:.2f} Pos:{len(state['positions'])} Pend:{len(state['pending'])} | "
        f"Total fires:{state['fires_total']} Qualified:{state['qualified_setups']} WallBlocked:{state['wall_confluence_blocked']}")
    # Win/loss tracker (paper)
    wins = state['wins_tp1_tp2'] + state['wins_tp1_be'] + state['wins_tp1_betimeout']
    losses = state['losses_sl']
    closed_count = wins + losses + state['expired_unfilled'] + state['closed_open_eow']
    wr = (wins / max(1, wins + losses)) * 100 if (wins + losses) else 0
    log(f"  Tracker: closed={closed_count} (W:{state['wins_tp1_tp2']}+{state['wins_tp1_be']}+{state['wins_tp1_betimeout']}/L:{state['losses_sl']}/EXP:{state['expired_unfilled']}/EOW:{state['closed_open_eow']}) "
        f"WR={wr:.0f}% PnL%={state['pnl_pct_total']*100:+.2f}")
    sd = state['stage_drops']
    log(f"  Drops: zones={sd['no_zones']} not_at_zone={sd['not_at_zone']} mtf_block={sd['mtf_block']} no_ltf={sd['no_ltf']} build_fail={sd['build_setup_fail']}")
    ld = state['ltf_drops']
    log(f"  LTF-drops: piv={ld['no_pivots']} atr={ld['no_atr']} sweep={ld['no_sweep']} window={ld['no_local_window']} mss={ld['no_mss']} ob={ld['no_ob_window']}")

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
    log(f"  SETUP_EXPIRE_S: {SETUP_EXPIRE_S}, RUNNER_BE_TIMEOUT_S: {RUNNER_BE_TIMEOUT_S}, TRACKER_TIMEOUT_S: {TRACKER_TIMEOUT_S}")
    log(f"  STATE_FILE: {STATE_FILE}")
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
