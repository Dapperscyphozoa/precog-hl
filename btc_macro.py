"""BTC macro-structure awareness for cross-asset position management.

Reads BTC verified walls from orderbook_ws (6-venue depth aggregated) and
exposes proximity / breakthrough / rejection state. Used by precog entry
gate, position lifecycle, and confluence engine to react to BTC structural
levels (sell walls = alt-wide BUY headwind, buy walls = SELL headwind).

Distinct from btc_correlation.py which uses TREND only (EMA20 dist).
This module uses STRUCTURE (verified walls = stacked liquidity that
historically rejects price). Together they form macro context.

Public API:
    near_resistance(distance_pct=0.005) -> (bool, wall_dict_or_none)
    near_support(distance_pct=0.005) -> (bool, wall_dict_or_none)
    wall_broken(direction) -> bool   # 'up' or 'down'
    wall_rejected(direction) -> bool  # bounce off wall
    status() -> dict for /health
"""
import time
import threading

# State: track recent BTC mid prices to detect break/reject patterns
_PRICE_HISTORY = []  # list of (ts, mid)
_PRICE_HISTORY_MAX = 60  # ~30min at 30s sample
_LAST_NEAR_WALL = {'side': None, 'price': None, 'first_ts': 0}
_BROKEN_EVENTS = []  # list of (ts, direction, wall_price)
_REJECTED_EVENTS = []  # list of (ts, direction, wall_price)
_LOCK = threading.Lock()
_LAST_REFRESH_TS = 0
_REFRESH_INTERVAL = 30.0  # cache wall reads for 30s

# Configurable thresholds (env-tunable via precog/confluence callers)
RESISTANCE_PROXIMITY_PCT = 0.005    # 0.5% near sell wall
SUPPORT_PROXIMITY_PCT = 0.005       # 0.5% near buy wall
BREAKTHROUGH_PCT = 0.003            # 0.3% past wall = "broken"
REJECTION_PCT = 0.005               # 0.5% away from wall after touch = "rejected"
MIN_WALL_USD = 1_000_000            # $1M+ aggregate to count as "major"


def _btc_mid():
    """Get current BTC mid from orderbook_ws's depth feed."""
    try:
        import orderbook_ws as _ob
        with _ob._LOCK:
            d = _ob._DEPTH.get('BTC', {})
            return float(d.get('mid', 0) or 0)
    except Exception:
        return 0.0


def _btc_walls():
    """Get verified BTC walls. Filtered to MIN_WALL_USD threshold."""
    try:
        import orderbook_ws as _ob
        walls = _ob.get_walls('BTC') or []
        return [w for w in walls if w.get('usd', 0) >= MIN_WALL_USD]
    except Exception:
        return []


def _refresh_state():
    """Sample BTC mid + walls. Track break/reject events. Throttled to 30s."""
    global _LAST_REFRESH_TS
    now = time.time()
    if now - _LAST_REFRESH_TS < _REFRESH_INTERVAL:
        return
    _LAST_REFRESH_TS = now
    mid = _btc_mid()
    if mid <= 0:
        return
    walls = _btc_walls()

    with _LOCK:
        _PRICE_HISTORY.append((now, mid))
        if len(_PRICE_HISTORY) > _PRICE_HISTORY_MAX:
            _PRICE_HISTORY.pop(0)

        # Detect break/reject relative to last-seen near wall
        if _LAST_NEAR_WALL.get('price'):
            wp = _LAST_NEAR_WALL['price']
            wside = _LAST_NEAR_WALL['side']
            if wside == 'ask':  # sell wall
                if mid > wp * (1 + BREAKTHROUGH_PCT):
                    _BROKEN_EVENTS.append((now, 'up', wp))
                    _LAST_NEAR_WALL.update({'side': None, 'price': None, 'first_ts': 0})
                elif mid < wp * (1 - REJECTION_PCT):
                    _REJECTED_EVENTS.append((now, 'down', wp))
                    _LAST_NEAR_WALL.update({'side': None, 'price': None, 'first_ts': 0})
            elif wside == 'bid':  # buy wall
                if mid < wp * (1 - BREAKTHROUGH_PCT):
                    _BROKEN_EVENTS.append((now, 'down', wp))
                    _LAST_NEAR_WALL.update({'side': None, 'price': None, 'first_ts': 0})
                elif mid > wp * (1 + REJECTION_PCT):
                    _REJECTED_EVENTS.append((now, 'up', wp))
                    _LAST_NEAR_WALL.update({'side': None, 'price': None, 'first_ts': 0})

        # Update near-wall snapshot if currently within proximity of one
        nearest_ask = nearest_bid = None
        for w in walls:
            side = w.get('side')
            price = w.get('price', 0)
            if price <= 0:
                continue
            dist = abs(price - mid) / mid
            if side == 'ask' and price > mid and dist <= RESISTANCE_PROXIMITY_PCT:
                if nearest_ask is None or dist < abs(nearest_ask['price'] - mid) / mid:
                    nearest_ask = w
            if side == 'bid' and price < mid and dist <= SUPPORT_PROXIMITY_PCT:
                if nearest_bid is None or dist < abs(nearest_bid['price'] - mid) / mid:
                    nearest_bid = w

        # Track which wall we're "at" (one at a time — closer one wins)
        cur = None
        if nearest_ask and nearest_bid:
            d_ask = abs(nearest_ask['price'] - mid)
            d_bid = abs(nearest_bid['price'] - mid)
            cur = nearest_ask if d_ask < d_bid else nearest_bid
        elif nearest_ask:
            cur = nearest_ask
        elif nearest_bid:
            cur = nearest_bid

        if cur:
            if (_LAST_NEAR_WALL.get('side') != cur['side']
                or abs((_LAST_NEAR_WALL.get('price') or 0) - cur['price']) / max(cur['price'], 1) > 0.001):
                _LAST_NEAR_WALL.update({
                    'side': cur['side'],
                    'price': cur['price'],
                    'first_ts': now,
                    'usd': cur['usd'],
                })

        # Prune old events (>1h)
        cutoff = now - 3600
        while _BROKEN_EVENTS and _BROKEN_EVENTS[0][0] < cutoff:
            _BROKEN_EVENTS.pop(0)
        while _REJECTED_EVENTS and _REJECTED_EVENTS[0][0] < cutoff:
            _REJECTED_EVENTS.pop(0)


def near_resistance(distance_pct=None):
    """Return (True, wall_dict) if BTC within distance_pct of a verified
    sell wall. False, None otherwise. distance_pct overrides default."""
    _refresh_state()
    mid = _btc_mid()
    if mid <= 0:
        return False, None
    threshold = distance_pct if distance_pct is not None else RESISTANCE_PROXIMITY_PCT
    walls = _btc_walls()
    nearest = None
    for w in walls:
        if w.get('side') != 'ask':
            continue
        price = w.get('price', 0)
        if price <= mid:
            continue
        dist = (price - mid) / mid
        if dist <= threshold:
            if nearest is None or dist < (nearest['price'] - mid) / mid:
                nearest = w
    return (nearest is not None), nearest


def near_support(distance_pct=None):
    """Return (True, wall_dict) if BTC within distance_pct of a verified
    buy wall. False, None otherwise."""
    _refresh_state()
    mid = _btc_mid()
    if mid <= 0:
        return False, None
    threshold = distance_pct if distance_pct is not None else SUPPORT_PROXIMITY_PCT
    walls = _btc_walls()
    nearest = None
    for w in walls:
        if w.get('side') != 'bid':
            continue
        price = w.get('price', 0)
        if price >= mid:
            continue
        dist = (mid - price) / mid
        if dist <= threshold:
            if nearest is None or dist < (mid - nearest['price']) / mid:
                nearest = w
    return (nearest is not None), nearest


def wall_broken(direction, lookback_sec=600):
    """Return True if a wall in `direction` ('up'/'down') was broken in
    the last `lookback_sec` (default 10min)."""
    _refresh_state()
    cutoff = time.time() - lookback_sec
    with _LOCK:
        return any(d == direction and ts >= cutoff
                   for ts, d, _ in _BROKEN_EVENTS)


def wall_rejected(direction, lookback_sec=600):
    """Return True if a wall was rejected in `direction` in last lookback."""
    _refresh_state()
    cutoff = time.time() - lookback_sec
    with _LOCK:
        return any(d == direction and ts >= cutoff
                   for ts, d, _ in _REJECTED_EVENTS)


def near_wall_summary():
    """One-call combined state for entry gates / pos mgmt callers."""
    _refresh_state()
    near_res, res_wall = near_resistance()
    near_sup, sup_wall = near_support()
    return {
        'mid': _btc_mid(),
        'near_resistance': near_res,
        'resistance_wall': res_wall,
        'near_support': near_sup,
        'support_wall': sup_wall,
        'recent_break_up': wall_broken('up'),
        'recent_break_down': wall_broken('down'),
        'recent_reject_up': wall_rejected('up'),
        'recent_reject_down': wall_rejected('down'),
    }


def status():
    """Status for /health endpoint."""
    _refresh_state()
    mid = _btc_mid()
    walls = _btc_walls()
    near_res, res_wall = near_resistance()
    near_sup, sup_wall = near_support()
    return {
        'btc_mid': round(mid, 2) if mid else 0,
        'verified_walls_count': len(walls),
        'near_resistance': near_res,
        'resistance_distance_pct': (round((res_wall['price'] - mid) / mid * 100, 3)
                                    if (res_wall and mid > 0) else None),
        'resistance_wall_usd': res_wall.get('usd') if res_wall else None,
        'near_support': near_sup,
        'support_distance_pct': (round((mid - sup_wall['price']) / mid * 100, 3)
                                 if (sup_wall and mid > 0) else None),
        'support_wall_usd': sup_wall.get('usd') if sup_wall else None,
        'broken_events_1h': len(_BROKEN_EVENTS),
        'rejected_events_1h': len(_REJECTED_EVENTS),
        'last_refresh_age_sec': round(time.time() - _LAST_REFRESH_TS, 1),
    }
