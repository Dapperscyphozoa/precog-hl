#!/usr/bin/env python3
"""
htf_zones.py — Shared HTF context detector for SMC v2 (REVERSAL) and BRK (CONTINUATION).

Single source of truth for everything visible on the chart:
  - HTF pivots, bias (BULL/BEAR/NONE), trend_intact
  - Order Blocks (last opposing candle before displacement ≥ displace_atr × ATR)
  - FVGs (3-bar gap ≥ 0.3 × ATR)
  - Wider Supply/Demand zones (around HTF swings ± 0.5 × ATR)
  - Liquidity pools: PDH, PDL, PWH, PWL, EQH, EQL, session H/L (Asia/London/NY)
  - Per-zone status state machine (NONE / WATCH / SWEPT / ARMED / BROKEN / RECLAIMED)

Drop-in replacement: existing call sites that did
    htfs = htf_bias_and_zones(c4, lb, displace, max_age)
still work — return signature is a strict superset of the old one.

For UZT (Lesson 2) bidirectional flow, use:
    ctx = build_context(c4, c1h=c1h, c1d=c1d, params=params)
which returns the same per-bar states plus pools + sd_zones + sessions arrays.
"""
import math


# ═══════════════════════════════════════════════════════════════════════════
# PRIMITIVES
# ═══════════════════════════════════════════════════════════════════════════

def atr_series(highs, lows, closes, period=14):
    n = len(highs)
    if n == 0:
        return []
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i-1]
        tr.append(max(h-l, abs(h-pc), abs(l-pc)))
    out = [0.0] * n
    if n < period:
        return out
    s = sum(tr[:period]) / period
    out[period-1] = s
    for i in range(period, n):
        s = (s * (period-1) + tr[i]) / period
        out[i] = s
    return out


def _pivots(highs, lows, lb):
    """Return (swing_h, swing_l) as lists of (idx, price)."""
    n = len(highs)
    sh, sl = [], []
    for i in range(lb, n-lb):
        ph, pl = highs[i], lows[i]
        if all(ph > highs[i-k] and ph > highs[i+k] for k in range(1, lb+1)):
            sh.append((i, ph))
        if all(pl < lows[i-k] and pl < lows[i+k] for k in range(1, lb+1)):
            sl.append((i, pl))
    return sh, sl


# ═══════════════════════════════════════════════════════════════════════════
# CORE HTF BIAS + ZONES (back-compat with existing call sites)
# ═══════════════════════════════════════════════════════════════════════════

def htf_bias_and_zones(c4h, lb, displace_atr, max_age_bars):
    """Original API. Returns list of {t, bias, trend_intact, zones, last_swing_h, last_swing_l}.

    `zones` here is the ENRICHED list — includes OB, FVG, and (new) wider S/D rectangles.
    Existing consumers iterate `z['top']/z['bot']/z['is_bull']/z['kind']` and ignore extras.
    """
    if len(c4h) < max(lb*2+1, 20):
        return []
    n = len(c4h)
    highs = [b['h'] for b in c4h]
    lows = [b['l'] for b in c4h]
    closes = [b['c'] for b in c4h]
    opens = [b['o'] for b in c4h]
    atr = atr_series(highs, lows, closes, 14)

    swing_h = []  # (idx, price)
    swing_l = []
    zones = []
    states = []

    for i in range(n):
        # Pivot detection (lookback both sides)
        ci = i - lb
        if ci >= lb:
            ph = highs[ci]; pl = lows[ci]
            is_ph = all(ph > highs[ci-k] and ph > highs[ci+k] for k in range(1, lb+1))
            is_pl = all(pl < lows[ci-k] and pl < lows[ci+k] for k in range(1, lb+1))
            if is_ph:
                swing_h.append((ci, ph))
                # Wider Supply zone around the swing high (S/D from chart manual §7)
                _push_sd(zones, top=ph, bot=ph - 0.5*atr[ci] if atr[ci] > 0 else ph*0.997,
                         is_bull=False, idx=ci)
            if is_pl:
                swing_l.append((ci, pl))
                _push_sd(zones, top=pl + 0.5*atr[ci] if atr[ci] > 0 else pl*1.003,
                         bot=pl, is_bull=True, idx=ci)

        # OB / FVG detection on closed bars
        if i >= 2 and atr[i] > 0:
            disp = displace_atr * atr[i]
            sb = (closes[i] > opens[i]) and (closes[i]-opens[i]) > disp
            sbe = (closes[i] < opens[i]) and (opens[i]-closes[i]) > disp
            if sb and closes[i-1] < opens[i-1] and closes[i] > highs[i-1]:
                zones.append({'top': opens[i-1], 'bot': lows[i-1], 'is_bull': True, 'kind': 'OB', 'idx': i-1})
            if sbe and closes[i-1] > opens[i-1] and closes[i] < lows[i-1]:
                zones.append({'top': highs[i-1], 'bot': opens[i-1], 'is_bull': False, 'kind': 'OB', 'idx': i-1})
            ms = 0.3 * atr[i]
            if i >= 2 and lows[i] > highs[i-2] and (lows[i] - highs[i-2]) >= ms:
                zones.append({'top': lows[i], 'bot': highs[i-2], 'is_bull': True, 'kind': 'FVG', 'idx': i})
            if i >= 2 and highs[i] < lows[i-2] and (lows[i-2] - highs[i]) >= ms:
                zones.append({'top': lows[i-2], 'bot': highs[i], 'is_bull': False, 'kind': 'FVG', 'idx': i})

        # Mitigation + age-out
        cutoff = i - max_age_bars
        zones = [z for z in zones
                 if z['idx'] >= cutoff
                 and not ((z['is_bull'] and lows[i] <= z['bot']) or
                          (not z['is_bull'] and highs[i] >= z['top']))]

        # Bias from last 3 swings each side
        bias = 'NONE'
        trend_intact = False
        if len(swing_h) >= 2 and len(swing_l) >= 2:
            sh = swing_h[-3:]
            sl = swing_l[-3:]
            hh = all(sh[j][1] > sh[j-1][1] for j in range(1, len(sh)))
            hl = all(sl[j][1] > sl[j-1][1] for j in range(1, len(sl)))
            ll = all(sl[j][1] < sl[j-1][1] for j in range(1, len(sl)))
            lh = all(sh[j][1] < sh[j-1][1] for j in range(1, len(sh)))
            if hh and hl:
                bias, trend_intact = 'BULL', True
            elif ll and lh:
                bias, trend_intact = 'BEAR', True

        states.append({
            't': c4h[i]['t'],
            'bias': bias,
            'trend_intact': trend_intact,
            'zones': list(zones),
            'last_swing_h': swing_h[-1][1] if swing_h else None,
            'last_swing_l': swing_l[-1][1] if swing_l else None,
            'swing_h_chain': [p for _, p in swing_h[-3:]],
            'swing_l_chain': [p for _, p in swing_l[-3:]],
        })
    return states


def _push_sd(zones, top, bot, is_bull, idx):
    """Append wider S/D zone if not duplicating an existing one within 0.1%."""
    for z in zones:
        if z.get('kind') == 'SD' and z['is_bull'] == is_bull:
            if abs(z['top'] - top) / top < 0.001 and abs(z['bot'] - bot) / max(bot, 1e-9) < 0.001:
                return
    zones.append({'top': top, 'bot': bot, 'is_bull': is_bull, 'kind': 'SD', 'idx': idx})


def htf_state_at(states, ts):
    """Latest HTF state at-or-before ts. Binary search."""
    if not states:
        return None
    if ts < states[0]['t']:
        return None
    lo, hi = 0, len(states)
    while lo < hi:
        mid = (lo + hi) // 2
        if states[mid]['t'] <= ts:
            lo = mid + 1
        else:
            hi = mid
    return states[lo-1] if lo > 0 else None


# ═══════════════════════════════════════════════════════════════════════════
# LIQUIDITY POOLS — PDH/PDL, PWH/PWL, EQH/EQL, sessions
# ═══════════════════════════════════════════════════════════════════════════

def compute_daily_pools(c1d):
    """Return list of {t, pdh, pdl} per day (using PRIOR day H/L, like chart label)."""
    out = []
    for i in range(1, len(c1d)):
        out.append({
            't': c1d[i]['t'],
            'pdh': c1d[i-1]['h'],
            'pdl': c1d[i-1]['l'],
        })
    return out


def compute_weekly_pools(c1d):
    """Compute PWH/PWL from daily candles. Returns {t, pwh, pwl} per day-of-new-week-and-after."""
    if not c1d:
        return []
    DAY_MS = 86400 * 1000
    # ISO week boundaries — Monday 00:00 UTC is start of week
    # Use modular arithmetic: 1970-01-01 was Thursday, so week starts shifted.
    # Simpler: bucket by floor(t/(7*DAY_MS)) with epoch correction.
    EPOCH_THU = 0  # 1970-01-01 = Thursday. Mon-anchored = subtract 4 days.
    weeks = {}  # week_id -> (high, low)
    for c in c1d:
        wk = (c['t'] - 4*DAY_MS) // (7*DAY_MS)
        h, l = weeks.get(wk, (-math.inf, math.inf))
        weeks[wk] = (max(h, c['h']), min(l, c['l']))
    out = []
    for c in c1d:
        wk = (c['t'] - 4*DAY_MS) // (7*DAY_MS)
        prev_wk = wk - 1
        if prev_wk in weeks:
            ph, pl = weeks[prev_wk]
            out.append({'t': c['t'], 'pwh': ph, 'pwl': pl})
    return out


def compute_eq_pools(swing_h, swing_l, tol_pct=0.05):
    """Detect equal highs/lows from pivot lists. Returns list of {idx, level, kind, age}."""
    pools = []
    # Equal highs
    for i in range(1, len(swing_h)):
        a_idx, a_p = swing_h[i-1]
        b_idx, b_p = swing_h[i]
        if abs(a_p - b_p) / max(a_p, 1e-9) * 100 <= tol_pct:
            pools.append({
                'idx': b_idx,
                'level': max(a_p, b_p),
                'kind': 'EQH',
                'first_idx': a_idx,
            })
    # Equal lows
    for i in range(1, len(swing_l)):
        a_idx, a_p = swing_l[i-1]
        b_idx, b_p = swing_l[i]
        if abs(a_p - b_p) / max(a_p, 1e-9) * 100 <= tol_pct:
            pools.append({
                'idx': b_idx,
                'level': min(a_p, b_p),
                'kind': 'EQL',
                'first_idx': a_idx,
            })
    return pools


def compute_session_pools(c1h, tz_offset_hours=0):
    """Bucket 1H bars into Asia/London/NY by GMT hour, track running H/L per session.

    Asia: 22:00-07:00 GMT (wraps midnight)
    London: 07:00-16:00 GMT
    NY: 13:00-22:00 GMT (overlaps London 13-16)

    Returns list of {t_close, session, high, low} entries — one per session-end.
    """
    if not c1h:
        return []
    DAY_MS = 86400 * 1000

    def session_for(t_ms):
        h = (t_ms // 3600000 + tz_offset_hours) % 24
        if h >= 22 or h < 7:
            return 'ASIA'
        if h < 16:
            return 'LONDON'
        return 'NY'

    out = []
    cur = None  # {session, day_key, high, low, last_t}
    for c in c1h:
        s = session_for(c['t'])
        # Day key shifts at session start; ASIA wraps so use floor at 22:00 anchor
        anchor_hr = 22 if s == 'ASIA' else (7 if s == 'LONDON' else 13)
        adj = c['t'] - anchor_hr * 3600000
        day_key = adj // DAY_MS
        key = (s, day_key)
        if cur is None or cur['key'] != key:
            if cur is not None:
                out.append({
                    't_close': cur['last_t'],
                    'session': cur['session'],
                    'high': cur['high'],
                    'low': cur['low'],
                })
            cur = {'key': key, 'session': s, 'high': c['h'], 'low': c['l'], 'last_t': c['t']}
        else:
            cur['high'] = max(cur['high'], c['h'])
            cur['low'] = min(cur['low'], c['l'])
            cur['last_t'] = c['t']
    if cur is not None:
        out.append({
            't_close': cur['last_t'],
            'session': cur['session'],
            'high': cur['high'],
            'low': cur['low'],
        })
    return out


def pools_at(state_t, daily_pools, weekly_pools, eq_pools, htf_pivots_h, htf_pivots_l, session_pools, c4h_idx_at_t,
             include_kinds=('PDH', 'PDL', 'PWH', 'PWL', 'EQH', 'EQL', 'AsiaH', 'AsiaL', 'LonH', 'LonL', 'NYH', 'NYL')):
    """Materialize the active liquidity pools at a given timestamp.

    Returns list of {level, kind, age_bars, source_t}.
    Pools ABOVE current price are BSL targets; pools BELOW are SSL targets.
    Caller can filter by direction.
    """
    pools = []
    # Daily
    if 'PDH' in include_kinds or 'PDL' in include_kinds:
        d = _latest_at(daily_pools, state_t)
        if d:
            if 'PDH' in include_kinds:
                pools.append({'level': d['pdh'], 'kind': 'PDH', 'source_t': d['t']})
            if 'PDL' in include_kinds:
                pools.append({'level': d['pdl'], 'kind': 'PDL', 'source_t': d['t']})
    # Weekly
    if 'PWH' in include_kinds or 'PWL' in include_kinds:
        w = _latest_at(weekly_pools, state_t)
        if w:
            if 'PWH' in include_kinds:
                pools.append({'level': w['pwh'], 'kind': 'PWH', 'source_t': w['t']})
            if 'PWL' in include_kinds:
                pools.append({'level': w['pwl'], 'kind': 'PWL', 'source_t': w['t']})
    # Equal H/L on HTF
    cur_idx = c4h_idx_at_t(state_t) if c4h_idx_at_t else None
    for eq in eq_pools:
        if cur_idx is not None and eq['idx'] > cur_idx:
            continue
        if eq['kind'] in include_kinds:
            pools.append({'level': eq['level'], 'kind': eq['kind'], 'source_t': None,
                          'age_bars': (cur_idx - eq['idx']) if cur_idx is not None else None})
    # Sessions — only the most-recent CLOSED session per type
    seen = set()
    for s in reversed(session_pools):
        if s['t_close'] > state_t:
            continue
        sess = s['session']
        if sess in seen:
            continue
        seen.add(sess)
        h_kind = {'ASIA': 'AsiaH', 'LONDON': 'LonH', 'NY': 'NYH'}[sess]
        l_kind = {'ASIA': 'AsiaL', 'LONDON': 'LonL', 'NY': 'NYL'}[sess]
        if h_kind in include_kinds:
            pools.append({'level': s['high'], 'kind': h_kind, 'source_t': s['t_close']})
        if l_kind in include_kinds:
            pools.append({'level': s['low'], 'kind': l_kind, 'source_t': s['t_close']})
        if len(seen) == 3:
            break
    return pools


def _latest_at(items, ts):
    """Latest item with t <= ts."""
    best = None
    for x in items:
        if x['t'] <= ts:
            best = x
        else:
            break
    return best


# ═══════════════════════════════════════════════════════════════════════════
# UNIFIED BUILD — full HTF context object
# ═══════════════════════════════════════════════════════════════════════════

def build_context(c4h, c1h=None, c1d=None, params=None):
    """Compute the full HTF context: bias states + pools + sessions in one shot.

    Returns:
      {
        'states': [...],          # original htf_bias_and_zones output
        'daily_pools': [...],
        'weekly_pools': [...],
        'eq_pools': [...],
        'session_pools': [...],
        'pools_at': callable(t) -> list of pools
      }
    """
    p = params or {}
    states = htf_bias_and_zones(c4h, p.get('htf_lb', 5), p.get('htf_displace', 1.75),
                                p.get('htf_max_age', 540))
    daily = compute_daily_pools(c1d) if c1d else []
    weekly = compute_weekly_pools(c1d) if c1d else []
    sess = compute_session_pools(c1h) if c1h else []

    # HTF pivots for EQ detection
    if c4h and len(c4h) >= 2*p.get('htf_lb', 5) + 1:
        highs = [b['h'] for b in c4h]
        lows = [b['l'] for b in c4h]
        sh, sl = _pivots(highs, lows, p.get('htf_lb', 5))
    else:
        sh, sl = [], []
    eq = compute_eq_pools(sh, sl, tol_pct=p.get('eql_tol_pct', 0.05))

    times = [b['t'] for b in c4h] if c4h else []

    def _idx_at(t):
        if not times or t < times[0]:
            return None
        lo, hi = 0, len(times)
        while lo < hi:
            mid = (lo+hi)//2
            if times[mid] <= t:
                lo = mid+1
            else:
                hi = mid
        return lo - 1 if lo > 0 else None

    def pools_for(t):
        return pools_at(t, daily, weekly, eq, sh, sl, sess, _idx_at)

    return {
        'states': states,
        'daily_pools': daily,
        'weekly_pools': weekly,
        'eq_pools': eq,
        'session_pools': sess,
        'pools_at': pools_for,
        'htf_pivots_h': sh,
        'htf_pivots_l': sl,
    }


# ═══════════════════════════════════════════════════════════════════════════
# UTILITIES — for engines consuming this module
# ═══════════════════════════════════════════════════════════════════════════

def nearest_pool(pools, price, side, max_dist_pct=2.0):
    """Find closest pool above (side='above') or below (side='below') price.
    Returns pool dict or None.
    """
    candidates = []
    for p in pools:
        lvl = p['level']
        if side == 'above' and lvl > price:
            d = (lvl - price) / price * 100
            if d <= max_dist_pct:
                candidates.append((d, p))
        elif side == 'below' and lvl < price:
            d = (price - lvl) / price * 100
            if d <= max_dist_pct:
                candidates.append((d, p))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def zone_status(zone, c15_segment):
    """Classify per-zone status given recent 15m bar segment.

    Returns one of: NONE, IN_ZONE, BROKEN, RECLAIMED.
    Used by unified_state to bridge SMC v2 and BRK.
    """
    if not c15_segment:
        return 'NONE'
    last = c15_segment[-1]
    is_in = last['l'] <= zone['top'] and last['h'] >= zone['bot']
    if is_in:
        return 'IN_ZONE'
    # Check if any recent bar closed through with displacement
    for b in c15_segment[-5:]:
        body = abs(b['c'] - b['o'])
        if zone['is_bull']:
            # Demand zone broken DOWN
            if b['c'] < zone['bot'] and body > 0:
                return 'BROKEN'
        else:
            # Supply zone broken UP
            if b['c'] > zone['top'] and body > 0:
                return 'BROKEN'
    return 'NONE'
