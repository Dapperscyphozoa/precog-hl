"""LAYER B — BTC volatility flash detector.

Detects abnormal short-term BTC volatility relative to recent baseline.
Used to suspend new entries during regime transitions and storm events
where the trend regime detector hasn't caught up yet.

Design rationale (per session 2026-04-28 discussion):
- Fixed threshold is wrong — 5m vol distribution shifts by regime.
  Calm chop: 0.05-0.10% std. Bull-storm: 0.3-0.6%.
- Use P95 of last 7d 1h-std distribution as adaptive threshold.
- Asymmetric hysteresis: 2 flags to engage (10min), 6 clears to release (30min).
  Bias toward defensive — cheap to suspend, expensive to be wrong.
- Cold start: fallback to 0.4% fixed for first hour while history loads.

API:
  is_volatile() -> bool       # main gate consumer
  status() -> dict            # for /vol_status endpoint
  start()                     # spawn refresh thread (lazy if not called)

Refresh schedule:
- Percentile recomputed hourly (cheap, ~2016 bars)
- Current 1h std checked every 5min on bar boundary

Failure mode:
- HL fetch error → keeps last known threshold + std, continues
- No history at all → returns False (fail-soft to "not volatile")
"""
import os
import time
import json
import threading
import urllib.request

# ─── CONFIG (env-tunable) ─────────────────────────────────────────────
PCTILE              = float(os.environ.get('VOL_PCTILE', '95'))
HISTORY_DAYS        = int(os.environ.get('VOL_HISTORY_DAYS', '7'))
RECOMPUTE_INTERVAL_S = int(os.environ.get('VOL_RECOMPUTE_S', '3600'))
CHECK_INTERVAL_S    = int(os.environ.get('VOL_CHECK_S', '300'))
HYSTERESIS_FLAG     = int(os.environ.get('VOL_HYST_FLAG', '2'))
# VOL_FLASH_ACTION — what to do with EXISTING positions on flash trigger.
# Options:
#   hold              — leave positions alone, only block new entries (default)
#   flatten_losers    — close positions with negative uPnL
#   lock_winners      — close positions with positive uPnL (lock profit)
#   flatten           — close everything (max defense)
# Half-close intentionally NOT offered (compounds fees without meaningful
# risk reduction). NOTE: this module exposes the desired action via
# desired_action(); actual position-closing must be wired into the
# main loop in precog.py to read this and act. Detector is observation-only.
VOL_FLASH_ACTION    = os.environ.get('VOL_FLASH_ACTION', 'hold').lower()
_VALID_ACTIONS = {'hold', 'flatten_losers', 'lock_winners', 'flatten'}
if VOL_FLASH_ACTION not in _VALID_ACTIONS:
    print(f'[vol_detector] WARN: invalid VOL_FLASH_ACTION={VOL_FLASH_ACTION!r}, '
          f'falling back to hold', flush=True)
    VOL_FLASH_ACTION = 'hold'
HYSTERESIS_CLEAR    = int(os.environ.get('VOL_HYST_CLEAR', '6'))
COLD_START_THRESHOLD = float(os.environ.get('VOL_COLD_START_PCT', '0.004'))  # 0.4%
ENABLED             = os.environ.get('VOL_DETECTOR_ENABLED', '1') == '1'
HL_INFO_URL         = 'https://api.hyperliquid.xyz/info'
WINDOW_BARS         = 12  # 12 × 5m = 1h

_LOCK = threading.Lock()
_CACHE = {
    'threshold_pct': None,        # current P95 threshold (decimal, e.g., 0.0035 = 0.35%)
    'current_std_pct': None,      # latest computed current 1h std (decimal)
    'history_n': 0,               # number of 1h windows in distribution
    'last_pctile_ts': 0,
    'last_check_ts': 0,
    'flag_streak': 0,             # consecutive checks above threshold
    'clear_streak': 0,            # consecutive checks below threshold
    'is_volatile': False,         # current state (with hysteresis)
    'transitions': [],            # log of state changes
    'action_callback_invoked': 0, # count of CLEAR→VOLATILE callbacks fired
    'action_callback_errors': 0,  # count of callback exceptions
    'fetch_errors': 0,
    'cold_start': True,
    'started_ts': 0,
}
_ACTION_CALLBACK = None           # set via register_action_callback()
_RUNNING = False


def _log(msg):
    print(f'[vol_detector] {msg}', flush=True)


def _fetch_btc_5m(days):
    """Fetch BTC 5m candles for last `days` days."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    body = json.dumps({
        'type': 'candleSnapshot',
        'req': {'coin': 'BTC', 'interval': '5m',
                'startTime': start_ms, 'endTime': end_ms}
    }).encode()
    req = urllib.request.Request(
        HL_INFO_URL, data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except Exception:
        _CACHE['fetch_errors'] += 1
        return None
    if not isinstance(data, list):
        return None
    bars = []
    for k in data:
        try:
            bars.append({'t': int(k['t']), 'c': float(k['c'])})
        except (KeyError, TypeError, ValueError):
            continue
    return bars


def _compute_log_returns(bars):
    """Return list of log returns from sorted 5m bars."""
    if len(bars) < 2:
        return []
    closes = [b['c'] for b in bars]
    import math
    rets = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0 and closes[i] > 0:
            rets.append(math.log(closes[i] / closes[i-1]))
    return rets


def _rolling_std(rets, window):
    """Rolling std over `window` returns. Returns list of std values."""
    if len(rets) < window:
        return []
    out = []
    for i in range(window, len(rets) + 1):
        slice_ = rets[i - window:i]
        # Manual std (no numpy dep)
        n = len(slice_)
        if n < 2:
            continue
        mean = sum(slice_) / n
        var = sum((r - mean) ** 2 for r in slice_) / n
        out.append(var ** 0.5)
    return out


def _percentile(values, p):
    """Return p-th percentile of values (p in [0, 100])."""
    if not values:
        return None
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_v) - 1)
    if f == c:
        return sorted_v[f]
    return sorted_v[f] + (sorted_v[c] - sorted_v[f]) * (k - f)


def _refresh_threshold():
    """Recompute the P95 threshold from last 7d of BTC 5m bars."""
    bars = _fetch_btc_5m(HISTORY_DAYS)
    if not bars or len(bars) < WINDOW_BARS * 24:  # need at least 24h
        _log(f'refresh: insufficient history ({len(bars) if bars else 0} bars)')
        return None
    rets = _compute_log_returns(bars)
    distribution = _rolling_std(rets, WINDOW_BARS)
    if len(distribution) < 100:
        _log(f'refresh: insufficient distribution ({len(distribution)} windows)')
        return None
    threshold = _percentile(distribution, PCTILE)
    with _LOCK:
        _CACHE['threshold_pct'] = threshold
        _CACHE['history_n'] = len(distribution)
        _CACHE['last_pctile_ts'] = int(time.time())
        _CACHE['cold_start'] = False
    _log(f'refresh: P{int(PCTILE)} threshold = {threshold*100:.4f}% '
         f'(from {len(distribution)} 1h windows)')
    return threshold


def _check_current_std():
    """Compute current 1h std (last 12 5m bars) and update flag state."""
    # Need just last ~2 hours of 5m bars
    bars = _fetch_btc_5m(1)
    if not bars or len(bars) < WINDOW_BARS + 1:
        return None
    # Use last WINDOW_BARS+1 closes for WINDOW_BARS returns
    recent = bars[-(WINDOW_BARS + 1):]
    rets = _compute_log_returns(recent)
    if len(rets) < WINDOW_BARS:
        return None
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    cur_std = var ** 0.5
    return cur_std


def _update_state():
    """One full check cycle: refresh threshold if stale, check std, apply hysteresis."""
    now = time.time()
    # Refresh threshold hourly
    if now - _CACHE.get('last_pctile_ts', 0) > RECOMPUTE_INTERVAL_S:
        _refresh_threshold()
    # Use cold-start threshold if no history yet
    threshold = _CACHE.get('threshold_pct')
    if threshold is None:
        threshold = COLD_START_THRESHOLD
        _log(f'cold start: using fallback threshold {threshold*100:.2f}%')
    cur_std = _check_current_std()
    if cur_std is None:
        return
    with _LOCK:
        _CACHE['current_std_pct'] = cur_std
        _CACHE['last_check_ts'] = int(now)
        if cur_std > threshold:
            _CACHE['flag_streak'] += 1
            _CACHE['clear_streak'] = 0
        else:
            _CACHE['clear_streak'] += 1
            _CACHE['flag_streak'] = 0
        # Apply hysteresis
        prev_state = _CACHE['is_volatile']
        if not prev_state and _CACHE['flag_streak'] >= HYSTERESIS_FLAG:
            _CACHE['is_volatile'] = True
            _CACHE['transitions'].append({
                'ts': int(now), 'to': 'VOLATILE',
                'std': cur_std, 'threshold': threshold,
                'desired_action': VOL_FLASH_ACTION,
            })
            _log(f'STATE → VOLATILE (std={cur_std*100:.3f}% > P{int(PCTILE)}={threshold*100:.3f}%, '
                 f'streak={_CACHE["flag_streak"]}, desired_action={VOL_FLASH_ACTION})')
            if VOL_FLASH_ACTION != 'hold':
                _log(f'POSITION ACTION INTENT: {VOL_FLASH_ACTION}')
                # Invoke registered callback (precog wires this to actual close
                # logic on boot). Fires ONCE per CLEAR→VOLATILE transition.
                cb = _ACTION_CALLBACK
                if cb:
                    try:
                        cb(VOL_FLASH_ACTION)
                        _CACHE['action_callback_invoked'] += 1
                    except Exception as e:
                        _CACHE['action_callback_errors'] += 1
                        _log(f'callback err: {type(e).__name__}: {e}')
                else:
                    _log('no action_callback registered — action is observation-only')
        elif prev_state and _CACHE['clear_streak'] >= HYSTERESIS_CLEAR:
            _CACHE['is_volatile'] = False
            _CACHE['transitions'].append({
                'ts': int(now), 'to': 'CLEAR',
                'std': cur_std, 'threshold': threshold,
            })
            _log(f'STATE → CLEAR (std={cur_std*100:.3f}% <= P{int(PCTILE)}={threshold*100:.3f}%, '
                 f'clear_streak={_CACHE["clear_streak"]})')
        # Trim transition log
        if len(_CACHE['transitions']) > 50:
            _CACHE['transitions'] = _CACHE['transitions'][-50:]


def _loop():
    global _RUNNING
    _RUNNING = True
    _CACHE['started_ts'] = int(time.time())
    _log(f'started: pctile=P{int(PCTILE)} history={HISTORY_DAYS}d '
         f'check_every={CHECK_INTERVAL_S}s hyst_flag={HYSTERESIS_FLAG} '
         f'hyst_clear={HYSTERESIS_CLEAR}')
    # First refresh + check immediately
    _refresh_threshold()
    while True:
        try:
            _update_state()
        except Exception as e:
            _log(f'check error: {type(e).__name__}: {e}')
        time.sleep(CHECK_INTERVAL_S)


def start():
    """Spawn the detector thread. Idempotent. No-op if VOL_DETECTOR_ENABLED=0."""
    global _RUNNING
    if not ENABLED:
        _log('disabled (VOL_DETECTOR_ENABLED=0)')
        return
    if _RUNNING:
        return
    t = threading.Thread(target=_loop, name='vol-detector', daemon=True)
    t.start()
    _log('thread launched')


def is_volatile():
    """Main gate consumer: returns True if BTC vol is currently flagged.
    Fail-soft: returns False if disabled, never started, or no data yet."""
    if not ENABLED:
        return False
    with _LOCK:
        return _CACHE.get('is_volatile', False)


def desired_action():
    """What to do with EXISTING positions when volatile. Returns one of:
      'hold' | 'flatten_losers' | 'lock_winners' | 'flatten'
    Configured via VOL_FLASH_ACTION env. Default 'hold' (no-op).
    The actual position-closing logic is wired via register_action_callback."""
    if not ENABLED or not is_volatile():
        return 'hold'
    return VOL_FLASH_ACTION


def register_action_callback(fn):
    """Register a callback invoked on CLEAR→VOLATILE transitions when
    VOL_FLASH_ACTION != 'hold'. Callback signature: fn(action_str).
    Fires ONCE per transition (idempotent — won't re-fire while volatile).

    Caller's responsibility:
      - Iterate live positions
      - Apply close criteria per action
      - Handle errors gracefully (callback errors are logged but not retried)

    Pattern (from precog.py boot):
        import vol_detector
        vol_detector.register_action_callback(_handle_vol_flash_action)
        vol_detector.start()
    """
    global _ACTION_CALLBACK
    _ACTION_CALLBACK = fn
    _log(f'action callback registered: {getattr(fn, "__name__", repr(fn))}')


def status():
    """Snapshot for /vol_status endpoint."""
    with _LOCK:
        c = dict(_CACHE)
    cold = c.get('cold_start', True)
    threshold = c.get('threshold_pct')
    cur_std = c.get('current_std_pct')
    return {
        'enabled': ENABLED,
        'running': _RUNNING,
        'is_volatile': c.get('is_volatile', False),
        'desired_action': desired_action(),
        'flash_action_config': VOL_FLASH_ACTION,
        'cold_start': cold,
        'threshold_pct': round(threshold * 100, 4) if threshold else None,
        'current_std_pct': round(cur_std * 100, 4) if cur_std else None,
        'ratio_to_threshold': round(cur_std / threshold, 3) if (cur_std and threshold) else None,
        'pctile': PCTILE,
        'history_days': HISTORY_DAYS,
        'history_windows': c.get('history_n', 0),
        'flag_streak': c.get('flag_streak', 0),
        'clear_streak': c.get('clear_streak', 0),
        'hysteresis_flag': HYSTERESIS_FLAG,
        'hysteresis_clear': HYSTERESIS_CLEAR,
        'last_check_ts': c.get('last_check_ts', 0),
        'last_check_age_sec': int(time.time() - c.get('last_check_ts', 0)) if c.get('last_check_ts') else None,
        'last_pctile_ts': c.get('last_pctile_ts', 0),
        'last_pctile_age_sec': int(time.time() - c.get('last_pctile_ts', 0)) if c.get('last_pctile_ts') else None,
        'fetch_errors': c.get('fetch_errors', 0),
        'action_callback_registered': _ACTION_CALLBACK is not None,
        'action_callback_invoked': c.get('action_callback_invoked', 0),
        'action_callback_errors': c.get('action_callback_errors', 0),
        'recent_transitions': c.get('transitions', [])[-10:],
        'uptime_sec': int(time.time() - c.get('started_ts', 0)) if c.get('started_ts') else 0,
    }
