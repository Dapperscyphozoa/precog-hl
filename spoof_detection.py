"""Spoof detection. Wall disappears >50% on approach = fake wall (liquidity trap).
Signal: fade the trap direction. Wall withdrew = price will continue through.
"""
import time, threading
import orderbook_ws

SPOOF_APPROACH_PCT = 0.003   # price within 0.3% of wall
SPOOF_WITHDRAW_PCT = 0.5      # wall size drops >50%
SPOOF_WINDOW_SEC = 30         # detection window
COOLDOWN_SEC = 300

_TRACK = {}       # coin_sideprice -> {initial_usd, ts}
_SPOOFS = {}      # coin -> {ts, direction, original_wall}
_LAST_FIRED = {}
_LOCK = threading.Lock()

def _key(coin, side, price):
    return f"{coin}_{side}_{price:.6f}"

def scan_walls(coin, current_px):
    """Call per tick. Track approach + withdrawal of walls near current price."""
    if not current_px: return
    try:
        walls = orderbook_ws.get_walls(coin)
    except Exception:
        return
    now = time.time()
    with _LOCK:
        for w in walls:
            if w['distance_pct'] > SPOOF_APPROACH_PCT: continue
            k = _key(coin, w['side'], w['price'])
            prev = _TRACK.get(k)
            if not prev:
                _TRACK[k] = {'initial_usd': w['usd'], 'ts': now, 'price': w['price'], 'side': w['side']}
                continue
            if now - prev['ts'] > SPOOF_WINDOW_SEC:
                _TRACK[k] = {'initial_usd': w['usd'], 'ts': now, 'price': w['price'], 'side': w['side']}
                continue
            shrinkage = 1 - (w['usd'] / max(prev['initial_usd'], 1))
            if shrinkage > SPOOF_WITHDRAW_PCT:
                # Wall spoofed — bid wall withdrawal = bearish (support gone), ask = bullish
                direction = 'SELL' if w['side'] == 'bid' else 'BUY'
                _SPOOFS[coin] = {'ts': now, 'direction': direction,
                                 'original_wall': prev['initial_usd'], 'remaining': w['usd']}

def get_spoof_signal(coin, max_age_sec=120):
    with _LOCK:
        s = _SPOOFS.get(coin)
    if not s: return None
    if time.time() - s['ts'] > max_age_sec: return None
    if time.time() - _LAST_FIRED.get(coin, 0) < COOLDOWN_SEC: return None
    return s

def mark_fired(coin):
    with _LOCK:
        _LAST_FIRED[coin] = time.time()

def status():
    with _LOCK:
        recent = sum(1 for s in _SPOOFS.values() if time.time() - s['ts'] < 300)
    return {'tracked_walls': len(_TRACK), 'recent_spoofs': recent}
