"""Signal persistence gate: require same signal on 2 consecutive closed bars before firing.
Cuts fakeouts, estimated +5 WR points (needs live measurement).
"""
import time, threading

_LOCK = threading.Lock()
_LAST_SIGNAL = {}  # coin -> (side, bar_ts, timestamp_logged)

PERSISTENCE_WINDOW_SEC = 900  # last signal must be within 15min

def check(coin, side, bar_ts):
    """Returns True if this is the 2nd consecutive bar with same side. First bar returns False (staged)."""
    now = time.time()
    with _LOCK:
        prev = _LAST_SIGNAL.get(coin)
        _LAST_SIGNAL[coin] = (side, bar_ts, now)
    if not prev: return False
    prev_side, prev_ts, prev_logged = prev
    if side != prev_side: return False
    if bar_ts == prev_ts: return False  # same bar, not two consecutive
    if now - prev_logged > PERSISTENCE_WINDOW_SEC: return False  # too stale
    return True

def clear(coin):
    with _LOCK:
        _LAST_SIGNAL.pop(coin, None)
