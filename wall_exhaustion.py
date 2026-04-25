"""
wall_exhaustion.py — Detect walls that are FAILING, fade them.

EDGE
====
Most bots assume walls hold (mean-reversion at support/resistance).
The asymmetric play is detecting when a wall is about to FAIL — the
side that was leaning on it gets liquidated, price punches through,
and you're already positioned for the breakout.

DATA SOURCES (zero new feeds)
=============================
orderbook_ws._WALLS_HISTORY: deque of (ts, usd_size) per (coin, side, bucket)
sampled every 30s for last 10min. We compute:

  decay_pct = (peak_size_in_window - current_size) / peak_size_in_window

  decay_pct < 0.30  → wall STABLE  (holding)
  decay_pct < 0.55  → wall WEAKENING
  decay_pct >= 0.55 → wall EXHAUSTING (about to fail)

PROXIMITY GATE
==============
Exhaustion only matters when price is APPROACHING the wall — a wall
500bp away can shrink without affecting our trade. Require:
  distance_pct <= 0.5%  (within 50bp)
  approach_velocity > 0  (price moving toward wall)

SIGNAL DIRECTION
================
Counter to where the wall WAS holding price:
  bid wall (was support) exhausting → SELL (breakdown coming)
  ask wall (was resistance) exhausting → BUY (breakout coming)

Asymmetric vs wall_bounce: we fire BEFORE the wall fails (when it's
clearly weakening), not after the bounce. Wall_bounce trades the
"hold" pattern. wall_exhaustion trades the "fail" pattern.

PUBLIC API
==========
check(coin, current_px) -> (side: 'BUY'|'SELL'|None, context: dict)
status() -> diagnostics dict
"""

import os
import time
import threading

# Lazy import — orderbook_ws may not be ready at module import time
_ob_ws = None


def _get_ob_ws():
    global _ob_ws
    if _ob_ws is None:
        try:
            import orderbook_ws as _o
            _ob_ws = _o
        except Exception:
            _ob_ws = False
    return _ob_ws or None


# ─── Configuration (env-overridable) ─────────────────────────────────────
EXHAUSTION_THRESHOLD = float(os.environ.get('WALL_EXH_THRESHOLD', '0.55'))   # 55% decay
WEAKENING_THRESHOLD  = float(os.environ.get('WALL_EXH_WEAK_THRESHOLD', '0.30'))
PROXIMITY_PCT        = float(os.environ.get('WALL_EXH_PROXIMITY_PCT', '0.005'))  # 50bp
MIN_HISTORY_SAMPLES  = int(os.environ.get('WALL_EXH_MIN_SAMPLES', '4'))     # need 4 samples (2min)
MIN_PEAK_USD         = float(os.environ.get('WALL_EXH_MIN_PEAK_USD', '750000'))  # was a real wall
COOLDOWN_SEC         = int(os.environ.get('WALL_EXH_COOLDOWN_SEC', '300'))  # 5min/coin
APPROACH_LOOKBACK    = int(os.environ.get('WALL_EXH_APPROACH_LOOKBACK', '60'))  # 1min approach window

# ─── State ─────────────────────────────────────────────────────────────────
_LAST_FIRED = {}     # coin -> ts
_PRICE_HISTORY = {}  # coin -> [(ts, px), ...] for approach velocity calc
_LOCK = threading.Lock()

_STATS = {
    'check_calls':        0,
    'no_history':         0,
    'walls_evaluated':    0,
    'classified_stable':  0,
    'classified_weakening': 0,
    'classified_exhausting': 0,
    'fires':              0,
    'skipped_cooldown':   0,
    'skipped_proximity':  0,
    'skipped_no_approach': 0,
}


def _record_price(coin, px):
    """Track price for approach velocity computation."""
    if not px or px <= 0:
        return
    with _LOCK:
        h = _PRICE_HISTORY.setdefault(coin, [])
        h.append((time.time(), float(px)))
        # Prune older than lookback + 30s headroom
        cutoff = time.time() - APPROACH_LOOKBACK - 30
        _PRICE_HISTORY[coin] = [x for x in h if x[0] > cutoff]


def _approach_velocity(coin, current_px, wall_px, side):
    """Returns +1 if price approaching wall FROM CORRECT SIDE, 0 otherwise.

    Critical: side-aware. A 'bid' wall (support) can only be validly approached
    from ABOVE — both start and current price must be > wall_px. A price that
    crossed THROUGH the wall reads as 'approaching' if you use abs() distance,
    but it's already past the wall — that case must return 0.

    side: 'bid' or 'ask' (the side of the wall, not the trade direction)
    """
    with _LOCK:
        hist = list(_PRICE_HISTORY.get(coin, []))
    if len(hist) < 3:
        return 0
    cutoff = time.time() - APPROACH_LOOKBACK
    relevant = [(ts, p) for ts, p in hist if ts >= cutoff]
    if len(relevant) < 3:
        return 0
    earliest_px = relevant[0][1]

    # SIDE-AWARENESS GUARD
    # bid wall = support (below mid). Valid approach: price coming DOWN from above.
    #   Both endpoints must be ABOVE wall_px. If current is below, price already broke through.
    # ask wall = resistance (above mid). Valid approach: price going UP from below.
    #   Both endpoints must be BELOW wall_px.
    if side == 'bid':
        if current_px <= wall_px or earliest_px <= wall_px:
            return 0
    else:  # 'ask'
        if current_px >= wall_px or earliest_px >= wall_px:
            return 0

    earliest_dist = abs(earliest_px - wall_px)
    current_dist = abs(current_px - wall_px)
    if current_dist < earliest_dist * 0.7:   # closed 30%+ of distance
        return 1
    if current_dist > earliest_dist * 1.3:
        return -1
    return 0


def _decay_pct(history_deque):
    """Returns decay_pct in [0, 1]. 0 = stable, 1 = fully decayed.
    Uses peak size in window vs current size (last sample)."""
    if not history_deque or len(history_deque) < MIN_HISTORY_SAMPLES:
        return 0.0
    sizes = [usd for ts, usd in history_deque]
    peak = max(sizes)
    if peak < MIN_PEAK_USD:
        return 0.0  # never a real wall
    current = sizes[-1]
    if peak <= 0:
        return 0.0
    decay = (peak - current) / peak
    return max(0.0, min(1.0, decay))


def _classify(decay):
    if decay >= EXHAUSTION_THRESHOLD:
        return 'EXHAUSTING'
    if decay >= WEAKENING_THRESHOLD:
        return 'WEAKENING'
    return 'STABLE'


def check(coin, current_px):
    """Evaluate walls for `coin`. If any verified wall is EXHAUSTING and price
    is approaching, return (side, context). Else (None, None).

    side: 'BUY'  → ask wall (was resistance) exhausting → breakout long
    side: 'SELL' → bid wall (was support) exhausting → breakdown short
    """
    _STATS['check_calls'] += 1

    if not current_px or current_px <= 0:
        return None, None

    ob = _get_ob_ws()
    if ob is None:
        return None, None

    _record_price(coin, current_px)

    # Cooldown
    if time.time() - _LAST_FIRED.get(coin, 0) < COOLDOWN_SEC:
        _STATS['skipped_cooldown'] += 1
        return None, None

    # Walk verified walls; check each for exhaustion + proximity
    try:
        verified = ob.get_walls(coin) or []
    except Exception:
        return None, None

    if not verified:
        _STATS['no_history'] += 1
        return None, None

    # Iterate walls; require we know the history deque key to compute decay.
    # _WALLS_HISTORY is keyed by (coin, side, bucket_pct) where bucket is
    # the 0.1% distance bucket from mid at sample time. We can re-derive the
    # bucket from the wall's current 'distance_pct' field.
    try:
        history_dict = ob._WALLS_HISTORY
        lock = ob._LOCK
    except Exception:
        return None, None

    candidates = []  # (decay_pct, side_str, wall_dict)
    with lock:
        for w in verified:
            _STATS['walls_evaluated'] += 1
            side = w['side']  # 'bid' or 'ask'
            wall_px = w['price']
            usd = w.get('usd', 0)
            if usd < MIN_PEAK_USD:
                continue
            # Proximity gate
            dist_pct = abs(current_px - wall_px) / current_px
            if dist_pct > PROXIMITY_PCT:
                _STATS['skipped_proximity'] += 1
                continue
            # Recover bucket from distance_pct field stored at detection
            bucket = w.get('distance_pct')
            if bucket is None:
                continue
            key = (coin.upper(), side, bucket)
            hist = history_dict.get(key)
            if not hist:
                continue
            decay = _decay_pct(hist)
            cls = _classify(decay)
            if cls == 'STABLE':
                _STATS['classified_stable'] += 1
            elif cls == 'WEAKENING':
                _STATS['classified_weakening'] += 1
            elif cls == 'EXHAUSTING':
                _STATS['classified_exhausting'] += 1
                candidates.append((decay, side, w, dist_pct))

    if not candidates:
        return None, None

    # Most-decayed wall wins
    candidates.sort(key=lambda x: x[0], reverse=True)
    decay, side, wall, dist_pct = candidates[0]

    # Require approach: price must be moving TOWARD the wall FROM CORRECT SIDE.
    # Side-aware velocity guards against price that already crossed through.
    velocity = _approach_velocity(coin, current_px, wall['price'], side)
    if velocity != 1:
        _STATS['skipped_no_approach'] += 1
        return None, None

    # Direction: counter to where wall was holding price
    # bid wall (support) exhausting + price approaching from above
    #   = price falling toward broken support → SELL
    # ask wall (resistance) exhausting + price approaching from below
    #   = price rising toward broken resistance → BUY
    trade_side = 'SELL' if side == 'bid' else 'BUY'

    _LAST_FIRED[coin] = time.time()
    _STATS['fires'] += 1

    ctx = {
        'wall_side':       side,
        'wall_price':      wall['price'],
        'wall_usd':        wall['usd'],
        'decay_pct':       round(decay * 100, 1),
        'distance_pct':    round(dist_pct * 100, 3),
        'classification':  'EXHAUSTING',
        'velocity':        velocity,
    }
    return trade_side, ctx


def status():
    """Diagnostics for /health."""
    out = dict(_STATS)
    out['exhaustion_threshold_pct'] = EXHAUSTION_THRESHOLD * 100
    out['weakening_threshold_pct'] = WEAKENING_THRESHOLD * 100
    out['proximity_pct'] = PROXIMITY_PCT * 100
    out['min_peak_usd'] = MIN_PEAK_USD
    out['cooldown_sec'] = COOLDOWN_SEC
    out['tracked_coins'] = len(_PRICE_HISTORY)
    return out
