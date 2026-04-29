"""ASIAN_SESSION_OPEN — calendar-driven directional bias engine.

Edge: 00:00 UTC and 08:00 UTC have measurable directional bias on top alts
as Asian and EU sessions open. Trade the first 5min impulse on whichever
direction sets in first 90s.

Mechanism:
- At 00:00 UTC and 08:00 UTC, watch for 90s
- Compute direction from first 90s of price action (sign of close - open)
- If |first 90s move| > 0.10%, fire trade in that direction
- 5-min timer exit
- TP 0.4%, SL 0.5%
- Run on top 20 alts by HL OI

API:
  poll_and_signal(coin, candles_1m) -> dict | None
    candles_1m: list of 1-min OHLCV bars, ascending, latest = last
    Returns signal dict {side, entry_px, tp_pct, sl_pct, max_hold_s, trigger_ts}
    or None.

  status() -> dict for /asian_session_status

Tunables (env):
  ASIAN_SESSION_ENABLED         default 1
  ASIAN_SESSION_HOURS           default '0,8' (UTC hours)
  ASIAN_SESSION_TRIGGER_PCT     default 0.0010 (0.10% min impulse)
  ASIAN_SESSION_TP_PCT          default 0.004
  ASIAN_SESSION_SL_PCT          default 0.005
  ASIAN_SESSION_HOLD_S          default 300 (5 min)
  ASIAN_SESSION_WINDOW_S        default 90  (first N seconds of session)

Fire-once-per-session-per-coin: tracks which (coin, session_hour, date)
have already fired to avoid duplicate fires within the same session.
"""
import os
import time
import threading
from datetime import datetime, timezone

ENABLED          = os.environ.get('ASIAN_SESSION_ENABLED', '1') == '1'
TRIGGER_PCT      = float(os.environ.get('ASIAN_SESSION_TRIGGER_PCT', '0.0010'))
TP_PCT           = float(os.environ.get('ASIAN_SESSION_TP_PCT', '0.004'))
SL_PCT           = float(os.environ.get('ASIAN_SESSION_SL_PCT', '0.005'))
MAX_HOLD_S       = int(os.environ.get('ASIAN_SESSION_HOLD_S', '300'))
WINDOW_S         = int(os.environ.get('ASIAN_SESSION_WINDOW_S', '90'))
SESSION_HOURS    = {int(h.strip()) for h in
                    os.environ.get('ASIAN_SESSION_HOURS', '0,8').split(',')
                    if h.strip()}

_LOCK = threading.Lock()
_FIRED = {}   # (coin, session_hour, date_str) -> ts (idempotency)
_STATS = {
    'checks':         0,
    'in_session':     0,
    'in_window':      0,
    'fired':          0,
    'below_trigger':  0,
}


def _is_in_session_window(now_ts=None):
    """Return (in_session: bool, session_hour: int, secs_since_open: int).
    in_session = True only during the first WINDOW_S seconds after 00:00 or 08:00 UTC."""
    now_ts = now_ts or time.time()
    dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    h = dt.hour
    if h not in SESSION_HOURS:
        return False, h, 0
    secs_since_open = dt.minute * 60 + dt.second
    if secs_since_open <= WINDOW_S:
        return True, h, secs_since_open
    return False, h, secs_since_open


def poll_and_signal(coin, candles_1m, now_ts=None):
    """Detect Asian session impulse on a coin.

    Args:
      coin: symbol
      candles_1m: list of 1m OHLCV bars (ascending). Last bar = current.
      now_ts: current ts (for testing)

    Returns: signal dict or None.
    """
    _STATS['checks'] += 1
    if not ENABLED:
        return None
    in_session, h, secs = _is_in_session_window(now_ts)
    if not in_session:
        return None
    _STATS['in_session'] += 1
    # Need at least 2 bars to measure direction
    if not candles_1m or len(candles_1m) < 2:
        return None
    # Idempotency: fire once per (coin, hour, date)
    now = now_ts or time.time()
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    date_str = dt.strftime('%Y-%m-%d')
    key = (coin, h, date_str)
    with _LOCK:
        if key in _FIRED:
            return None
    _STATS['in_window'] += 1
    # Compute first-window move: from session-open candle to current
    # Session open is at h:00:00. The bar containing that timestamp is the
    # "open candle." Find it by ts.
    open_ts = int(now) - secs  # secs since session open
    first_bar = None
    for b in candles_1m:
        try:
            bar_t = int(b.get('t', 0)) // 1000  # ms → s
        except Exception:
            continue
        if bar_t >= open_ts - 60 and bar_t <= open_ts + 60:
            first_bar = b
            break
    if first_bar is None:
        return None
    try:
        open_px = float(first_bar.get('o', 0) or 0)
        cur_px = float(candles_1m[-1].get('c', 0) or 0)
    except Exception:
        return None
    if open_px <= 0 or cur_px <= 0:
        return None
    move_pct = (cur_px - open_px) / open_px
    if abs(move_pct) < TRIGGER_PCT:
        _STATS['below_trigger'] += 1
        return None
    side = 'BUY' if move_pct > 0 else 'SELL'
    # Mark fired
    with _LOCK:
        _FIRED[key] = int(now)
        # Trim old entries (keep last 7 days)
        cutoff_ts = int(now) - 7 * 86400
        for k in list(_FIRED.keys()):
            if _FIRED[k] < cutoff_ts:
                del _FIRED[k]
    _STATS['fired'] += 1
    return {
        'engine': 'ASIAN_SESSION',
        'coin': coin,
        'side': side,
        'entry_px': cur_px,
        'tp_pct': TP_PCT,
        'sl_pct': SL_PCT,
        'max_hold_s': MAX_HOLD_S,
        'trigger_ts': int(now),
        'session_hour': h,
        'window_s': secs,
        'move_pct': move_pct,
    }


def status():
    """For /asian_session_status endpoint."""
    with _LOCK:
        fired_recent = list(_FIRED.items())[-20:]
    in_session, h, secs = _is_in_session_window()
    return {
        'enabled': ENABLED,
        'session_hours': sorted(SESSION_HOURS),
        'trigger_pct': TRIGGER_PCT,
        'tp_pct': TP_PCT,
        'sl_pct': SL_PCT,
        'max_hold_s': MAX_HOLD_S,
        'window_s': WINDOW_S,
        'in_session_now': in_session,
        'current_hour': h,
        'secs_since_open': secs if in_session else None,
        'fired_recent': [{'coin': k[0], 'hour': k[1], 'date': k[2], 'ts': v}
                         for k, v in fired_recent],
        'stats': dict(_STATS),
    }
