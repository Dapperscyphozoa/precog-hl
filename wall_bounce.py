"""Wall-bounce retest entry engine. Third signal source.
Entry: verified wall holds → price pulls away ≥0.3% → returns within ±0.1% → fire aligned with V3.
SL: tight behind wall (wall fails = exit fast). TP: opposite-side wall or 1.5x wall-distance.

Env-tunable for chop regime: in low-vol chop, the default 0.3% pullback in 5min
rarely triggers. Operators can relax via WALL_BNC_PULL_MIN (default 0.003) and
WALL_BNC_PULL_LOOKBACK_SEC (default 300). For chop: 0.0015 + 600 fires more.
"""
import os, time, threading
import orderbook_ws

RETEST_PROXIMITY = float(os.environ.get('WALL_BNC_RETEST_PROXIMITY', '0.001'))   # within 0.1% of wall
PULL_MIN = float(os.environ.get('WALL_BNC_PULL_MIN', '0.003'))                   # ≥0.3% pull-away
PULL_LOOKBACK_SEC = int(os.environ.get('WALL_BNC_PULL_LOOKBACK_SEC', '300'))     # 5min default
MIN_WALL_USD = float(os.environ.get('WALL_BNC_MIN_WALL_USD', '750000'))
COOLDOWN_SEC = int(os.environ.get('WALL_BNC_COOLDOWN_SEC', '600'))               # 10min/coin

_LAST_FIRED = {}           # coin -> ts
_PULL_HISTORY = {}         # coin -> [(ts, px, wall_price, wall_side)]
_LOCK = threading.Lock()

def _record_price(coin, px):
    """Track price for pullback detection."""
    with _LOCK:
        h = _PULL_HISTORY.setdefault(coin, [])
        h.append((time.time(), px))
        # Prune
        cutoff = time.time() - PULL_LOOKBACK_SEC - 60
        _PULL_HISTORY[coin] = [x for x in h if x[0] > cutoff]

def check(coin, current_px, v3_direction):
    """Returns ('BUY'|'SELL', wall_dict) if retest entry fires. v3_direction: +1 up, -1 dn, 0 neutral."""
    if not current_px or current_px <= 0:
        return None, None
    _record_price(coin, current_px)
    # Cooldown
    if time.time() - _LAST_FIRED.get(coin, 0) < COOLDOWN_SEC:
        return None, None
    # Check both sides for eligible walls
    for wall_side, trade_side, dir_req in [('bid', 'BUY', 1), ('ask', 'SELL', -1)]:
        try:
            wall = orderbook_ws.get_nearest_wall(coin, wall_side)
        except Exception:
            continue
        if not wall or wall.get('usd', 0) < MIN_WALL_USD:
            continue
        # V3 must align (skip counter-trend retests)
        if v3_direction != 0 and v3_direction != dir_req:
            continue
        wp = wall['price']
        # Retest proximity
        dist = abs(current_px - wp) / current_px
        if dist > RETEST_PROXIMITY:
            continue
        # Did price pull away ≥PULL_MIN from this wall within lookback?
        with _LOCK:
            hist = list(_PULL_HISTORY.get(coin, []))
        now = time.time()
        pulled = False
        for ts, hpx in hist:
            if now - ts > PULL_LOOKBACK_SEC:
                continue
            away = abs(hpx - wp) / wp
            if away >= PULL_MIN:
                # Direction check: pull must have been AWAY from wall in the right direction
                if wall_side == 'bid' and hpx > wp * (1 + PULL_MIN):
                    pulled = True; break
                if wall_side == 'ask' and hpx < wp * (1 - PULL_MIN):
                    pulled = True; break
        if not pulled:
            continue
        _LAST_FIRED[coin] = now
        return trade_side, wall
    return None, None

def wall_broken(coin, side, original_wall_price, current_px):
    """Check if wall that supported/resisted our trade has been eaten through.
    side: 'BUY' (we held above bid wall) or 'SELL' (we held below ask wall).
    Returns True if wall invalidated → exit immediately.
    """
    if not current_px or not original_wall_price: return False
    try:
        wall = orderbook_ws.get_nearest_wall(coin, 'bid' if side == 'BUY' else 'ask')
    except Exception:
        return False
    # If wall gone from tracked verified list OR moved >0.5% from original = broken
    if not wall: return True
    moved = abs(wall['price'] - original_wall_price) / original_wall_price
    if moved > 0.005: return True
    # Price has crossed through original wall level
    if side == 'BUY' and current_px < original_wall_price * 0.998: return True
    if side == 'SELL' and current_px > original_wall_price * 1.002: return True
    return False
