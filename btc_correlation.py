"""BTC correlation guard. When BTC moves >1% in 15min, confirm alt trades align with BTC direction.
Prevents correlated alt-blowup clusters (10 alts dumping same hour while BTC pumps).
"""
import time, threading
import bybit_ws

WINDOW_SEC = 900  # 15min
MOVE_THRESHOLD = 0.01  # 1%
_CACHE = {'ts': 0, 'btc_dir': 0, 'btc_move': 0}
_LOCK = threading.Lock()

def _refresh():
    try:
        candles = bybit_ws.get_candles('BTC', limit=5)
        if len(candles) < 3: return
        # Most recent close vs 15min ago
        latest = candles[-1][4]
        old = candles[-3][4]  # 3 bars back on 5m = 15min
        move = (latest - old) / old if old > 0 else 0
        direction = 1 if move > MOVE_THRESHOLD else (-1 if move < -MOVE_THRESHOLD else 0)
        with _LOCK:
            _CACHE['ts'] = time.time()
            _CACHE['btc_dir'] = direction
            _CACHE['btc_move'] = move
    except Exception:
        pass

def allow_alt_trade(coin, side):
    """When BTC moving hard, alt must align. Major coins exempt (BTC, ETH)."""
    if coin in ('BTC', 'ETH'): return True
    now = time.time()
    with _LOCK:
        stale = now - _CACHE['ts'] > 60
        direction = _CACHE['btc_dir']
    if stale: _refresh()
    with _LOCK:
        direction = _CACHE['btc_dir']
    if direction == 0: return True  # BTC not moving → allow any alt direction
    want_dir = 1 if side == 'BUY' else -1
    return want_dir == direction

def get_state():
    with _LOCK:
        return dict(_CACHE)
