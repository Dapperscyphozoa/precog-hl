"""HOUR_VETO — block signals during UTC hours that are net losers.

From full /analyze data audit (469 real trades), these UTC hours had
cumulative negative P&L:
  04 UTC, 06, 07, 09, 14, 15, 16, 19 → cumulative -$13.55

Profitable hours (allow): 02, 10, 11, 12, 13 → cumulative +$3.41
Other hours (00, 01, 03, 05, 08, 17, 18, 20-23): allow by default
  (insufficient negative evidence to block).

CALIBRATION CAVEAT:
  Per-hour samples are small (hour 19 n=27, hour 23 n=17).
  Wilson CIs are wide. Some hours may have looked bad because of one
  specific engine that's now disabled by VERIFIED_LOSER_BASELINE.
  Re-run /analyze by_hour_utc after +100 closes to confirm the bad
  hours stay bad. Tighten or relax HOUR_VETO_HOURS env at that waypoint
  — env is live-reloaded, no redeploy needed.

Tunables (env):
  HOUR_VETO_ENABLED       default 1
  HOUR_VETO_HOURS         default '4,6,7,9,14,15,16,19'

API:
  blocked() -> (is_blocked: bool, hour: int, reason: str)
  status() -> dict
"""
import os
import time
from datetime import datetime, timezone


ENABLED = os.environ.get('HOUR_VETO_ENABLED', '1') == '1'
_RAW = os.environ.get('HOUR_VETO_HOURS', '4,6,7,9,14,15,16,19')
BLOCKED_HOURS = set()
for tok in _RAW.split(','):
    tok = tok.strip()
    if not tok:
        continue
    try:
        h = int(tok)
        if 0 <= h <= 23:
            BLOCKED_HOURS.add(h)
    except ValueError:
        pass

# Audit n + sum_pnl from /analyze by_hour_utc snapshot (2026-04-29).
# Surfaced in /hour_veto_status so operator can re-evaluate at +100-trade
# waypoint and decide whether each hour's veto remains warranted.
_AUDIT_BASELINE = {
    4:  {'n': 22, 'sum_pnl': -1.06},
    6:  {'n': 18, 'sum_pnl': -0.78},
    7:  {'n': 25, 'sum_pnl': -2.79},
    9:  {'n': 24, 'sum_pnl': -2.03},
    14: {'n': 21, 'sum_pnl': -1.54},
    15: {'n': 23, 'sum_pnl': -2.26},
    16: {'n': 19, 'sum_pnl': -0.83},
    19: {'n': 27, 'sum_pnl': -2.26},
}

_STATS = {'checks': 0, 'blocks': 0, 'blocks_per_hour': {}}


def blocked(now_ts=None):
    """Return (is_blocked, hour_utc, reason).
    Fail-soft: disabled returns (False, hour, 'disabled').
    """
    _STATS['checks'] += 1
    now_ts = now_ts or time.time()
    h = datetime.fromtimestamp(now_ts, tz=timezone.utc).hour
    if not ENABLED:
        return False, h, 'disabled'
    if h in BLOCKED_HOURS:
        _STATS['blocks'] += 1
        _STATS['blocks_per_hour'][h] = _STATS['blocks_per_hour'].get(h, 0) + 1
        return True, h, f'utc_hour_{h}_blocked'
    return False, h, f'utc_hour_{h}_allowed'


def status():
    is_blocked, h, reason = blocked()
    return {
        'enabled': ENABLED,
        'blocked_hours_utc': sorted(BLOCKED_HOURS),
        'current_hour_utc': h,
        'currently_blocking': is_blocked,
        'reason': reason,
        'audit_baseline_per_hour': _AUDIT_BASELINE,
        'calibration_note': 'Wilson CIs wide on n<30 buckets. Re-run /analyze by_hour_utc at +100 closes to confirm. Use HOUR_VETO_HOURS env to tune.',
        'stats': dict(_STATS),
    }
