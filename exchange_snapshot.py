"""Exchange Snapshot — cached, versioned view of HL exchange state.

Reconciler reads from here instead of making live HL calls. This prevents
false orphan detection during HL 429 throttling or transient failures.

The snapshot is refreshed by a dedicated thread on a fixed cadence.
Callers get (data, age_sec, stale) so they can decide whether to act.

PUBLIC API:
    start(user_state_fn, user_fills_fn)   -> None (starts refresh daemon)
    get()                                  -> dict {positions, fills, ts, version, stale, age_sec}
    force_refresh()                        -> bool (synchronous refresh, returns success)
    status()                               -> dict (for /lifecycle endpoint)
"""
import threading
import time

_SNAPSHOT = {
    'positions': {},       # coin -> {size, entry, pnl, mark, lev}
    'fills': [],           # list of recent fill dicts
    'ts': 0.0,             # unix seconds of last successful refresh
    'version': 0,
    'last_error': None,
    'refresh_count': 0,
    'error_count': 0,
    'consecutive_errors': 0,
}
_LOCK = threading.Lock()
_STOP = threading.Event()
_REFRESH_INTERVAL_SEC = 5
_STALE_THRESHOLD_SEC = 60   # 2026-04-25: 30→60. With refresh_interval at 10s
                            # and occasional 429-induced skips, 30s was tripping
                            # reconciler halt unnecessarily. 60s = 6 missed
                            # refreshes before halt — true outage signal.
_FILLS_WINDOW_SEC = 300  # keep 5 min of fills

_user_state_fn = None  # callable(wallet) -> dict (HL clearinghouseState)
_user_fills_fn = None  # callable(wallet, start_ts_ms, end_ts_ms) -> list[dict]
_wallet = None
_daemon_thread = None


def _parse_positions(user_state: dict) -> dict:
    """Convert HL clearinghouseState to flat {coin: info} map."""
    out = {}
    if not user_state:
        return out
    for ap in user_state.get('assetPositions', []):
        p = ap.get('position', {})
        coin = p.get('coin', '')
        try:
            szi = float(p.get('szi', 0))
        except (TypeError, ValueError):
            continue
        if szi == 0 or not coin:
            continue
        try:
            entry = float(p.get('entryPx', 0) or 0)
            pnl = float(p.get('unrealizedPnl', 0) or 0)
            lev = int(p.get('leverage', {}).get('value', 1) or 1)
        except (TypeError, ValueError):
            entry, pnl, lev = 0, 0, 1
        out[coin] = {
            'size': abs(szi),
            'side': 'L' if szi > 0 else 'S',
            'szi': szi,
            'entry': entry,
            'pnl': pnl,
            'lev': lev,
        }
    return out


def _refresh_once() -> bool:
    """One refresh cycle. Returns True on success."""
    if _user_state_fn is None or _wallet is None:
        return False
    try:
        user_state = _user_state_fn(_wallet)
        positions = _parse_positions(user_state)
    except Exception as e:
        with _LOCK:
            _SNAPSHOT['last_error'] = f'user_state: {e}'
            _SNAPSHOT['error_count'] += 1
            _SNAPSHOT['consecutive_errors'] += 1
        return False

    # Fetch recent fills (optional — only if fn provided)
    fills = []
    if _user_fills_fn is not None:
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - _FILLS_WINDOW_SEC * 1000
        try:
            fills = _user_fills_fn(_wallet, start_ms, now_ms) or []
        except Exception as e:
            # Don't fail whole refresh on fills error
            with _LOCK:
                _SNAPSHOT['last_error'] = f'user_fills (non-fatal): {e}'

    with _LOCK:
        _SNAPSHOT['positions'] = positions
        _SNAPSHOT['fills'] = fills
        _SNAPSHOT['ts'] = time.time()
        _SNAPSHOT['version'] += 1
        _SNAPSHOT['refresh_count'] += 1
        _SNAPSHOT['consecutive_errors'] = 0
    return True


def _refresh_loop():
    while not _STOP.is_set():
        _refresh_once()
        _STOP.wait(_REFRESH_INTERVAL_SEC)


def start(user_state_fn, user_fills_fn=None, wallet=None,
          refresh_interval_sec=None):
    """Start the refresh daemon. Safe to call multiple times (idempotent)."""
    global _user_state_fn, _user_fills_fn, _wallet, _daemon_thread, _REFRESH_INTERVAL_SEC
    _user_state_fn = user_state_fn
    _user_fills_fn = user_fills_fn
    _wallet = wallet
    if refresh_interval_sec:
        _REFRESH_INTERVAL_SEC = refresh_interval_sec

    if _daemon_thread is not None and _daemon_thread.is_alive():
        return

    # First refresh synchronous so snapshot is ready before daemon starts
    _refresh_once()

    _daemon_thread = threading.Thread(target=_refresh_loop,
                                      name='exchange-snapshot',
                                      daemon=True)
    _daemon_thread.start()
    print(f'[exchange_snapshot] daemon started (interval={_REFRESH_INTERVAL_SEC}s)', flush=True)


def stop():
    _STOP.set()


def force_refresh() -> bool:
    """Synchronous refresh. Returns True on success."""
    return _refresh_once()


def get() -> dict:
    """Return current snapshot view. Safe to call from any thread."""
    with _LOCK:
        age = time.time() - _SNAPSHOT['ts'] if _SNAPSHOT['ts'] > 0 else float('inf')
        return {
            'positions': dict(_SNAPSHOT['positions']),
            'fills': list(_SNAPSHOT['fills']),
            'ts': _SNAPSHOT['ts'],
            'version': _SNAPSHOT['version'],
            'age_sec': age,
            'stale': age > _STALE_THRESHOLD_SEC,
        }


def status() -> dict:
    """For /lifecycle endpoint."""
    with _LOCK:
        age = time.time() - _SNAPSHOT['ts'] if _SNAPSHOT['ts'] > 0 else float('inf')
        return {
            'version': _SNAPSHOT['version'],
            'age_sec': round(age, 1),
            'stale': age > _STALE_THRESHOLD_SEC,
            'refresh_count': _SNAPSHOT['refresh_count'],
            'error_count': _SNAPSHOT['error_count'],
            'consecutive_errors': _SNAPSHOT['consecutive_errors'],
            'last_error': _SNAPSHOT['last_error'],
            'refresh_interval_sec': _REFRESH_INTERVAL_SEC,
            'positions_count': len(_SNAPSHOT['positions']),
        }
