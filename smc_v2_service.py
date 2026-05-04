#!/usr/bin/env python3
"""
smc_v2_service.py — Standalone SMC v2 (R3) live trader.

Runs alongside PreCog v8.28 on the same HL account. Total isolation:
  - Tags all orders with cloid prefix `smcv2_`
  - Owns only positions it placed (tracked in /var/data/smc_v2_state.json)
  - Does NOT touch precog.py, does NOT manage other engines' positions
  - Does NOT read/modify PreCog's state file

Strategy: SMC top-down (HTF=4H → MTF=1H → LTF=15m), Rank 3 config.
Setup pipeline: IDLE → IN_ZONE → SWEPT → ARMED → FILL.
Exits: 50% TP1 → SL→BE → 50% TP2 (or BE-stop, or time-stop).

Sizing: FIXED_NOTIONAL_USD per trade (default $25). Leverage: from leverage_map (default 10x).
"""
import os, sys, time, json, math, hashlib, traceback
from datetime import datetime, timezone
from collections import defaultdict, deque

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account


# ═══════════════════════════════════════════════════════
# CONFIG — R3 (locked from sweep results)
# ═══════════════════════════════════════════════════════
PARAMS = {
    'htf_lb': 5,
    'htf_displace': 1.75,
    'htf_max_age': 540,
    'ltf_lb': 4,
    'sweep_vol': 1.0,
    'mss_vol': 1.0,
    'displace': 2.0,
    'sl_buf_pct': 0.0003,
    'approach_pct': 0.03,
    'rr_min': 2.25,
    'timeout_bars': 40,
}

# Coins with strict-negative outcomes in 52-day backtest (n>=2, 0 wins)
BLACKLIST = {'IP', 'ATOM', 'AIXBT', 'ENS', 'OP', 'SKR', 'STRK', 'WLFI', 'kLUNC', 'BLAST'}

# Majors — handled by PreCog v8.28, not SMC v2
EXCLUDED_MAJORS = {'BTC','ETH','BNB','SOL','BCH','LTC','XRP','ADA','DOGE','AVAX','DOT','TRX','TON'}

# Sizing
FIXED_NOTIONAL_USD = float(os.environ.get('SMCV2_NOTIONAL_USD', '25'))
DEFAULT_LEVERAGE = int(os.environ.get('SMCV2_LEVERAGE', '10'))
MAX_CONCURRENT = int(os.environ.get('SMCV2_MAX_CONCURRENT', '20'))
LIVE_TRADING = os.environ.get('SMCV2_LIVE', '0') == '1'

# Timing
TICK_SEC = 60                       # main loop cadence
LTF_SCAN_INTERVAL_SEC = 5 * 60      # scan setups every 15m bar boundary
HTF_REFRESH_SEC = 4 * 3600          # refresh HTF state every 4h boundary
POSITION_CHECK_SEC = 30             # poll position state for TP1 fills
MAX_HOLD_BARS_LTF = 40 * 4          # 40 bars × 15m = 10h; keep liberal time-stop
MAX_HOLD_SEC = MAX_HOLD_BARS_LTF * 15 * 60

# Storage
STATE_PATH = os.environ.get('SMCV2_STATE_PATH', '/var/data/smc_v2_state.json')
LOG_BUFFER = deque(maxlen=500)


# ═══════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════
def log(msg):
    line = f'[{datetime.now(timezone.utc).isoformat()}] {msg}'
    print(line, flush=True)
    LOG_BUFFER.append(line)


# ═══════════════════════════════════════════════════════
# ENGINE — HTF / MTF / LTF (port of /tmp/smcv2/smc_v2_engine.py)
# ═══════════════════════════════════════════════════════
def atr_series(highs, lows, closes, period=14):
    n = len(closes)
    if n < 2: return [0.0]*n
    trs = [highs[0]-lows[0]]
    for i in range(1,n):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    out=[]; s=0.0
    for i,tr in enumerate(trs):
        if i<period: s+=tr; out.append(s/(i+1))
        else:        out.append((out[-1]*(period-1)+tr)/period)
    return out


def sma(values, period):
    out, s = [], 0.0
    for i,v in enumerate(values):
        s += v
        if i >= period: s -= values[i-period]
        out.append(s / min(i+1, period))
    return out


def htf_bias_and_zones(c4h, lb, displace_atr, max_age_bars):
    """Compute HTF state series. Returns list of {ts, bias, zones} per 4H bar close."""
    if len(c4h) < max(lb*2+1, 20):
        return []
    n = len(c4h)
    highs = [b['h'] for b in c4h]; lows = [b['l'] for b in c4h]
    closes = [b['c'] for b in c4h]; opens = [b['o'] for b in c4h]
    atr = atr_series(highs, lows, closes, 14)

    swing_h = []  # (idx, price)
    swing_l = []
    zones = []   # {top, bot, is_bull, kind, idx}
    states = []

    for i in range(n):
        # Detect pivot at i-lb
        ci = i - lb
        if ci >= lb:
            ph = highs[ci]; pl = lows[ci]
            is_ph = all(ph > highs[ci-k] and ph > highs[ci+k] for k in range(1,lb+1))
            is_pl = all(pl < lows[ci-k] and pl < lows[ci+k] for k in range(1,lb+1))
            if is_ph: swing_h.append((ci, ph))
            if is_pl: swing_l.append((ci, pl))

        # Detect new OB / FVG (only after enough bars)
        if i >= 2 and atr[i] > 0:
            disp = displace_atr * atr[i]
            sb = (closes[i] > opens[i]) and (closes[i]-opens[i]) > disp
            sbe = (closes[i] < opens[i]) and (opens[i]-closes[i]) > disp
            if sb and closes[i-1] < opens[i-1] and closes[i] > highs[i-1]:
                zones.append({'top': opens[i-1], 'bot': lows[i-1], 'is_bull': True, 'kind':'OB', 'idx': i-1})
            if sbe and closes[i-1] > opens[i-1] and closes[i] < lows[i-1]:
                zones.append({'top': highs[i-1], 'bot': opens[i-1], 'is_bull': False, 'kind':'OB', 'idx': i-1})
            ms = 0.3 * atr[i]
            if i >= 2 and lows[i] > highs[i-2] and (lows[i] - highs[i-2]) >= ms:
                zones.append({'top': lows[i], 'bot': highs[i-2], 'is_bull': True, 'kind':'FVG', 'idx': i})
            if i >= 2 and highs[i] < lows[i-2] and (lows[i-2] - highs[i]) >= ms:
                zones.append({'top': lows[i-2], 'bot': highs[i], 'is_bull': False, 'kind':'FVG', 'idx': i})

        # Mitigate + age out
        cutoff = i - max_age_bars
        zones = [z for z in zones
                 if z['idx'] >= cutoff
                 and not ((z['is_bull'] and lows[i] <= z['bot']) or
                          (not z['is_bull'] and highs[i] >= z['top']))]

        # Compute bias from last 3 swings each side
        bias = 'NONE'; trend_intact = False
        if len(swing_h) >= 2 and len(swing_l) >= 2:
            sh = swing_h[-3:]; sl = swing_l[-3:]
            hh = all(sh[j][1] > sh[j-1][1] for j in range(1, len(sh)))
            hl = all(sl[j][1] > sl[j-1][1] for j in range(1, len(sl)))
            ll = all(sl[j][1] < sl[j-1][1] for j in range(1, len(sl)))
            lh = all(sh[j][1] < sh[j-1][1] for j in range(1, len(sh)))
            if hh and hl: bias, trend_intact = 'BULL', True
            elif ll and lh: bias, trend_intact = 'BEAR', True

        states.append({
            't': c4h[i]['t'],
            'bias': bias,
            'trend_intact': trend_intact,
            'zones': list(zones),
            'last_swing_h': swing_h[-1][1] if swing_h else None,
            'last_swing_l': swing_l[-1][1] if swing_l else None,
        })
    return states


def htf_state_at(states, ts):
    """Find the latest HTF state at or before ts."""
    if not states: return None
    if ts < states[0]['t']: return None
    # Binary search
    lo, hi = 0, len(states)
    while lo < hi:
        mid = (lo+hi)//2
        if states[mid]['t'] <= ts: lo = mid+1
        else: hi = mid
    return states[lo-1] if lo > 0 else None


def precompute_mtf_pivots(c1h, lb):
    n = len(c1h)
    highs = [b['h'] for b in c1h]; lows = [b['l'] for b in c1h]
    swing_h, swing_l = [], []
    for i in range(n):
        ci = i - lb
        if ci >= lb:
            ph = highs[ci]; pl = lows[ci]
            is_ph = all(ph > highs[ci-k] and ph > highs[ci+k] for k in range(1,lb+1))
            is_pl = all(pl < lows[ci-k] and pl < lows[ci+k] for k in range(1,lb+1))
            if is_ph: swing_h.append((c1h[ci]['t'], ph))
            if is_pl: swing_l.append((c1h[ci]['t'], pl))
    return swing_h, swing_l


def mtf_state_ok(c1h, mtf_phs, mtf_pls, ts, htf_bias):
    """Return True if MTF structure_ok at time ts under given HTF bias."""
    if htf_bias == 'NONE': return False
    # Find current 1h bar at ts
    idx = -1
    for i, b in enumerate(c1h):
        if b['t'] <= ts: idx = i
        else: break
    if idx < 5: return False
    cl = c1h[idx]['c']
    # Find last swing high/low at or before ts
    last_h = None
    for t,p in reversed(mtf_phs):
        if t <= ts: last_h = p; break
    last_l = None
    for t,p in reversed(mtf_pls):
        if t <= ts: last_l = p; break
    if htf_bias == 'BULL':
        return last_l is not None and cl > last_l
    if htf_bias == 'BEAR':
        return last_h is not None and cl < last_h
    return False


def run_ltf(c15, htf_states, c1h, mtf_phs, mtf_pls, params):
    """Replay LTF state machine. Returns list of fired setups."""
    n = len(c15)
    if n < 30: return []
    LB = params['ltf_lb']
    sweep_vol = params['sweep_vol']
    mss_vol = params['mss_vol']
    displace = params['displace']
    sl_buf_pct = params['sl_buf_pct']
    rr_min = params['rr_min']
    timeout_bars = params['timeout_bars']
    approach_pct = params['approach_pct']

    highs = [b['h'] for b in c15]; lows = [b['l'] for b in c15]
    closes = [b['c'] for b in c15]; opens = [b['o'] for b in c15]
    vols = [b['v'] for b in c15]; times = [b['t'] for b in c15]
    atr = atr_series(highs, lows, closes, 14)
    vol_avg = sma(vols, 20)

    pivots_h, pivots_l = [], []
    state = 'IDLE'; state_bar = 0; setup = {}
    setups_fired = []

    for i in range(n):
        if i < max(LB*2+1, 20):
            continue
        ci = i - LB
        if ci >= LB:
            ph = highs[ci]; pl = lows[ci]
            is_ph = all(ph > highs[ci-k] and ph > highs[ci+k] for k in range(1,LB+1))
            is_pl = all(pl < lows[ci-k] and pl < lows[ci+k] for k in range(1,LB+1))
            if is_ph: pivots_h.append((ci, ph))
            if is_pl: pivots_l.append((ci, pl))

        htf = htf_state_at(htf_states, times[i])
        if not htf or htf['bias'] == 'NONE' or not htf['zones']:
            if state != 'IDLE': state = 'IDLE'
            continue
        if not mtf_state_ok(c1h, mtf_phs, mtf_pls, times[i], htf['bias']):
            if state != 'IDLE': state = 'IDLE'
            continue

        bull_setup = (htf['bias'] == 'BULL')

        # IDLE → IN_ZONE
        if state == 'IDLE':
            for z in htf['zones']:
                # Match bias: BULL needs is_bull demand zone, BEAR needs supply
                if bull_setup and not z['is_bull']: continue
                if (not bull_setup) and z['is_bull']: continue
                # Price near the zone (within approach_pct)
                if bull_setup:
                    # demand below; price approaching from above or inside
                    if lows[i] <= z['top'] * (1 + approach_pct) and highs[i] >= z['bot']:
                        state = 'IN_ZONE'; state_bar = i
                        setup = {'htf_zone': z, 'is_long': True}
                        break
                else:
                    if highs[i] >= z['bot'] * (1 - approach_pct) and lows[i] <= z['top']:
                        state = 'IN_ZONE'; state_bar = i
                        setup = {'htf_zone': z, 'is_long': False}
                        break
            continue

        # IN_ZONE → SWEPT
        if state == 'IN_ZONE':
            v_ok = vols[i] >= vol_avg[i] * sweep_vol
            last_pl_v = pivots_l[-1][1] if pivots_l else None
            last_ph_v = pivots_h[-1][1] if pivots_h else None
            if setup['is_long']:
                if last_pl_v is not None and lows[i] < last_pl_v and closes[i] > last_pl_v and v_ok:
                    setup.update({'sweep_wick': lows[i], 'sweep_idx': i, 'atr_at_sweep': atr[i]})
                    state = 'SWEPT'; state_bar = i
                    continue
            else:
                if last_ph_v is not None and highs[i] > last_ph_v and closes[i] < last_ph_v and v_ok:
                    setup.update({'sweep_wick': highs[i], 'sweep_idx': i, 'atr_at_sweep': atr[i]})
                    state = 'SWEPT'; state_bar = i
                    continue
            if (i - state_bar) > timeout_bars:
                state = 'IDLE'
            continue

        # SWEPT → ARMED (MSS confirmation)
        if state == 'SWEPT':
            v_ok = vols[i] >= vol_avg[i] * mss_vol
            disp_thresh = displace * atr[i]
            body = abs(closes[i] - opens[i])
            body_ok = body > disp_thresh * 0.4
            last_pl_v = pivots_l[-1][1] if pivots_l else None
            last_ph_v = pivots_h[-1][1] if pivots_h else None
            mss = False
            if setup['is_long']:
                if last_ph_v is not None and closes[i] > last_ph_v and closes[i] > opens[i] and v_ok and body_ok:
                    mss = True
            else:
                if last_pl_v is not None and closes[i] < last_pl_v and closes[i] < opens[i] and v_ok and body_ok:
                    mss = True

            if mss:
                sweep_wick = setup['sweep_wick']
                if setup['is_long']:
                    entry = max(opens[i], sweep_wick) if opens[i] < closes[i] else lows[i]
                    sl = sweep_wick * (1 - sl_buf_pct)
                    risk = entry - sl
                    if risk <= 0:
                        state = 'IDLE'; continue
                    tp1 = last_ph_v if last_ph_v and last_ph_v > entry else entry + risk*1.5
                    tp2 = entry + risk * 3.0
                else:
                    entry = min(opens[i], sweep_wick) if opens[i] > closes[i] else highs[i]
                    sl = sweep_wick * (1 + sl_buf_pct)
                    risk = sl - entry
                    if risk <= 0:
                        state = 'IDLE'; continue
                    tp1 = last_pl_v if last_pl_v and last_pl_v < entry else entry - risk*1.5
                    tp2 = entry - risk * 3.0

                rr_tp1 = abs(tp1 - entry) / risk
                rr_tp2 = abs(tp2 - entry) / risk
                if rr_tp2 >= rr_min and rr_tp1 >= 1.0:
                    setup.update({
                        'entry': entry, 'sl': sl, 'tp1': tp1, 'tp2': tp2,
                        'rr_tp1': rr_tp1, 'rr_tp2': rr_tp2,
                        'mss_idx': i, 'mss_t': times[i],
                    })
                    state = 'ARMED'; state_bar = i
                else:
                    state = 'IDLE'
            elif (i - state_bar) > timeout_bars:
                state = 'IDLE'
            continue

        # ARMED → FILL (price retests entry)
        if state == 'ARMED':
            entry = setup['entry']
            hit = (lows[i] <= entry) if setup['is_long'] else (highs[i] >= entry)
            if hit:
                setup_fired = dict(setup)
                setup_fired.update({'fill_idx': i, 'fill_t': times[i]})
                setups_fired.append(setup_fired)
                state = 'IDLE'; setup = {}
            elif (i - state_bar) > timeout_bars:
                state = 'IDLE'

    return setups_fired


# ═══════════════════════════════════════════════════════
# HL CLIENT
# ═══════════════════════════════════════════════════════
WALLET = os.environ.get('HL_ADDRESS') or os.environ.get('HYPERLIQUID_ACCOUNT')
PRIV_KEY = os.environ.get('HL_PRIVATE_KEY') or os.environ.get('PRIVATE_KEY')

if not WALLET or not PRIV_KEY:
    log('FATAL: HL_ADDRESS/HL_PRIVATE_KEY not set in env')
    sys.exit(1)

acct = Account.from_key(PRIV_KEY)
info = Info(constants.MAINNET_API_URL, skip_ws=True)
exchange = Exchange(acct, constants.MAINNET_API_URL, account_address=WALLET)

_META_CACHE = {}
def get_sz_decimals(coin):
    if not _META_CACHE:
        try:
            m = info.meta()
            for u in m['universe']:
                _META_CACHE[u['name']] = int(u.get('szDecimals', 0))
        except Exception as e:
            log(f'meta fetch err: {e}')
    return _META_CACHE.get(coin, 2)


def round_price(coin, px):
    if px <= 0: return px
    szD = get_sz_decimals(coin)
    max_dec = max(0, 6 - szD)
    sig_scale = 10 ** (5 - int(math.floor(math.log10(abs(px)))) - 1)
    px_sig = round(px * sig_scale) / sig_scale
    return round(px_sig, max_dec)


def round_size(coin, sz):
    return round(sz, get_sz_decimals(coin))


def get_universe():
    try:
        m = info.meta()
        coins = []
        for u in m['universe']:
            n = u.get('name')
            if not n: continue
            if u.get('isDelisted'): continue
            if n in EXCLUDED_MAJORS: continue
            if n in BLACKLIST: continue
            coins.append(n)
        return coins
    except Exception as e:
        log(f'universe fetch err: {e}')
        return []


# ═══════════════════════════════════════════════════════
# CANDLE FETCH — OKX primary (avoids HL CloudFront 429), HL fallback
# ═══════════════════════════════════════════════════════
import urllib.request
import urllib.parse

OKX_CANDLES = 'https://www.okx.com/api/v5/market/candles'
OKX_HISTORY = 'https://www.okx.com/api/v5/market/history-candles'

OKX_TF = {'4h': '4H', '1h': '1H', '15m': '15m', '5m': '5m'}

# HL coin → OKX inst (subset; full map in okx_fetch.py)
HL_OKX = {
    'kPEPE':'PEPE-USDT-SWAP', 'kSHIB':'SHIB-USDT-SWAP', 'kBONK':'BONK-USDT-SWAP',
    'kFLOKI':'FLOKI-USDT-SWAP', 'kDOGS':'DOGS-USDT-SWAP', 'kCAT':'CAT-USDT-SWAP',
    'kNEIRO':'NEIRO-USDT-SWAP', 'kLUNC':'LUNC-USDT-SWAP',
    'MATIC':'POL-USDT-SWAP', 'FTM':'S-USDT-SWAP', 'RNDR':'RENDER-USDT-SWAP',
}
# Coins not on OKX — return [] immediately (skip silently)
NOT_ON_OKX = {'PURR','HYPE','XMR','MKR','RUNE','VET','KAS','BAL','EOS','VVV',
              'STABLE','HFUN','OMNI','MEW','BIO','TST','MEGA','BABY',
              'FARTCOIN','TAO','HMSTR','SCR','GOAT','MOODENG','GRASS'}

_okx_last_call = [0.0]


def _okx_throttle():
    gap = 0.1  # 10 req/sec to stay well under OKX 20/2s limit
    delta = time.time() - _okx_last_call[0]
    if delta < gap:
        time.sleep(gap - delta)
    _okx_last_call[0] = time.time()


def _hl_to_okx_inst(coin):
    if coin in NOT_ON_OKX:
        return None
    return HL_OKX.get(coin, f'{coin}-USDT-SWAP')


def _okx_fetch_page(inst, tf, after_ms=None, history=False):
    """Fetch up to 300 (candles) or 100 (history-candles) bars from OKX."""
    url = OKX_HISTORY if history else OKX_CANDLES
    limit = 100 if history else 300
    params = {'instId': inst, 'bar': tf, 'limit': limit}
    if after_ms is not None:
        params['after'] = str(after_ms)
    qs = urllib.parse.urlencode(params)
    _okx_throttle()
    try:
        req = urllib.request.Request(f'{url}?{qs}',
            headers={'Accept':'application/json','User-Agent':'smcv2/1.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read())
    except Exception as e:
        return None, f'net err: {e}'
    if not isinstance(payload, dict) or payload.get('code') != '0':
        return None, f'okx err: {payload.get("code")} {payload.get("msg","")}'
    rows = payload.get('data') or []
    bars = []
    for k in rows:
        try:
            bars.append({'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
                         'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5])})
        except (IndexError, ValueError, TypeError):
            continue
    bars.sort(key=lambda b: b['t'])
    return bars, None


def fetch_candles_okx(coin, tf, days):
    """Paginated fetch from OKX. /candles for first page, /history-candles for older."""
    inst = _hl_to_okx_inst(coin)
    if inst is None:
        return []
    okx_tf = OKX_TF.get(tf, tf)
    target_start_ms = int(time.time()*1000) - days*86400*1000

    # First page: most recent 300 bars from /candles
    bars, err = _okx_fetch_page(inst, okx_tf, after_ms=None, history=False)
    if err:
        return []
    if not bars:
        return []

    # Paginate older via /history-candles using 'after' = oldest_t
    # Loop limited to avoid runaway calls
    for _ in range(20):
        if not bars or bars[0]['t'] <= target_start_ms:
            break
        oldest = bars[0]['t']
        page, err = _okx_fetch_page(inst, okx_tf, after_ms=oldest, history=True)
        if err or not page:
            break
        # Merge (page may overlap; dedupe on 't')
        seen = {b['t'] for b in bars}
        new = [b for b in page if b['t'] not in seen]
        if not new:
            break
        bars = sorted(new + bars, key=lambda b: b['t'])

    # Trim to target window
    bars = [b for b in bars if b['t'] >= target_start_ms]
    return bars


def fetch_candles(coin, tf, days):
    """Public fetcher: OKX with HL fallback."""
    bars = fetch_candles_okx(coin, tf, days)
    if bars and len(bars) >= 30:
        return bars
    # Fallback to HL only if OKX returned too little (rare)
    end = int(time.time()*1000)
    target = end - days*86400000
    intvl_ms = {'4h': 4*3600*1000, '1h': 3600*1000, '15m': 15*60*1000}[tf]
    win_ms = 4900 * intvl_ms
    seen = {}
    cur_end = end
    for _ in range(5):
        if cur_end <= target: break
        cur_start = max(cur_end - win_ms, target)
        try:
            raw = info.candles_snapshot(coin, tf, cur_start, cur_end)
        except Exception:
            break
        if not isinstance(raw, list) or not raw:
            break
        new = 0; oldest = cur_end
        for c in raw:
            try:
                t = int(c['t'])
                if t in seen: continue
                seen[t] = {'t': t, 'o': float(c['o']), 'h': float(c['h']),
                           'l': float(c['l']), 'c': float(c['c']), 'v': float(c['v'])}
                new += 1
                if t < oldest: oldest = t
            except Exception:
                pass
        if new == 0 or oldest <= target: break
        cur_end = oldest - 1
        time.sleep(0.4)
    return sorted(seen.values(), key=lambda x: x['t'])


# ═══════════════════════════════════════════════════════
# STATE PERSISTENCE
# ═══════════════════════════════════════════════════════
def load_state():
    default = {'positions': {}, 'history': [], 'last_scan_ts': 0,
               'last_fill_check_ts': 0, 'consec_losses': 0}
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH) as f:
                loaded = json.load(f)
            for k,v in default.items():
                if k not in loaded: loaded[k] = v
            return loaded
    except Exception as e:
        log(f'state load err: {e}')
    return default


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log(f'state save err: {e}')


# ═══════════════════════════════════════════════════════
# EXECUTION — entry + native SL + native TP1 + native TP2
# ═══════════════════════════════════════════════════════
def make_cloid(coin, suffix):
    """16-byte hex cloid via SHA-256. Uniquely encodes coin+timestamp+suffix
    regardless of coin name length (was truncating suffix for coins >=5 chars).
    """
    raw = f'smcv2_{coin}_{int(time.time()*1000)}_{suffix}'.encode('utf-8')
    return '0x' + hashlib.sha256(raw).hexdigest()[:32]


def calc_size(coin, entry_px):
    """Size = notional / price, rounded to coin's szDecimals."""
    sz = FIXED_NOTIONAL_USD / entry_px
    return round_size(coin, sz)


def place_entry(coin, is_long, entry_px, sz, cloid):
    """Place limit entry order at entry_px (post-only / GTC)."""
    if not LIVE_TRADING:
        log(f'  [DRY] place_entry {coin} {"BUY" if is_long else "SELL"} sz={sz} px={entry_px} cloid={cloid}')
        return {'status': 'ok', 'response': {'data': {'statuses': [{'resting': {'oid': 0}}]}}}
    try:
        return exchange.order(coin, is_long, sz, entry_px,
                              {'limit': {'tif': 'Gtc'}}, cloid=cloid)
    except Exception as e:
        log(f'  entry order err {coin}: {e}')
        return None


def place_native_stop(coin, is_long_pos, sz, trigger_px, cloid, is_market=True):
    """Place native trigger order: stop loss against position direction."""
    is_buy = not is_long_pos  # closing direction
    trigger = {'triggerPx': trigger_px, 'isMarket': is_market, 'tpsl': 'sl'}
    if not LIVE_TRADING:
        log(f'  [DRY] place_sl {coin} closeIsBuy={is_buy} sz={sz} trig={trigger_px} cloid={cloid}')
        return {'status': 'ok'}
    try:
        return exchange.order(coin, is_buy, sz, trigger_px,
                              {'trigger': trigger}, reduce_only=True, cloid=cloid)
    except Exception as e:
        log(f'  sl order err {coin}: {e}')
        return None


def place_native_tp(coin, is_long_pos, sz, trigger_px, cloid):
    """Place native trigger TP: market on trigger."""
    is_buy = not is_long_pos
    trigger = {'triggerPx': trigger_px, 'isMarket': True, 'tpsl': 'tp'}
    if not LIVE_TRADING:
        log(f'  [DRY] place_tp {coin} closeIsBuy={is_buy} sz={sz} trig={trigger_px} cloid={cloid}')
        return {'status': 'ok'}
    try:
        return exchange.order(coin, is_buy, sz, trigger_px,
                              {'trigger': trigger}, reduce_only=True, cloid=cloid)
    except Exception as e:
        log(f'  tp order err {coin}: {e}')
        return None


def cancel_order(coin, cloid):
    """Cancel a resting order by cloid (string, 0x-prefixed 32-hex).
    HL SDK has cancel(coin, oid) for numeric oid and cancel_by_cloid(coin, cloid)
    for string cloid — must use the latter since we never store numeric oids.
    """
    if not LIVE_TRADING:
        log(f'  [DRY] cancel {coin} cloid={cloid}')
        return {'status': 'ok'}
    if not cloid:
        log(f'  cancel {coin}: no cloid provided')
        return None
    try:
        return exchange.cancel_by_cloid(coin, cloid)
    except Exception as e:
        # Fallback: SDK may require Cloid object wrapper instead of raw string
        try:
            from hyperliquid.utils.signing import Cloid
            return exchange.cancel_by_cloid(coin, Cloid.from_str(cloid))
        except Exception as e2:
            log(f'  cancel err {coin} cloid={str(cloid)[:18]}...: {e} (wrapper fallback: {e2})')
            return None


def market_close(coin, is_long_pos, sz, slippage=0.005, cloid=None):
    """Close exactly `sz` of position via reduce_only IOC limit at mid±slippage.
    Uses our own tracked cloid (returned for caller to record).

    Does NOT use exchange.market_close — that closes the entire wallet
    position on this coin, which would wipe shared positions on this account.
    """
    is_buy = not is_long_pos
    if cloid is None:
        cloid = make_cloid(coin, 'mc')

    if not LIVE_TRADING:
        log(f'  [DRY] market_close {coin} closeIsBuy={is_buy} sz={sz} cloid={cloid[:14]}...')
        return {'status': 'ok', 'cloid': cloid}

    # Fetch mid for slippage-protected IOC. Retry once on transient failure.
    mid = 0.0
    for _attempt in range(2):
        try:
            mids = info.all_mids()
            mid = float(mids.get(coin, 0)) if mids else 0
            if mid > 0: break
        except Exception as e:
            log(f'  market_close {coin}: mid fetch err: {e}')
        time.sleep(0.3)
    if mid <= 0:
        log(f'  market_close {coin}: NO MID — refusing to close (would risk '
            f'unsized close that takes out shared positions). Will retry next reconcile.')
        return None

    if is_buy:
        limit_px = round_price(coin, mid * (1 + slippage))
    else:
        limit_px = round_price(coin, mid * (1 - slippage))
    sz_rounded = round_size(coin, sz)
    if sz_rounded <= 0:
        log(f'  market_close {coin}: size rounded to 0 (sz={sz})')
        return None

    try:
        res = exchange.order(coin, is_buy, sz_rounded, limit_px,
                             {'limit': {'tif': 'Ioc'}}, reduce_only=True, cloid=cloid)
        log(f'  market_close {coin}: sent reduce_only IOC sz={sz_rounded} '
            f'px={limit_px} (mid={mid}, slip={slippage*100:.1f}%)')
        return res
    except Exception as e:
        log(f'  close err {coin}: {e}')
        return None


# ═══════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ═══════════════════════════════════════════════════════
def fire_setup(coin, setup, state):
    """Place entry + SL + TP1 + TP2 atomically (best-effort sequential)."""
    if len(state['positions']) >= MAX_CONCURRENT:
        log(f'  {coin} skip: max concurrent {MAX_CONCURRENT}')
        return False
    if coin in state['positions']:
        log(f'  {coin} skip: already have position')
        return False

    is_long = setup['is_long']
    entry = round_price(coin, setup['entry'])
    sl = round_price(coin, setup['sl'])
    tp1 = round_price(coin, setup['tp1'])
    tp2 = round_price(coin, setup['tp2'])
    sz_total = calc_size(coin, entry)
    if sz_total <= 0:
        log(f'  {coin} skip: size 0 (notional={FIXED_NOTIONAL_USD}, entry={entry})')
        return False
    sz_half = round_size(coin, sz_total / 2)
    sz_half2 = round_size(coin, sz_total - sz_half)  # ensure exact total

    log(f'FIRE {coin} {"LONG" if is_long else "SHORT"} entry={entry} sl={sl} tp1={tp1} tp2={tp2} sz={sz_total} (half={sz_half}+{sz_half2})')

    cloid_entry = make_cloid(coin, 'e')
    cloid_sl = make_cloid(coin, 's')
    cloid_tp1 = make_cloid(coin, 't1')
    cloid_tp2 = make_cloid(coin, 't2')

    res_entry = place_entry(coin, is_long, entry, sz_total, cloid_entry)
    if not res_entry or res_entry.get('status') != 'ok':
        log(f'  {coin} entry rejected; aborting setup')
        return False

    # Place protective orders immediately (will activate on fill)
    place_native_stop(coin, is_long, sz_total, sl, cloid_sl)
    place_native_tp(coin, is_long, sz_half, tp1, cloid_tp1)
    place_native_tp(coin, is_long, sz_half2, tp2, cloid_tp2)

    state['positions'][coin] = {
        'is_long': is_long,
        'entry': entry, 'sl': sl, 'tp1': tp1, 'tp2': tp2,
        'sz_total': sz_total, 'sz_half': sz_half, 'sz_half2': sz_half2,
        'cloid_entry': cloid_entry, 'cloid_sl': cloid_sl,
        'cloid_tp1': cloid_tp1, 'cloid_tp2': cloid_tp2,
        'fired_t': int(time.time()*1000),
        'mss_t': setup.get('mss_t'),
        'rr_tp1': setup.get('rr_tp1'), 'rr_tp2': setup.get('rr_tp2'),
        'phase': 'pending_fill',  # pending_fill → live → tp1_filled → done
    }
    save_state(state)
    return True


def fetch_recent_fills(since_ms):
    """Fetch user fills since since_ms via userFillsByTime endpoint.
    Returns list of fill dicts, each with keys including 'cloid', 'time',
    'coin', 'px', 'sz', 'side', 'closedPnl', 'oid'.
    """
    try:
        import urllib.request as _ur
        body = json.dumps({
            'type': 'userFillsByTime',
            'user': WALLET,
            'startTime': since_ms,
            'endTime': int(time.time()*1000),
        }).encode('utf-8')
        req = _ur.Request('https://api.hyperliquid.xyz/info', data=body,
                          headers={'Content-Type': 'application/json'})
        with _ur.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        return data if isinstance(data, list) else []
    except Exception as e:
        log(f'fills fetch err: {e}')
        return []


def reconcile_positions(state):
    """Cloid-matched reconciliation. Isolated from PreCog activity.

    Source of truth: HL userFillsByTime → match fills against our cloids.
    Phase transitions:
      pending_fill → live: entry leg cumulative fill ≥ 95% of sz_total
      live → tp1_filled: tp1 leg fill (then cancel SL, place new SL at entry/BE)
      live → done:       sl leg fill OR tp2 leg fill (rare, gap)
      tp1_filled → done: sl leg fill (BE-stop) OR tp2 leg fill
    """
    last_check = state.get('last_fill_check_ts', 0)
    if last_check == 0:
        last_check = int(time.time()*1000) - 24*3600*1000  # cold start: last 24h

    fills = fetch_recent_fills(last_check)

    # Build cloid → (coin, leg_key) map from current positions
    cloid_map = {}
    for coin, pos in state['positions'].items():
        for leg_key, cloid_field in (('entry','cloid_entry'), ('sl','cloid_sl'),
                                      ('tp1','cloid_tp1'), ('tp2','cloid_tp2'),
                                      ('close','cloid_close')):
            c = pos.get(cloid_field)
            if c:
                cloid_map[c] = (coin, leg_key)

    fills_processed = 0
    for fill in fills:
        cloid = fill.get('cloid')
        if not cloid: continue
        if cloid not in cloid_map: continue
        coin, leg = cloid_map[cloid]
        pos = state['positions'].get(coin)
        if not pos or pos.get('phase') == 'done': continue

        try:
            fill_px = float(fill.get('px', 0))
            fill_sz = float(fill.get('sz', 0))
        except (ValueError, TypeError):
            continue
        fills_processed += 1
        log(f'  {coin} {leg.upper()} fill sz={fill_sz} px={fill_px}')

        # ENTRY leg
        if leg == 'entry' and pos['phase'] == 'pending_fill':
            cum = pos.get('entry_filled_sz', 0.0) + fill_sz
            pos['entry_filled_sz'] = cum
            if cum >= pos['sz_total'] * 0.95:
                pos['phase'] = 'live'
                pos['actual_entry_px'] = fill_px  # last partial fill price
                log(f'  {coin} ENTRY FILLED cum={cum:.6f} (≥95% of {pos["sz_total"]})')

        # TP1 leg → move SL to BE
        elif leg == 'tp1' and pos['phase'] in ('live', 'pending_fill'):
            log(f'  {coin} TP1 hit at {fill_px} — moving SL to BE @ {pos["entry"]}')
            cancel_order(coin, pos.get('cloid_sl'))
            new_cloid = make_cloid(coin, 'sb')
            place_native_stop(coin, pos['is_long'], pos['sz_half2'], pos['entry'], new_cloid)
            pos['cloid_sl'] = new_cloid
            pos['sl'] = pos['entry']
            pos['phase'] = 'tp1_filled'
            pos['tp1_fill_px'] = fill_px
            pos['tp1_fill_t'] = fill.get('time', int(time.time()*1000))

        # TP2 leg → done
        elif leg == 'tp2' and pos['phase'] in ('live', 'tp1_filled'):
            log(f'  {coin} TP2 hit at {fill_px} — runner closed')
            pos['phase'] = 'done'
            pos['close_reason'] = 'tp2'
            pos['close_px'] = fill_px
            pos['closed_t'] = fill.get('time', int(time.time()*1000))
            state['history'].append(pos)
            del state['positions'][coin]

        # SL leg → done (label as BE-stop if it fired after TP1)
        elif leg == 'sl' and pos['phase'] in ('live', 'tp1_filled'):
            label = 'be_stop' if pos['phase'] == 'tp1_filled' else 'sl'
            log(f'  {coin} {label.upper()} hit at {fill_px}')
            pos['phase'] = 'done'
            pos['close_reason'] = label
            pos['close_px'] = fill_px
            pos['closed_t'] = fill.get('time', int(time.time()*1000))
            state['history'].append(pos)
            del state['positions'][coin]

        # CLOSE leg (time-stop close fill arrived)
        elif leg == 'close' and pos['phase'] in ('live', 'tp1_filled'):
            log(f'  {coin} TIME-STOP close confirmed at {fill_px}')
            pos['phase'] = 'done'
            pos['close_reason'] = 'time_stop'
            pos['close_px'] = fill_px
            pos['closed_t'] = fill.get('time', int(time.time()*1000))
            state['history'].append(pos)
            del state['positions'][coin]

    # Time-stop check (independent of fills)
    for coin, pos in list(state['positions'].items()):
        if pos.get('phase') in ('live', 'tp1_filled'):
            age_sec = (time.time()*1000 - pos['fired_t']) / 1000
            if age_sec > MAX_HOLD_SEC and not pos.get('cloid_close'):
                log(f'  {coin} TIME STOP — sized reduce_only IOC ({age_sec/3600:.1f}h held, '
                    f'phase={pos["phase"]})')
                sz_remain = pos['sz_half2'] if pos['phase']=='tp1_filled' else pos['sz_total']
                close_cloid = make_cloid(coin, 'mc')
                res = market_close(coin, pos['is_long'], sz_remain, cloid=close_cloid)
                if res is not None:
                    # Record cloid so the close-fill drives phase=done on next reconcile
                    pos['cloid_close'] = close_cloid
                    pos['time_stop_sent_t'] = int(time.time()*1000)
                # If res is None (no mid, etc.), retry next reconcile

    # Update fill cursor: latest fill time minus 60s overlap; or now-60s if no fills
    if fills:
        latest_fill_t = max(int(f.get('time', 0)) for f in fills)
        state['last_fill_check_ts'] = max(latest_fill_t - 60_000, last_check)
    else:
        state['last_fill_check_ts'] = int(time.time()*1000) - 60_000

    if fills_processed > 0:
        log(f'reconcile: {fills_processed} of {len(fills)} fills matched our cloids')
    save_state(state)


# ═══════════════════════════════════════════════════════
# SCAN LOOP
# ═══════════════════════════════════════════════════════
_last_full_scan = 0
_coin_data_cache = {}  # {coin: {'4h': [...], '1h': [...], '15m': [...], 'fetched': ts}}


def scan_for_setups(state):
    """Run engine across the universe; fire any new setups."""
    global _last_full_scan
    coins = get_universe()
    log(f'scanning {len(coins)} coins (open positions: {len(state["positions"])})')

    fired = 0
    for coin in coins:
        if coin in state['positions']:
            continue
        if len(state['positions']) >= MAX_CONCURRENT:
            log(f'  max concurrent reached ({MAX_CONCURRENT}); stopping scan')
            break
        try:
            cache = _coin_data_cache.get(coin, {})
            now_s = time.time()
            need_refresh = (not cache) or (now_s - cache.get('fetched', 0)) > 600
            if need_refresh:
                c4 = fetch_candles(coin, '4h', 90)
                c1 = fetch_candles(coin, '1h', 90)
                c15 = fetch_candles(coin, '15m', 52)
                if not (c4 and c1 and c15):
                    continue
                if len(c4) < 30 or len(c1) < 100 or len(c15) < 500:
                    continue
                cache = {'4h': c4, '1h': c1, '15m': c15, 'fetched': now_s}
                _coin_data_cache[coin] = cache
            c4, c1, c15 = cache['4h'], cache['1h'], cache['15m']

            # Run engine
            htfs = htf_bias_and_zones(c4, PARAMS['htf_lb'], PARAMS['htf_displace'], PARAMS['htf_max_age'])
            if not htfs: continue
            mtf_phs, mtf_pls = precompute_mtf_pivots(c1, lb=PARAMS['ltf_lb'])
            setups = run_ltf(c15, htfs, c1, mtf_phs, mtf_pls, PARAMS)

            # Only act on setups that fired in the LAST bar (avoid replaying historical)
            if not setups: continue
            last_bar_t = c15[-1]['t']
            recent = [s for s in setups if s['fill_t'] >= last_bar_t - 15*60*1000]
            if not recent: continue

            # Take the most recent setup
            s = max(recent, key=lambda x: x['fill_t'])
            log(f'  setup {coin}: {"LONG" if s["is_long"] else "SHORT"} @ {s["entry"]:.5f}')
            if fire_setup(coin, s, state):
                fired += 1
        except Exception as e:
            log(f'  scan err {coin}: {e}')
            traceback.print_exc()

    log(f'scan complete: {fired} new setups fired')
    state['last_scan_ts'] = int(time.time()*1000)
    save_state(state)


# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════
def main():
    log(f'SMC v2 service starting | wallet={WALLET[:10]}... | LIVE={LIVE_TRADING} | notional=${FIXED_NOTIONAL_USD}')
    log(f'PARAMS={PARAMS}')
    log(f'BLACKLIST={sorted(BLACKLIST)}')
    state = load_state()
    log(f'loaded state: {len(state["positions"])} open positions, {len(state["history"])} closed')

    last_scan = state.get('last_scan_ts', 0) / 1000.0
    last_reconcile = 0

    while True:
        try:
            now = time.time()
            # Reconcile every POSITION_CHECK_SEC
            if now - last_reconcile >= POSITION_CHECK_SEC:
                reconcile_positions(state)
                last_reconcile = now

            # Scan on 15m bar close (every 15min, offset by 30s for HL bar closure)
            mins_in_15 = (int(now) % 900)
            on_bar_boundary = (mins_in_15 < 60)  # within 1 min after :00 :15 :30 :45
            if on_bar_boundary and (now - last_scan) >= LTF_SCAN_INTERVAL_SEC - 60:
                scan_for_setups(state)
                last_scan = now

            time.sleep(TICK_SEC)
        except KeyboardInterrupt:
            log('SIGINT received — exiting cleanly')
            save_state(state)
            sys.exit(0)
        except Exception as e:
            log(f'main loop err: {e}\n{traceback.format_exc()}')
            time.sleep(30)


if __name__ == '__main__':
    main()
