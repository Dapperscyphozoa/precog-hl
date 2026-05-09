"""
risk_caps.py — Two daily/correlation safety caps.

1. Daily loss kill switch:
   Track equity at UTC 00:00 each day. If current equity drops by >= 5%
   from that anchor, halt all entries until the next UTC midnight.

2. Correlation cap (rolling window):
   Limit the number of NEW armed positions opened in any rolling 60-minute
   window to 3. Prevents whole-portfolio exposure to a single BTC dump
   that triggers wicks across 5+ coins simultaneously.

Both caps return (allow: bool, reason: str). Called from smc_engine gate
sequence before submission.
"""
import time
import threading
import logging
from collections import deque
import datetime as dt

log = logging.getLogger(__name__)

DAILY_DD_PCT = 5.0        # halt at -5% from UTC midnight equity
CORR_WINDOW_SEC = 60 * 60
CORR_MAX_OPENS = 3

_state = {
    'utc_anchor_day': None,      # date object
    'utc_anchor_equity': None,
    'halted_for_day': False,
}
_recent_opens = deque()         # timestamps (s)
_lock = threading.Lock()


def _utc_today():
    return dt.datetime.now(dt.timezone.utc).date()


def update_anchor(equity):
    """Call this whenever a fresh equity reading is available. Auto-resets
    at UTC midnight."""
    if equity is None:
        return
    today = _utc_today()
    with _lock:
        if _state['utc_anchor_day'] != today:
            _state['utc_anchor_day'] = today
            _state['utc_anchor_equity'] = float(equity)
            _state['halted_for_day'] = False
            log.info(f"risk_caps: new UTC day {today}, anchor equity=${equity:.2f}")
        elif _state['utc_anchor_equity'] is None:
            _state['utc_anchor_equity'] = float(equity)


def daily_dd_check(current_equity):
    """Return (allow, reason). False = halt entries."""
    if current_equity is None:
        return True, ''
    update_anchor(current_equity)
    with _lock:
        anchor = _state['utc_anchor_equity']
        if anchor is None or anchor <= 0:
            return True, ''
        dd_pct = (anchor - current_equity) / anchor * 100
        if _state['halted_for_day']:
            return False, f'daily_loss_halt dd={dd_pct:.2f}% (halted)'
        if dd_pct >= DAILY_DD_PCT:
            _state['halted_for_day'] = True
            log.warning(f"risk_caps: DAILY LOSS HALT triggered. dd={dd_pct:.2f}% anchor=${anchor:.2f} current=${current_equity:.2f}")
            return False, f'daily_loss_halt dd={dd_pct:.2f}%'
        return True, ''


def corr_check():
    """Return (allow, reason). Limits new opens to N per rolling window."""
    now = time.time()
    with _lock:
        # Drop expired
        while _recent_opens and now - _recent_opens[0] > CORR_WINDOW_SEC:
            _recent_opens.popleft()
        if len(_recent_opens) >= CORR_MAX_OPENS:
            oldest_age = int(now - _recent_opens[0])
            return False, f'corr_cap {len(_recent_opens)}/{CORR_MAX_OPENS} in {CORR_WINDOW_SEC//60}min (oldest {oldest_age}s)'
        return True, ''


def record_open():
    """Call this on successful arm/entry."""
    now = time.time()
    with _lock:
        _recent_opens.append(now)


def status():
    now = time.time()
    with _lock:
        anchor = _state['utc_anchor_equity']
        recent = [t for t in _recent_opens if now - t <= CORR_WINDOW_SEC]
        return {
            'utc_anchor_day': str(_state['utc_anchor_day']),
            'utc_anchor_equity': anchor,
            'halted_for_day': _state['halted_for_day'],
            'recent_opens_in_window': len(recent),
            'corr_window_sec': CORR_WINDOW_SEC,
            'corr_max_opens': CORR_MAX_OPENS,
            'daily_dd_pct': DAILY_DD_PCT,
        }
