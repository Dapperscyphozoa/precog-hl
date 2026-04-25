"""
wall_absorption.py — Pre-reaction liquidity absorption at statistical extremes.

EDGE
====
Fills the gap in the wall stack:

  Engine            | Trigger timing
  ------------------|--------------------
  wall_exhaustion   | AFTER liquidity starts failing  (breakout)
  wall_bounce       | AFTER reaction confirms (retest after pullback)
  funding_engine    | When funding shows crowded positioning
  wall_absorption   | BEFORE reaction — first touch at BB extreme

Pattern: price reaches a Bollinger band extreme AND a verified multi-venue
wall sits at that extreme AND the wall is stable (not weakening). Trade
the bounce — fade the move back toward the mid-band.

This monetizes the regime where wall_exhaustion produces zero fires:
tight chop with stable walls. wall_exhaustion fires when walls FAIL;
this fires when walls HOLD.

DATA SOURCES (zero new feeds)
=============================
- orderbook_ws._VERIFIED_WALLS:  multi-venue $500k+ walls (existing)
- orderbook_ws._WALLS_HISTORY:   wall size deque for stability scoring
- okx_fetch.fetch_klines:        15m candles for BB calculation
- funding_arb._CACHE['hl']:      cross-engine overlap guard

GATES (all must pass)
=====================
1. regime == 'chop'  (passed in by caller)
2. WALL_ABSORB_ENABLED=1  (default disabled)
3. coin not on cooldown
4. funding overlap guard: |HL funding/hr| < threshold
5. cap on concurrent absorption positions
6. verified wall present, stable (decay < 30%), within 50bp of price
7. price at BB extreme (within 0.15% of upper/lower band)
8. price approaching wall from correct side
9. wall side aligns with band side (bid wall + lower band, ask wall + upper)

DIRECTION
=========
- bid wall + price at lower BB → BUY (long the bounce off support)
- ask wall + price at upper BB → SELL (short the rejection at resistance)

Note: wall_exhaustion's directions are INVERTED relative to this:
- wall_exhaustion bid+exhausting → SELL (breakdown short)
- wall_absorption bid+stable    → BUY (bounce long)

The two engines are mutually exclusive on wall classification (STABLE vs
EXHAUSTING) so they cannot fire together on the same wall.

PUBLIC API
==========
check(coin, current_px, regime, active_absorption_count) -> (side, ctx) | (None, None)
status() -> diagnostics dict
"""

import os
import time
import threading
import statistics

# Lazy imports — modules may not be ready at import time
_ob_ws = None
_okx = None
_funding_arb = None


def _get_ob_ws():
    global _ob_ws
    if _ob_ws is None:
        try:
            import orderbook_ws as _o
            _ob_ws = _o
        except Exception:
            _ob_ws = False
    return _ob_ws or None


def _get_okx():
    global _okx
    if _okx is None:
        try:
            import okx_fetch as _o
            _okx = _o
        except Exception:
            _okx = False
    return _okx or None


def _get_funding_arb():
    global _funding_arb
    if _funding_arb is None:
        try:
            import funding_arb as _f
            _funding_arb = _f
        except Exception:
            _funding_arb = False
    return _funding_arb or None


# ─── Configuration ───────────────────────────────────────────────────────
ENABLED              = os.environ.get('WALL_ABSORB_ENABLED', '0') == '1'
PROXIMITY_PCT        = float(os.environ.get('WALL_ABSORB_PROXIMITY_PCT', '0.005'))    # 50bp
BB_EXTREME_PCT       = float(os.environ.get('WALL_ABSORB_BB_EXTREME_PCT', '0.0015'))  # 15bp from band
MIN_PEAK_USD         = float(os.environ.get('WALL_ABSORB_MIN_PEAK_USD', '750000'))
COOLDOWN_SEC         = int(os.environ.get('WALL_ABSORB_COOLDOWN_SEC', '600'))         # 10min/coin
MAX_CONCURRENT       = int(os.environ.get('WALL_ABSORB_MAX_CONCURRENT', '3'))
APPROACH_LOOKBACK    = int(os.environ.get('WALL_ABSORB_APPROACH_LOOKBACK', '60'))
STABLE_DECAY_THRESH  = float(os.environ.get('WALL_ABSORB_STABLE_DECAY_PCT', '0.30'))  # < 30% decay = STABLE
BB_LENGTH            = int(os.environ.get('WALL_ABSORB_BB_LENGTH', '20'))
BB_STDEV             = float(os.environ.get('WALL_ABSORB_BB_STDEV', '2.0'))
BB_CACHE_TTL_SEC     = int(os.environ.get('WALL_ABSORB_BB_CACHE_TTL_SEC', '840'))     # 14min (one 15m bar)
FUNDING_GUARD_HR_PCT = float(os.environ.get('WALL_ABSORB_FUNDING_GUARD_HR_PCT', '0.00005'))  # 0.005%/hr

# ─── State ─────────────────────────────────────────────────────────────────
_LAST_FIRED = {}        # coin -> ts
_PRICE_HISTORY = {}     # coin -> [(ts, px), ...] for approach velocity
_BB_CACHE = {}          # coin -> {'ts','lower','upper','mid'}
_LOCK = threading.Lock()

_STATS = {
    'check_calls':           0,
    'disabled':              0,
    'wrong_regime':          0,
    'on_cooldown':           0,
    'capacity_full':         0,
    'no_walls':              0,
    'wall_below_min_usd':    0,
    'skipped_proximity':     0,
    'wall_not_stable':       0,
    'no_history':            0,
    'no_bb_data':            0,
    'not_at_bb_extreme':     0,
    'side_mismatch':         0,
    'skipped_no_approach':   0,
    'funding_overlap':       0,
    'fires':                 0,
}


# ─── Helpers ─────────────────────────────────────────────────────────────

def _record_price(coin, px):
    if not px or px <= 0:
        return
    with _LOCK:
        h = _PRICE_HISTORY.setdefault(coin, [])
        h.append((time.time(), float(px)))
        cutoff = time.time() - APPROACH_LOOKBACK - 30
        _PRICE_HISTORY[coin] = [x for x in h if x[0] > cutoff]


def _approach_velocity(coin, current_px, wall_px, side):
    """Side-aware approach detector — same pattern as wall_exhaustion."""
    with _LOCK:
        hist = list(_PRICE_HISTORY.get(coin, []))
    if len(hist) < 3:
        return 0
    cutoff = time.time() - APPROACH_LOOKBACK
    relevant = [(ts, p) for ts, p in hist if ts >= cutoff]
    if len(relevant) < 3:
        return 0
    earliest_px = relevant[0][1]
    # Side guards: bid wall valid only when both endpoints ABOVE wall_px;
    # ask wall valid only when both BELOW.
    if side == 'bid':
        if current_px <= wall_px or earliest_px <= wall_px:
            return 0
    else:
        if current_px >= wall_px or earliest_px >= wall_px:
            return 0
    earliest_dist = abs(earliest_px - wall_px)
    current_dist = abs(current_px - wall_px)
    if current_dist < earliest_dist * 0.7:
        return 1
    return 0


def _decay_pct(history_deque):
    """0..1 decay from peak. Mirrors wall_exhaustion._decay_pct."""
    if not history_deque or len(history_deque) < 4:
        return 0.0
    sizes = [usd for ts, usd in history_deque]
    peak = max(sizes)
    if peak < MIN_PEAK_USD:
        return 0.0
    current = sizes[-1]
    if peak <= 0:
        return 0.0
    return max(0.0, min(1.0, (peak - current) / peak))


def _compute_bb(coin):
    """Compute Bollinger Bands(20, 2) on 15m closes. Cached BB_CACHE_TTL_SEC.
    Returns dict {'lower','upper','mid'} or None on fetch failure."""
    now = time.time()
    with _LOCK:
        cached = _BB_CACHE.get(coin)
        if cached and now - cached['ts'] < BB_CACHE_TTL_SEC:
            return cached
    okx = _get_okx()
    if okx is None:
        return None
    try:
        bars = okx.fetch_klines(coin, '15m', BB_LENGTH + 5)
    except Exception:
        return None
    if not bars or len(bars) < BB_LENGTH:
        return None
    closes = [b['c'] for b in bars[-BB_LENGTH:]]
    mid = sum(closes) / len(closes)
    try:
        sd = statistics.pstdev(closes)
    except Exception:
        return None
    if sd <= 0:
        return None
    bb = {
        'ts':    now,
        'mid':   mid,
        'upper': mid + BB_STDEV * sd,
        'lower': mid - BB_STDEV * sd,
        'width': (BB_STDEV * sd) / mid if mid > 0 else 0,
    }
    with _LOCK:
        _BB_CACHE[coin] = bb
    return bb


def _funding_overlap(coin):
    """True if HL funding is even mildly extreme on this coin — defer to funding_engine."""
    fa = _get_funding_arb()
    if fa is None:
        return False
    try:
        with fa._LOCK:
            r = fa._CACHE['hl'].get(coin)
    except Exception:
        return False
    if r is None:
        return False
    return abs(float(r)) > FUNDING_GUARD_HR_PCT


# ─── Main check ──────────────────────────────────────────────────────────

def check(coin, current_px, regime='unknown', active_absorption_count=0):
    """Returns (side: 'BUY'|'SELL', context: dict) on fire, else (None, None).

    Args:
      coin: HL coin name (canonical case, e.g. 'BTC', 'kBONK')
      current_px: live mid price
      regime: regime_detector output ('chop','bull-calm','bear-calm','storm')
      active_absorption_count: number of currently-open absorption positions
    """
    _STATS['check_calls'] += 1

    if not ENABLED:
        _STATS['disabled'] += 1
        return None, None

    if regime != 'chop':
        _STATS['wrong_regime'] += 1
        return None, None

    if not current_px or current_px <= 0:
        return None, None

    _record_price(coin, current_px)

    if time.time() - _LAST_FIRED.get(coin, 0) < COOLDOWN_SEC:
        _STATS['on_cooldown'] += 1
        return None, None

    if active_absorption_count >= MAX_CONCURRENT:
        _STATS['capacity_full'] += 1
        return None, None

    # Funding overlap guard — defer to funding_engine when it would fire too
    if _funding_overlap(coin):
        _STATS['funding_overlap'] += 1
        return None, None

    ob = _get_ob_ws()
    if ob is None:
        return None, None

    # Pull verified walls
    try:
        verified = ob.get_walls(coin) or []
    except Exception:
        return None, None
    if not verified:
        _STATS['no_walls'] += 1
        return None, None

    # BB calc
    bb = _compute_bb(coin)
    if not bb:
        _STATS['no_bb_data'] += 1
        return None, None

    # Are we at a BB extreme?
    at_lower = current_px <= bb['lower'] * (1 + BB_EXTREME_PCT)
    at_upper = current_px >= bb['upper'] * (1 - BB_EXTREME_PCT)
    if not (at_lower or at_upper):
        _STATS['not_at_bb_extreme'] += 1
        return None, None

    # Iterate walls — find one that matches the BB side and is stable
    try:
        history_dict = ob._WALLS_HISTORY
        ob_lock = ob._LOCK
    except Exception:
        return None, None

    candidates = []  # (decay, side, wall_dict, dist_pct)
    with ob_lock:
        for w in verified:
            side = w['side']
            wall_px = w['price']
            usd = w.get('usd', 0)
            if usd < MIN_PEAK_USD:
                _STATS['wall_below_min_usd'] += 1
                continue
            # Side / band match: bid wall must align with lower BB; ask wall with upper BB
            if at_lower and side != 'bid':
                continue
            if at_upper and side != 'ask':
                continue
            if (at_lower and not at_upper and side != 'bid') or \
               (at_upper and not at_lower and side != 'ask'):
                _STATS['side_mismatch'] += 1
                continue
            # Proximity
            dist_pct = abs(current_px - wall_px) / current_px
            if dist_pct > PROXIMITY_PCT:
                _STATS['skipped_proximity'] += 1
                continue
            # Stability check via decay
            bucket = w.get('distance_pct')
            if bucket is None:
                continue
            # NB: orderbook_ws keys with original coin casing
            key = (coin, side, bucket)
            hist = history_dict.get(key)
            if not hist:
                _STATS['no_history'] += 1
                continue
            decay = _decay_pct(hist)
            if decay >= STABLE_DECAY_THRESH:
                _STATS['wall_not_stable'] += 1
                continue
            candidates.append((decay, side, w, dist_pct))

    if not candidates:
        return None, None

    # Most-stable wall wins (lowest decay)
    candidates.sort(key=lambda x: x[0])
    decay, side, wall, dist_pct = candidates[0]

    # Approach gate (side-aware) — must be moving toward the wall from correct side
    if _approach_velocity(coin, current_px, wall['price'], side) != 1:
        _STATS['skipped_no_approach'] += 1
        return None, None

    # Direction: FADE the move — bounce off wall back toward mid
    # bid wall (support at lower BB) → BUY
    # ask wall (resistance at upper BB) → SELL
    trade_side = 'BUY' if side == 'bid' else 'SELL'

    _LAST_FIRED[coin] = time.time()
    _STATS['fires'] += 1

    ctx = {
        'wall_side':       side,
        'wall_price':      wall['price'],
        'wall_usd':        wall['usd'],
        'wall_decay_pct':  round(decay * 100, 1),
        'distance_pct':    round(dist_pct * 100, 3),
        'bb_position':     'LOWER' if at_lower else 'UPPER',
        'bb_lower':        round(bb['lower'], 6),
        'bb_upper':        round(bb['upper'], 6),
        'bb_mid':          round(bb['mid'], 6),
        'classification':  'STABLE',
    }
    return trade_side, ctx


def status():
    out = dict(_STATS)
    out.update({
        'enabled':                ENABLED,
        'proximity_pct':          PROXIMITY_PCT * 100,
        'bb_extreme_pct':         BB_EXTREME_PCT * 100,
        'min_peak_usd':           MIN_PEAK_USD,
        'cooldown_sec':           COOLDOWN_SEC,
        'max_concurrent':         MAX_CONCURRENT,
        'stable_decay_threshold': STABLE_DECAY_THRESH * 100,
        'funding_guard_hr_pct':   FUNDING_GUARD_HR_PCT * 100,
        'tracked_coins':          len(_PRICE_HISTORY),
        'bb_cache_size':          len(_BB_CACHE),
    })
    return out
