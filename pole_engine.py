"""pole_engine.py — Pole-to-Pole / OB-to-OB SMC entry engine.

DESIGN
======
The trade thesis: in chop, price travels from one liquidity pole to the
opposite liquidity pole, sweeping engineered liquidity at each end before
reversing. Enter on the sweep, target the opposite pole, stop just outside
the swept extreme.

A POLE is any structural level at which price is likely to reverse:
  - Unmitigated bullish/bearish Order Block (OB): the last opposite candle
    before a Break of Structure (BOS). Mitigation = price has wicked into
    the OB body since formation.
  - Unmitigated Fair Value Gap (FVG): 3-bar gap (candle[i-1].h < candle[i+1].l
    for bullish, inverse for bearish). Mitigation = wick has filled gap.
  - Equal highs / equal lows: 2+ swing highs (or lows) within 0.1% of each
    other = engineered liquidity. Resting stops above/below.
  - Session highs/lows: Asian H/L, London H/L, NY H/L for current day.
  - Previous day high/low (PDH/PDL).
  - Previous week high/low (PWH/PWL).
  - Last swing high / swing low (5-bar pivot).

ENTRY trigger:
  Price WICKS through a pole (sweep), then closes back inside the prior
  range on the same bar (or next bar). Direction = opposite of sweep.

  Bullish entry: wick below an unmitigated bullish OB / equal lows / PDL,
  close back above. Long. SL = swept low - buffer. TP = nearest unmitigated
  pole above current price.

  Bearish entry: inverse.

R:R rule:
  TP must be ≥ 2× SL distance. If nearest opposite pole is closer, skip.

MITIGATION:
  Once a pole is touched within `MITIGATION_TOL_PCT` (default 0.1%), it's
  marked mitigated and removed from the active pole list. Mitigated poles
  do not produce entries; they ARE the trade for already-open positions
  but become non-fireable.

PER-LEVEL COOLDOWN:
  Each pole produces at most ONE fire. Once consumed, never refires until
  evicted from cache (~24h TTL). This prevents re-firing the same pole on
  every tick after it sweeps.

TIMEFRAME:
  Entry detection on 15m bars (matches chop oscillation cadence).
  Pole detection on 1h + 4h bars (structural levels).
  Higher TF poles weighted more (4h pole > 1h pole).

OUTPUT:
  detect(coin, bars_15m, bars_1h, bars_4h, now_ts) -> dict | None
  Returns: {side, entry, sl, tp, swept_pole, target_pole, rr, max_hold_s,
            confluences, source}
"""

import os
import time
from typing import Optional, List, Dict, Any

# ─── CONFIG ───────────────────────────────────────────────────────
PIVOT_LB              = int(os.environ.get('POLE_PIVOT_LB', '5'))           # bars each side for swing detection
EQUAL_TOL_PCT         = float(os.environ.get('POLE_EQUAL_TOL_PCT', '0.001')) # 0.1% — equal highs/lows
MITIGATION_TOL_PCT    = float(os.environ.get('POLE_MITIG_TOL_PCT', '0.001')) # 0.1% — pole touched = mitigated
SL_BUFFER_PCT         = float(os.environ.get('POLE_SL_BUFFER', '0.003'))     # 0.3% past swept extreme — wider, less wick clipping
MIN_RR                = float(os.environ.get('POLE_MIN_RR', '2.0'))          # 1:2 minimum
MAX_RR                = float(os.environ.get('POLE_MAX_RR', '8.0'))          # cap insane RR (probably broken target)
MAX_HOLD_HOURS        = int(os.environ.get('POLE_MAX_HOLD_HOURS', '12'))     # 12h max — was 24, timeouts winning at 12-18h
TP_FRACTION_OF_TARGET = float(os.environ.get('POLE_TP_FRAC', '0.7'))         # take TP at 70% of distance to opposite pole
SWEEP_WICK_MULT       = float(os.environ.get('POLE_SWEEP_WICK_MULT', '2.0')) # wick ≥ 2× body for sweep — tighter
OB_MIN_DISPLACEMENT   = float(os.environ.get('POLE_OB_MIN_DISP', '0.005'))   # 0.5% next-bar move = displacement
LEVEL_COOLDOWN_S      = int(os.environ.get('POLE_LEVEL_COOLDOWN_S', '86400')) # 24h per-level cooldown
USE_1H_POLES          = int(os.environ.get('POLE_USE_1H', '0'))              # 1h poles too noisy in backtest — off by default
USE_SESSION_POLES     = int(os.environ.get('POLE_USE_SESSION', '0'))         # session/daily H/L — also noisy in chop, off by default
USE_EQUAL_POLES       = int(os.environ.get('POLE_USE_EQUAL', '0'))           # equal highs/lows — noisy on 1h, off
EQUAL_POLES_MIN_COUNT = int(os.environ.get('POLE_EQUAL_MIN_COUNT', '3'))     # 3+ matches for equal level (was 2)

# Stats
_STATS = {
    'eval_calls':       0,
    'fires':            0,
    'no_poles':         0,
    'no_sweep':         0,
    'no_target_pole':   0,
    'rr_too_low':       0,
    'on_cooldown':      0,
    'errors':           0,
}

# Per-coin: list of (pole_key, fired_ts) — for cooldown
_FIRED_POLES = {}  # coin -> [{key, ts}]


def status():
    n = max(1, _STATS['eval_calls'])
    return {**_STATS, 'success_rate_pct': round((1 - _STATS['errors']/n) * 100, 2)}


# ─── BAR HELPERS ──────────────────────────────────────────────────
def _body(b):
    return abs(b['c'] - b['o'])

def _upper_wick(b):
    return b['h'] - max(b['o'], b['c'])

def _lower_wick(b):
    return min(b['o'], b['c']) - b['l']


# ─── PIVOT DETECTION ──────────────────────────────────────────────
def _find_pivots(bars: List[Dict], lb: int = PIVOT_LB):
    """Return (pivot_highs, pivot_lows) as lists of dict {idx, t, price}.

    A pivot high at index i = bars[i].h is the max of bars[i-lb:i+lb+1].
    Same for pivot low.
    """
    highs, lows = [], []
    for i in range(lb, len(bars) - lb):
        window = bars[i-lb:i+lb+1]
        if bars[i]['h'] == max(b['h'] for b in window):
            highs.append({'idx': i, 't': bars[i]['t'], 'price': bars[i]['h']})
        if bars[i]['l'] == min(b['l'] for b in window):
            lows.append({'idx': i, 't': bars[i]['t'], 'price': bars[i]['l']})
    return highs, lows


# ─── OB DETECTION (proper SMC: last opposite candle before BOS) ──
def _detect_obs(bars: List[Dict], pivot_highs, pivot_lows, lookback: int = 100):
    """Return list of unmitigated OBs.

    Bullish OB: last DOWN candle before a sequence that breaks the prior pivot HIGH
                (BOS up). The OB zone = [low, high] of that down candle.
    Bearish OB: last UP candle before a sequence that breaks the prior pivot LOW
                (BOS down).

    Mitigation: any subsequent bar wicks INTO the OB body (price < OB.high for
    bullish, price > OB.low for bearish).

    Returns: [{type, top, bottom, formed_t, formed_idx, tf}, ...]
    """
    if len(bars) < 10:
        return []
    obs = []
    cs = bars[-lookback:]
    base_idx = max(0, len(bars) - lookback)

    # For each pivot high in lookback window: did price BREAK above it later?
    for ph in pivot_highs:
        if ph['idx'] < base_idx:
            continue
        local_i = ph['idx'] - base_idx
        # Look forward for first bar that closes above the pivot high
        for j in range(local_i + 1, len(cs)):
            if cs[j]['c'] > ph['price']:
                # BOS up. Find last DOWN candle BEFORE j that's at-or-below the pivot
                for k in range(j - 1, max(local_i - 1, -1), -1):
                    if cs[k]['c'] < cs[k]['o']:
                        # bullish OB candidate
                        ob_top = cs[k]['h']
                        ob_bot = cs[k]['l']
                        formed_t = cs[k]['t']
                        # Check mitigation: any LATER bar wick into OB?
                        mitigated = False
                        for m in range(k + 1, len(cs)):
                            if cs[m]['l'] <= ob_top:
                                # touched it
                                mitigated = True
                                break
                        if not mitigated:
                            obs.append({
                                'type': 'bullish',
                                'top': ob_top, 'bottom': ob_bot,
                                'formed_t': formed_t, 'formed_idx': base_idx + k,
                            })
                        break
                break

    # Inverse for bearish OBs
    for pl in pivot_lows:
        if pl['idx'] < base_idx:
            continue
        local_i = pl['idx'] - base_idx
        for j in range(local_i + 1, len(cs)):
            if cs[j]['c'] < pl['price']:
                for k in range(j - 1, max(local_i - 1, -1), -1):
                    if cs[k]['c'] > cs[k]['o']:
                        ob_top = cs[k]['h']
                        ob_bot = cs[k]['l']
                        formed_t = cs[k]['t']
                        mitigated = False
                        for m in range(k + 1, len(cs)):
                            if cs[m]['h'] >= ob_bot:
                                mitigated = True
                                break
                        if not mitigated:
                            obs.append({
                                'type': 'bearish',
                                'top': ob_top, 'bottom': ob_bot,
                                'formed_t': formed_t, 'formed_idx': base_idx + k,
                            })
                        break
                break

    return obs


# ─── FVG DETECTION ────────────────────────────────────────────────
def _detect_fvgs(bars: List[Dict], lookback: int = 100):
    """Find unmitigated FVGs (3-bar gaps).

    Bullish FVG: bars[i-1].h < bars[i+1].l. Zone = [bars[i-1].h, bars[i+1].l].
    Mitigated when subsequent bar wicks INTO the zone.
    """
    if len(bars) < 10:
        return []
    fvgs = []
    cs = bars[-lookback:]
    base_idx = max(0, len(bars) - lookback)
    for i in range(1, len(cs) - 1):
        prv, nxt = cs[i-1], cs[i+1]
        # Bullish gap
        if prv['h'] < nxt['l']:
            top, bot = nxt['l'], prv['h']
            mitigated = any(cs[m]['l'] <= top for m in range(i + 2, len(cs)))
            if not mitigated:
                fvgs.append({
                    'type': 'bullish', 'top': top, 'bottom': bot,
                    'formed_t': cs[i]['t'], 'formed_idx': base_idx + i,
                })
        # Bearish gap
        if prv['l'] > nxt['h']:
            top, bot = prv['l'], nxt['h']
            mitigated = any(cs[m]['h'] >= bot for m in range(i + 2, len(cs)))
            if not mitigated:
                fvgs.append({
                    'type': 'bearish', 'top': top, 'bottom': bot,
                    'formed_t': cs[i]['t'], 'formed_idx': base_idx + i,
                })
    return fvgs


# ─── EQUAL HIGHS / EQUAL LOWS ─────────────────────────────────────
def _detect_equal_levels(pivots: List[Dict], price_ref: float, side: str, tol_pct: float = EQUAL_TOL_PCT):
    """Group pivot highs (or lows) that are within tol_pct of each other = equal levels.

    side: 'high' or 'low'.
    Returns: [{price, count, last_t, kind}].
    """
    if not pivots:
        return []
    sorted_p = sorted(pivots, key=lambda p: p['price'], reverse=(side == 'high'))
    groups = []
    used = set()
    for i, p in enumerate(sorted_p):
        if i in used:
            continue
        cluster = [p]
        used.add(i)
        for j in range(i + 1, len(sorted_p)):
            if j in used:
                continue
            if abs(sorted_p[j]['price'] - p['price']) / p['price'] <= tol_pct:
                cluster.append(sorted_p[j])
                used.add(j)
        if len(cluster) >= 2:
            avg_px = sum(c['price'] for c in cluster) / len(cluster)
            last_t = max(c['t'] for c in cluster)
            groups.append({
                'price': avg_px, 'count': len(cluster), 'last_t': last_t,
                'kind': 'equal_highs' if side == 'high' else 'equal_lows',
            })
    return groups


# ─── PDH/PDL/PWH/PWL/SESSION ──────────────────────────────────────
def _session_levels(bars_1h: List[Dict], now_ts_ms: int):
    """Return prior day H/L, prior week H/L, current Asian H/L (if known).

    Crypto markets are 24/7; "session" boundaries are by UTC hour:
      Asian:  00:00 - 08:00 UTC
      London: 08:00 - 12:00 UTC
      NY:     12:00 - 21:00 UTC
    """
    if not bars_1h:
        return {}
    levels = {}
    now_s = now_ts_ms / 1000
    DAY = 86400
    today_utc_midnight = (int(now_s) // DAY) * DAY * 1000
    yesterday_start = today_utc_midnight - DAY * 1000
    week_start = today_utc_midnight - 7 * DAY * 1000

    yest_bars = [b for b in bars_1h if yesterday_start <= b['t'] < today_utc_midnight]
    if yest_bars:
        levels['PDH'] = max(b['h'] for b in yest_bars)
        levels['PDL'] = min(b['l'] for b in yest_bars)

    week_bars = [b for b in bars_1h if week_start <= b['t'] < today_utc_midnight]
    if week_bars:
        levels['PWH'] = max(b['h'] for b in week_bars)
        levels['PWL'] = min(b['l'] for b in week_bars)

    # Today's Asian session
    asian_start = today_utc_midnight
    asian_end = today_utc_midnight + 8 * 3600 * 1000
    if now_ts_ms >= asian_end:
        asian_bars = [b for b in bars_1h if asian_start <= b['t'] < asian_end]
        if asian_bars:
            levels['ASIA_H'] = max(b['h'] for b in asian_bars)
            levels['ASIA_L'] = min(b['l'] for b in asian_bars)

    return levels


# ─── BUILD POLE LIST ──────────────────────────────────────────────
def _build_poles(bars_15m, bars_1h, bars_4h, now_ts_ms):
    """Aggregate all unmitigated poles into a single list.

    Each pole: {kind, side ('above'|'below'), price (or zone), tf, weight, key}
    """
    poles = []

    # 4h structural — heaviest
    if bars_4h:
        ph4, pl4 = _find_pivots(bars_4h)
        for ob in _detect_obs(bars_4h, ph4, pl4):
            poles.append({
                'kind': 'OB', 'side': 'below' if ob['type'] == 'bullish' else 'above',
                'top': ob['top'], 'bottom': ob['bottom'],
                'price': (ob['top'] + ob['bottom']) / 2,
                'tf': '4h', 'weight': 5,
                'key': f"OB_4h_{ob['type']}_{int(ob['formed_t'])}",
                'mid': (ob['top'] + ob['bottom']) / 2,
            })
        for fvg in _detect_fvgs(bars_4h):
            poles.append({
                'kind': 'FVG', 'side': 'below' if fvg['type'] == 'bullish' else 'above',
                'top': fvg['top'], 'bottom': fvg['bottom'],
                'price': (fvg['top'] + fvg['bottom']) / 2,
                'tf': '4h', 'weight': 4,
                'key': f"FVG_4h_{fvg['type']}_{int(fvg['formed_t'])}",
                'mid': (fvg['top'] + fvg['bottom']) / 2,
            })

    # 1h structural — OFF by default (proven noisy in 30d backtest: 26.9% WR)
    if bars_1h and USE_1H_POLES:
        ph1, pl1 = _find_pivots(bars_1h)
        for ob in _detect_obs(bars_1h, ph1, pl1):
            poles.append({
                'kind': 'OB', 'side': 'below' if ob['type'] == 'bullish' else 'above',
                'top': ob['top'], 'bottom': ob['bottom'],
                'price': (ob['top'] + ob['bottom']) / 2,
                'tf': '1h', 'weight': 3,
                'key': f"OB_1h_{ob['type']}_{int(ob['formed_t'])}",
                'mid': (ob['top'] + ob['bottom']) / 2,
            })
        for fvg in _detect_fvgs(bars_1h):
            poles.append({
                'kind': 'FVG', 'side': 'below' if fvg['type'] == 'bullish' else 'above',
                'top': fvg['top'], 'bottom': fvg['bottom'],
                'price': (fvg['top'] + fvg['bottom']) / 2,
                'tf': '1h', 'weight': 2,
                'key': f"FVG_1h_{fvg['type']}_{int(fvg['formed_t'])}",
                'mid': (fvg['top'] + fvg['bottom']) / 2,
            })

    # Equal highs/lows — OFF by default (45 trades in backtest @ 17.8% WR for EQH)
    if bars_1h and USE_EQUAL_POLES:
        ph1, pl1 = _find_pivots(bars_1h)
        eq_highs = _detect_equal_levels(ph1, bars_1h[-1]['c'], 'high')
        eq_lows = _detect_equal_levels(pl1, bars_1h[-1]['c'], 'low')
        for eh in eq_highs:
            if eh['count'] < EQUAL_POLES_MIN_COUNT:
                continue
            poles.append({
                'kind': 'EQH', 'side': 'above', 'top': eh['price'], 'bottom': eh['price'],
                'price': eh['price'], 'tf': '1h', 'weight': 4,
                'key': f"EQH_1h_{int(eh['last_t'])}_{eh['count']}",
                'mid': eh['price'],
            })
        for el in eq_lows:
            if el['count'] < EQUAL_POLES_MIN_COUNT:
                continue
            poles.append({
                'kind': 'EQL', 'side': 'below', 'top': el['price'], 'bottom': el['price'],
                'price': el['price'], 'tf': '1h', 'weight': 4,
                'key': f"EQL_1h_{int(el['last_t'])}_{el['count']}",
                'mid': el['price'],
            })

    # Session/PDH/PDL — kept on; these were marginal in backtest but structurally meaningful
    if bars_1h and USE_SESSION_POLES:
        sess = _session_levels(bars_1h, now_ts_ms)
        for name, px in sess.items():
            side = 'above' if name.endswith('H') else 'below'
            poles.append({
                'kind': name, 'side': side, 'top': px, 'bottom': px,
                'price': px, 'tf': '1h', 'weight': 3,
                'key': f"{name}_{int(now_ts_ms // 86400000)}",
                'mid': px,
            })

    return poles


# ─── SWEEP DETECTION ──────────────────────────────────────────────
def _detect_sweep(bars_15m, poles, current_price):
    """On the most recent 15m bar (last completed), did price wick through any pole then close back inside?

    Returns (signal_dict, swept_pole) or (None, None).
    """
    if len(bars_15m) < 2:
        return None, None

    bar = bars_15m[-1]  # latest
    body = _body(bar)
    upper = _upper_wick(bar)
    lower = _lower_wick(bar)

    # For BUY: lower wick swept a "below" pole, candle closed back above the pole top
    for pole in poles:
        if pole['side'] != 'below':
            continue
        # Wick below pole's top, close above pole's top
        if bar['l'] <= pole['top'] and bar['c'] > pole['top']:
            # Sweep magnitude check
            if lower < SWEEP_WICK_MULT * max(body, 1e-9):
                continue
            return ({
                'side': 'BUY',
                'sweep_low': bar['l'],
                'sweep_close': bar['c'],
                'sweep_t': bar['t'],
            }, pole)

    # For SELL: upper wick swept an "above" pole, close back below
    for pole in poles:
        if pole['side'] != 'above':
            continue
        if bar['h'] >= pole['bottom'] and bar['c'] < pole['bottom']:
            if upper < SWEEP_WICK_MULT * max(body, 1e-9):
                continue
            return ({
                'side': 'SELL',
                'sweep_high': bar['h'],
                'sweep_close': bar['c'],
                'sweep_t': bar['t'],
            }, pole)

    return None, None


# ─── COOLDOWN ─────────────────────────────────────────────────────
def _is_cooled_down(coin: str, pole_key: str, now_ts: int):
    fired = _FIRED_POLES.get(coin, [])
    cutoff = now_ts - LEVEL_COOLDOWN_S
    fired = [f for f in fired if f['ts'] >= cutoff]
    _FIRED_POLES[coin] = fired
    return not any(f['key'] == pole_key for f in fired)


def _mark_fired(coin: str, pole_key: str, now_ts: int):
    _FIRED_POLES.setdefault(coin, []).append({'key': pole_key, 'ts': now_ts})


# ─── MAIN DETECT ──────────────────────────────────────────────────
def detect(coin: str, bars_15m: List[Dict], bars_1h: List[Dict],
           bars_4h: List[Dict], now_ts_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Detect pole-to-pole entry on coin. Returns signal dict or None.

    Signal: {side, entry, sl, tp, swept_pole, target_pole, rr, max_hold_s,
             confluences, source}
    """
    _STATS['eval_calls'] += 1
    try:
        if now_ts_ms is None:
            now_ts_ms = int(time.time() * 1000)
        now_ts = now_ts_ms // 1000

        if not bars_15m or len(bars_15m) < 2:
            return None

        current_px = bars_15m[-1]['c']

        # 1. Build the pole map
        poles = _build_poles(bars_15m, bars_1h, bars_4h, now_ts_ms)
        if not poles:
            _STATS['no_poles'] += 1
            return None

        # 2. Detect sweep on most recent 15m bar
        sweep, swept_pole = _detect_sweep(bars_15m, poles, current_px)
        if not sweep:
            _STATS['no_sweep'] += 1
            return None

        # 3. Cooldown — has THIS pole already fired recently?
        if not _is_cooled_down(coin, swept_pole['key'], now_ts):
            _STATS['on_cooldown'] += 1
            return None

        # 4. Find target pole on opposite side (closest unmitigated, not the swept one)
        side = sweep['side']
        target_side = 'above' if side == 'BUY' else 'below'
        candidates = [p for p in poles if p['side'] == target_side]
        if not candidates:
            _STATS['no_target_pole'] += 1
            return None

        if side == 'BUY':
            # Target = lowest "above" pole (closest)
            candidates = [p for p in candidates if p['mid'] > current_px]
            if not candidates:
                _STATS['no_target_pole'] += 1
                return None
            target = min(candidates, key=lambda p: p['mid'])
        else:
            candidates = [p for p in candidates if p['mid'] < current_px]
            if not candidates:
                _STATS['no_target_pole'] += 1
                return None
            target = max(candidates, key=lambda p: p['mid'])

        # 5. Compute SL, TP, R:R
        # TP = TP_FRACTION_OF_TARGET fraction of distance from entry to opposite pole's nearest edge
        if side == 'BUY':
            entry = sweep['sweep_close']
            sl = sweep['sweep_low'] * (1 - SL_BUFFER_PCT)
            target_edge = target['bottom']  # near edge of opposite zone
            tp = entry + (target_edge - entry) * TP_FRACTION_OF_TARGET
        else:
            entry = sweep['sweep_close']
            sl = sweep['sweep_high'] * (1 + SL_BUFFER_PCT)
            target_edge = target['top']
            tp = entry + (target_edge - entry) * TP_FRACTION_OF_TARGET

        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk <= 0:
            _STATS['errors'] += 1
            return None
        rr = reward / risk

        if rr < MIN_RR:
            _STATS['rr_too_low'] += 1
            return None
        if rr > MAX_RR:
            # Cap target at MAX_RR distance — opposite pole probably stale/broken
            if side == 'BUY':
                tp = entry + risk * MAX_RR
            else:
                tp = entry - risk * MAX_RR
            rr = MAX_RR

        # 6. Confluences — extra poles being swept simultaneously (size boost)
        confluences = []
        for p in poles:
            if p['key'] == swept_pole['key']:
                continue
            if side == 'BUY' and p['side'] == 'below' and bars_15m[-1]['l'] <= p['top']:
                confluences.append(f"{p['kind']}_{p['tf']}")
            if side == 'SELL' and p['side'] == 'above' and bars_15m[-1]['h'] >= p['bottom']:
                confluences.append(f"{p['kind']}_{p['tf']}")

        # 7. Mark fired (one fire per pole per cooldown window)
        _mark_fired(coin, swept_pole['key'], now_ts)
        _STATS['fires'] += 1

        return {
            'engine':       'pole',
            'side':         side,
            'entry':        round(entry, 8),
            'sl':           round(sl, 8),
            'tp':           round(tp, 8),
            'rr':           round(rr, 2),
            'max_hold_s':   MAX_HOLD_HOURS * 3600,
            'swept_pole':   {'kind': swept_pole['kind'], 'tf': swept_pole['tf'],
                             'price': swept_pole['price'], 'weight': swept_pole['weight']},
            'target_pole':  {'kind': target['kind'], 'tf': target['tf'],
                             'price': target['price'], 'weight': target['weight']},
            'confluences':  confluences,
            'sweep_t':      sweep['sweep_t'],
            'source':       'pole_engine',
        }

    except Exception as e:
        _STATS['errors'] += 1
        import traceback as _tb
        _tb.print_exc()
        return None


# ─── DEBUG/INSPECT — public utilities for shadow logging ──────────
def map_poles(coin: str, bars_15m, bars_1h, bars_4h, now_ts_ms=None):
    """Return the current pole map for a coin (for dashboard / debug)."""
    if now_ts_ms is None:
        now_ts_ms = int(time.time() * 1000)
    return _build_poles(bars_15m, bars_1h, bars_4h, now_ts_ms)


def reset_cooldowns():
    """Clear per-level cooldown cache (debug only)."""
    _FIRED_POLES.clear()
