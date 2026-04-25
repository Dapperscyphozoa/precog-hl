"""
atomic_reconciler.py — Post-fill SL/TP size correction for atomic entries.

PROBLEM
=======
atomic_entry submits entry+SL+TP via HL bulk_orders in one API call. The SL
and TP triggers are sized to the REQUESTED entry amount. If HL partial-fills
the IOC entry (rare at small notional, real at scale or in thin liquidity),
the resting SL/TP are now larger than the actual position. When SL fires
HL silently rejects the reduce-only order for "exceeds position size" — the
position is effectively unprotected on the partial-filled portion.

SOLUTION
========
After every atomic entry, we wait for HL's WebSocket userFills/webData2 to
report the actual fill, then reconcile:
  - actual == intent (within tolerance) → mark CONFIRMED, done.
  - actual ≠ intent                     → cancel old SL/TP, place new at correct size.
  - actual = 0 after timeout            → entry never filled, clean up triggers.
  - reconcile fails                     → emergency close + halt.

STATE MACHINE (on ledger row.protection_state)
==============================================
  PROVISIONAL    → set by atomic_entry. Reconciler watches.
  CONFIRMED      → actual matches intent within SIZE_EPS_PCT. No action needed.
  RESIZED        → actual differed; SL/TP cancelled and replaced at correct size.
  RECONCILE_FAIL → couldn't reconcile (cancel/place errors); emergency close fired.

CONCURRENCY
===========
Reconciler shares enforce_protection's per-coin _COIN_LOCKS — both modules
serialize on the same lock. While reconciler holds the coin lock, enforce
won't run; while enforce runs, reconciler will skip and try next cycle.
This prevents double-cancel and oid races.

ARCHITECTURE
============
- One daemon thread, polls every POLL_INTERVAL (1s).
- Iterates rows where protection_state == 'PROVISIONAL' AND state == LIVE.
- Calls reconcile_one() under the per-coin lock.
- Timeout sweep at RECONCILE_TIMEOUT_SEC (15s) — emergency close on fail.
"""

import os
import time
import threading

import position_ledger

# Lazy import of enforce_protection (for shared per-coin locks). enforce_protection
# may not be importable in test environments, in which case we use our own locks.
try:
    import enforce_protection as _ep_mod
    _COIN_LOCKS = _ep_mod._COIN_LOCKS
except Exception:
    from collections import defaultdict
    _COIN_LOCKS = defaultdict(threading.Lock)

# ─── Configuration (env-overridable) ─────────────────────────────────────
POLL_INTERVAL_SEC = float(os.environ.get('RECONCILE_POLL_SEC', '1.0'))
SIZE_EPS_PCT = float(os.environ.get('RECONCILE_SIZE_EPS_PCT', '0.005'))   # 0.5%
RECONCILE_TIMEOUT_SEC = float(os.environ.get('RECONCILE_TIMEOUT_SEC', '15.0'))
WS_FRESHNESS_SEC = float(os.environ.get('RECONCILE_WS_FRESHNESS_SEC', '30.0'))

# ─── Stats ─────────────────────────────────────────────────────────────────
_STATS = {
    'reconcile_attempts': 0,
    'confirmed_match':    0,   # actual matched intent
    'resized_replaced':   0,   # actual differed; SL/TP replaced
    'fail_no_fill':       0,   # entry never filled within timeout
    'fail_cancel_err':    0,
    'fail_replace_err':   0,
    'reconcile_fail':     0,   # → emergency close
    'sweeps':             0,
    'awaiting_fill':      0,   # PROVISIONAL but ledger size still 0
}
_STATS_LOCK = threading.Lock()

# ─── Callbacks (injected by precog.py at startup) ─────────────────────────
_CB = {
    'cancel_order_fn':    None,   # (coin, oid) -> bool
    'place_sl_fn':        None,   # (coin, is_long, entry, size) -> sl_pct or None
    'place_tp_fn':        None,   # (coin, is_long, entry, size) -> tp_pct or None
    'emergency_close_fn': None,   # (coin, reason) -> bool
    'log_fn':             None,
}


def init(cancel_order_fn, place_sl_fn, place_tp_fn,
         emergency_close_fn=None, log_fn=None):
    """Wire callbacks. Idempotent. Must be called once at startup BEFORE start()."""
    _CB['cancel_order_fn']    = cancel_order_fn
    _CB['place_sl_fn']        = place_sl_fn
    _CB['place_tp_fn']        = place_tp_fn
    _CB['emergency_close_fn'] = emergency_close_fn
    _CB['log_fn']             = log_fn or (lambda m: None)


def _log(msg):
    fn = _CB['log_fn']
    if fn:
        try: fn(f'[atomic_reconciler] {msg}')
        except Exception: pass


# ─── Daemon thread ─────────────────────────────────────────────────────────
_thread = None
_stop = threading.Event()


def start():
    """Start daemon. Idempotent."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name='atomic-reconciler', daemon=True)
    _thread.start()
    _log(f'started (poll={POLL_INTERVAL_SEC}s, eps={SIZE_EPS_PCT*100:.2f}%, '
         f'timeout={RECONCILE_TIMEOUT_SEC}s)')


def stop():
    _stop.set()


def _loop():
    """Main reconciler loop. Wakes every POLL_INTERVAL, scans PROVISIONAL rows."""
    while not _stop.wait(POLL_INTERVAL_SEC):
        try:
            with _STATS_LOCK:
                _STATS['sweeps'] += 1
            _sweep()
        except Exception as e:
            _log(f'sweep err: {e}')


def _sweep():
    """One iteration: find PROVISIONAL rows, try to reconcile each."""
    if not position_ledger.ws_is_fresh(max_age_sec=WS_FRESHNESS_SEC):
        # WS feed stale — don't trust ledger sizes. Skip this sweep.
        return

    # Snapshot rows under ledger's lock (cheap; just dict items)
    try:
        rows = position_ledger.all_rows()
    except Exception:
        return

    for coin, row in rows.items():
        prot_state = row.get('protection_state')
        if prot_state != 'PROVISIONAL':
            continue
        # Acquire per-coin lock without blocking — if enforce is running,
        # try again next sweep
        lock = _COIN_LOCKS[coin]
        if not lock.acquire(blocking=False):
            continue
        try:
            # Re-fetch row under lock — state may have changed since snapshot
            row_now = position_ledger.get(coin)
            if not row_now or row_now.get('protection_state') != 'PROVISIONAL':
                continue
            _reconcile_one(coin, row_now)
        finally:
            lock.release()


def _reconcile_one(coin, row):
    """Decide outcome for one PROVISIONAL row. Updates ledger.protection_state."""
    intent = float(row.get('intent_size') or 0)
    actual = float(row.get('size') or 0)
    age = time.time() - float(row.get('created_ts') or 0)
    is_long = bool(row.get('is_long'))
    entry_px = row.get('entry_px') or row.get('intent_entry_px')

    # ─── No fill yet ─────────────────────────────────────────────────────
    if actual <= 1e-12:
        if age > RECONCILE_TIMEOUT_SEC:
            # Entry didn't fill within timeout. Cancel resting SL/TP and
            # mark FAIL. Position never opened so emergency close is moot —
            # just clean up triggers.
            _cancel_orphan_triggers(coin, row, reason='entry_no_fill_timeout')
            position_ledger.set_protection_state(coin, 'RECONCILE_FAIL',
                                                  reason='entry_never_filled')
            with _STATS_LOCK:
                _STATS['fail_no_fill'] += 1
                _STATS['reconcile_fail'] += 1
            _log(f'{coin} FAIL: entry never filled after {age:.1f}s — cleaned up triggers')
            return
        # Still within window — wait
        with _STATS_LOCK:
            _STATS['awaiting_fill'] += 1
        return

    # ─── Actual fill present — compare to intent ─────────────────────────
    if intent <= 1e-12:
        # Defensive: shouldn't happen, treat as confirmed
        position_ledger.set_protection_state(coin, 'CONFIRMED',
                                              reason='intent_zero_fallback')
        return

    pct_diff = abs(actual - intent) / intent
    with _STATS_LOCK:
        _STATS['reconcile_attempts'] += 1

    if pct_diff < SIZE_EPS_PCT:
        # Match within tolerance
        position_ledger.set_protection_state(coin, 'CONFIRMED',
                                              reason=f'matched_pct_diff={pct_diff*100:.2f}')
        with _STATS_LOCK:
            _STATS['confirmed_match'] += 1
        _log(f'{coin} CONFIRMED: intent={intent} actual={actual} (diff {pct_diff*100:.2f}%)')
        return

    # Mismatch → cancel + replace
    _log(f'{coin} RESIZE NEEDED: intent={intent} actual={actual} '
         f'(diff {pct_diff*100:.2f}%) — cancelling and replacing brackets')

    cancel_fn = _CB['cancel_order_fn']
    if cancel_fn is None:
        position_ledger.set_protection_state(coin, 'RECONCILE_FAIL',
                                              reason='no_cancel_callback')
        with _STATS_LOCK:
            _STATS['reconcile_fail'] += 1
        _log(f'{coin} FAIL: no cancel callback wired')
        return

    # 1. Cancel old SL/TP (best effort — log errors, continue)
    cancel_errors = 0
    for kind, oid in (('sl', row.get('sl_oid')), ('tp', row.get('tp_oid'))):
        if not oid:
            continue
        try:
            cancel_fn(coin, oid)
            _log(f'{coin} cancelled old {kind}_oid={oid}')
        except Exception as e:
            cancel_errors += 1
            _log(f'{coin} cancel {kind}_oid={oid} err: {e}')

    if cancel_errors > 0:
        with _STATS_LOCK:
            _STATS['fail_cancel_err'] += 1
        # Continue anyway — HL may have already cancelled them; replace below.

    # 2. Place new SL/TP at actual size
    place_sl = _CB['place_sl_fn']
    place_tp = _CB['place_tp_fn']
    if place_sl is None or place_tp is None:
        position_ledger.set_protection_state(coin, 'RECONCILE_FAIL',
                                              reason='no_place_callbacks')
        with _STATS_LOCK:
            _STATS['reconcile_fail'] += 1
        _log(f'{coin} FAIL: place callbacks not wired')
        _trigger_emergency(coin, 'no_place_callbacks')
        return

    sl_pct = None
    tp_pct = None
    try:
        sl_pct = place_sl(coin, is_long, entry_px, actual)
    except Exception as e:
        _log(f'{coin} place_sl err: {e}')
    try:
        tp_pct = place_tp(coin, is_long, entry_px, actual)
    except Exception as e:
        _log(f'{coin} place_tp err: {e}')

    if sl_pct is None:
        # SL placement failed — position is now naked. Emergency close.
        with _STATS_LOCK:
            _STATS['fail_replace_err'] += 1
            _STATS['reconcile_fail'] += 1
        position_ledger.set_protection_state(coin, 'RECONCILE_FAIL',
                                              reason='sl_replace_failed')
        _log(f'{coin} CRITICAL: SL replace failed — emergency close')
        _trigger_emergency(coin, 'sl_replace_failed_post_resize')
        return

    # SL placed; TP placed (or repair-pending). Mark RESIZED.
    if tp_pct is None:
        _log(f'{coin} TP replace failed but SL ok — RESIZED with TP-pending repair')
    position_ledger.set_protection_state(coin, 'RESIZED',
                                          reason=f'intent={intent}_actual={actual}')
    with _STATS_LOCK:
        _STATS['resized_replaced'] += 1
    _log(f'{coin} RESIZED: SL/TP replaced at actual={actual} '
         f'(was intent={intent})')


def _cancel_orphan_triggers(coin, row, reason):
    """Cancel resting SL/TP when entry never filled."""
    cancel_fn = _CB['cancel_order_fn']
    if cancel_fn is None:
        return
    for kind, oid in (('sl', row.get('sl_oid')), ('tp', row.get('tp_oid'))):
        if not oid:
            continue
        try:
            cancel_fn(coin, oid)
        except Exception as e:
            _log(f'{coin} orphan cancel {kind}_oid={oid} err: {e}')


def _trigger_emergency(coin, reason):
    """Fire emergency close callback if wired."""
    fn = _CB['emergency_close_fn']
    if fn is None:
        _log(f'{coin} no emergency_close callback — manual intervention needed')
        return
    try:
        fn(coin, f'reconciler:{reason}')
    except Exception as e:
        _log(f'{coin} emergency close err: {e}')


# ─── Diagnostic ────────────────────────────────────────────────────────────
def status():
    """Snapshot of reconciler state for /health."""
    with _STATS_LOCK:
        out = dict(_STATS)
    out['running'] = bool(_thread and _thread.is_alive())
    out['poll_interval_sec'] = POLL_INTERVAL_SEC
    out['size_eps_pct'] = SIZE_EPS_PCT
    out['reconcile_timeout_sec'] = RECONCILE_TIMEOUT_SEC
    out['callbacks_wired'] = {
        'cancel_order_fn':    _CB['cancel_order_fn'] is not None,
        'place_sl_fn':        _CB['place_sl_fn'] is not None,
        'place_tp_fn':        _CB['place_tp_fn'] is not None,
        'emergency_close_fn': _CB['emergency_close_fn'] is not None,
    }
    # Currently PROVISIONAL coins
    try:
        rows = position_ledger.all_rows()
        provisional = {c: round(time.time() - r.get('created_ts', 0), 1)
                       for c, r in rows.items()
                       if r.get('protection_state') == 'PROVISIONAL'}
        out['provisional_coins'] = provisional
    except Exception:
        out['provisional_coins'] = {}
    return out
