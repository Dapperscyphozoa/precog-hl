"""LAYER C — Position feedback / performance circuit-breaker.

Real-time signal that's faster than Layer A (regime detector, ~2h) and
Layer B (vol detector, ~10min). Watches OUR OWN trade outcomes — if the
last N closes show terrible WR + R:R, suspend new entries for a cool-off.

Catches:
- Strategy-environment mismatch (regime detector said "calm" but our
  trades are getting picked off — something's off, pause)
- Cluster losses (5 SLs in a row across different coins)
- Mid-regime degradation (P95 vol detector hasn't tripped, but trades
  are losing anyway)

This layer is REACTIVE — it can only fire after losses already happened.
But the suspension prevents EXTENDING the bleed. Cheaper than continuing
to fire into a regime mismatch.

Design:
- Window: last N hours of System B closes (default 1h)
- Trigger: WR < 30% AND avg_loss > 2× avg_win AND ≥5 closes in window
- Time-based release: warning stays for fixed duration, then re-evaluates
- Asymmetric: instant trigger, slow release (15 min default)

API:
  is_warning() -> bool        # main gate consumer
  status() -> dict            # for /perf_status endpoint
  start()                     # spawn check thread
"""
import os
import time
import threading

# ─── CONFIG (env-tunable) ─────────────────────────────────────────────
WINDOW_HOURS         = float(os.environ.get('PERF_WINDOW_HOURS', '1.0'))
# 2026-04-28: bumped 5 → 10. At n=5 with true_WR=50%, P(WR<30%) = 18.75%
# (false-positive rate). At n=10, drops to ~5.5%. Combined with the
# loss-ratio condition, joint false-positive becomes negligible.
# Trade-off: ~1.5-2h to first possible trigger at current trade frequency.
# Acceptable since Layer B (vol detector) handles fast events.
MIN_CLOSES           = int(os.environ.get('PERF_MIN_CLOSES', '10'))
WR_THRESHOLD         = float(os.environ.get('PERF_WR_THRESHOLD', '0.30'))
LOSS_RATIO_THRESHOLD = float(os.environ.get('PERF_LOSS_RATIO', '2.0'))
WARNING_DURATION_S   = int(os.environ.get('PERF_WARNING_DURATION_S', '900'))  # 15min
CHECK_INTERVAL_S     = int(os.environ.get('PERF_CHECK_S', '60'))  # 1min
ENABLED              = os.environ.get('PERF_FEEDBACK_ENABLED', '1') == '1'
SYSTEM               = os.environ.get('PERF_SYSTEM', 'b')  # 'a' or 'b'

_LOCK = threading.Lock()
_STATE = {
    'is_warning': False,
    'last_check_ts': 0,
    'last_warning_ts': 0,
    'window_stats': None,
    'transitions': [],
    'started_ts': 0,
    'check_count': 0,
    'trigger_count': 0,
}
_RUNNING = False


def _log(msg):
    print(f'[position_feedback] {msg}', flush=True)


def _check():
    """One evaluation: read ledger stats over window, apply rules, update state."""
    try:
        import trade_ledger
        agg = trade_ledger.system_aggregate(SYSTEM, hours=WINDOW_HOURS)
    except Exception as e:
        _log(f'ledger read err: {type(e).__name__}: {e}')
        return

    n = int(agg.get('closed_count') or 0)
    wins = int(agg.get('wins') or 0)
    losses = int(agg.get('losses') or 0)
    decided = wins + losses
    wr = (wins / decided) if decided > 0 else None

    # avg_win/avg_loss are signed — take abs for ratio
    avg_win = abs(float(agg.get('avg_win_usd') or 0))
    avg_loss = abs(float(agg.get('avg_loss_usd') or 0))
    loss_ratio = (avg_loss / avg_win) if avg_win > 0 else None

    now = time.time()
    is_bad = (
        n >= MIN_CLOSES and
        wr is not None and wr < WR_THRESHOLD and
        loss_ratio is not None and loss_ratio > LOSS_RATIO_THRESHOLD
    )

    with _LOCK:
        prev_warning = _STATE['is_warning']
        _STATE['last_check_ts'] = int(now)
        _STATE['check_count'] += 1
        _STATE['window_stats'] = {
            'n': n,
            'wins': wins,
            'losses': losses,
            'wr_pct': round(wr * 100, 1) if wr is not None else None,
            'avg_win_usd': round(avg_win, 4),
            'avg_loss_usd': round(avg_loss, 4),
            'loss_ratio': round(loss_ratio, 2) if loss_ratio is not None else None,
        }

        # State transitions
        if is_bad and not prev_warning:
            # Trigger warning
            _STATE['is_warning'] = True
            _STATE['last_warning_ts'] = int(now)
            _STATE['trigger_count'] += 1
            _STATE['transitions'].append({
                'ts': int(now), 'to': 'WARNING',
                'wr_pct': _STATE['window_stats']['wr_pct'],
                'loss_ratio': _STATE['window_stats']['loss_ratio'],
                'n': n,
            })
            _log(f'TRIGGER: WR={wr*100:.1f}% loss_ratio={loss_ratio:.2f} '
                 f'n={n} → WARNING for {WARNING_DURATION_S}s')
            if len(_STATE['transitions']) > 50:
                _STATE['transitions'] = _STATE['transitions'][-50:]
        elif prev_warning:
            # Time-based release — duration elapsed since warning_ts
            age = now - _STATE['last_warning_ts']
            if age >= WARNING_DURATION_S:
                # Re-evaluate: if still bad, refresh warning_ts; if good, clear
                if is_bad:
                    _STATE['last_warning_ts'] = int(now)
                    _log(f'WARNING extended: still bad after {age:.0f}s '
                         f'(WR={wr*100:.1f}% loss_ratio={loss_ratio:.2f})')
                else:
                    _STATE['is_warning'] = False
                    _STATE['transitions'].append({
                        'ts': int(now), 'to': 'CLEAR',
                        'wr_pct': _STATE['window_stats']['wr_pct'] if _STATE['window_stats'] else None,
                        'loss_ratio': _STATE['window_stats']['loss_ratio'] if _STATE['window_stats'] else None,
                        'n': n,
                    })
                    _log(f'CLEAR: stats recovered after {age:.0f}s '
                         f'(WR={wr*100:.1f}% loss_ratio={loss_ratio:.2f if loss_ratio else "n/a"})')


def _loop():
    global _RUNNING
    _RUNNING = True
    _STATE['started_ts'] = int(time.time())
    _log(f'started: window={WINDOW_HOURS}h min_closes={MIN_CLOSES} '
         f'wr_thresh={WR_THRESHOLD*100:.0f}% loss_ratio_thresh={LOSS_RATIO_THRESHOLD} '
         f'duration={WARNING_DURATION_S}s system={SYSTEM!r}')
    while True:
        try:
            _check()
        except Exception as e:
            _log(f'check error: {type(e).__name__}: {e}')
        time.sleep(CHECK_INTERVAL_S)


def start():
    """Spawn check thread. Idempotent. No-op if PERF_FEEDBACK_ENABLED=0."""
    global _RUNNING
    if not ENABLED:
        _log('disabled (PERF_FEEDBACK_ENABLED=0)')
        return
    if _RUNNING:
        return
    t = threading.Thread(target=_loop, name='position-feedback', daemon=True)
    t.start()
    _log('thread launched')


def is_warning():
    """Main gate consumer: True if performance is currently bad enough to
    warrant suspending new entries. Fail-soft: False if disabled or never
    started."""
    if not ENABLED:
        return False
    with _LOCK:
        return _STATE.get('is_warning', False)


def status():
    """Snapshot for /perf_status endpoint."""
    with _LOCK:
        s = dict(_STATE)
    now = time.time()
    warning_age_sec = None
    warning_remaining_s = None
    if s.get('is_warning') and s.get('last_warning_ts'):
        warning_age_sec = int(now - s['last_warning_ts'])
        warning_remaining_s = max(0, WARNING_DURATION_S - warning_age_sec)
    return {
        'enabled': ENABLED,
        'running': _RUNNING,
        'is_warning': s.get('is_warning', False),
        'system': SYSTEM,
        'window_hours': WINDOW_HOURS,
        'min_closes': MIN_CLOSES,
        'wr_threshold_pct': WR_THRESHOLD * 100,
        'loss_ratio_threshold': LOSS_RATIO_THRESHOLD,
        'warning_duration_s': WARNING_DURATION_S,
        'window_stats': s.get('window_stats'),
        'check_count': s.get('check_count', 0),
        'trigger_count': s.get('trigger_count', 0),
        'last_check_ts': s.get('last_check_ts', 0),
        'last_check_age_sec': int(now - s.get('last_check_ts', 0)) if s.get('last_check_ts') else None,
        'last_warning_ts': s.get('last_warning_ts', 0),
        'warning_age_sec': warning_age_sec,
        'warning_remaining_s': warning_remaining_s,
        'recent_transitions': s.get('transitions', [])[-10:],
        'uptime_sec': int(now - s.get('started_ts', 0)) if s.get('started_ts') else 0,
    }
