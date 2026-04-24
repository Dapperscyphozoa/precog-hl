"""Lifecycle Reconciler — sole authoritative executor of trade closes.

Runs as a daemon thread. On each cycle (15s):
  1. Refresh exchange snapshot.
  2. Drain intent queue.
  3. For each unique trade_id in intents:
     - If already closed in ledger: skip (idempotent).
     - If position absent from exchange: record close with reason "exchange_fill_late".
     - Otherwise: execute close on exchange, record in ledger.
  4. Detect orphans (exchange has coin, no open ledger trade) → adopt.
  5. Detect missing-closes (ledger open, exchange closed) → record close.
  6. Compute drift metric; halt new entries if unsafe.

MODES:
  observe (default, RECONCILER_AUTHORITATIVE=0): logs decisions, no writes.
  authoritative (RECONCILER_AUTHORITATIVE=1): executes + writes to ledger.

The module is wired into precog.py via start(). External close authority
(close_trade, execute_market_close, find_fill, etc.) is injected to keep
this file free of HL SDK dependency.

PUBLIC API:
    start(deps)  — begin daemon
    stop()
    status()     — metrics for /lifecycle
    flag_halt()  — read current halt flag
"""
import os
import threading
import time

_STOP = threading.Event()
_DAEMON = None
_LOCK = threading.Lock()

# Injected dependencies (set via start())
_deps = {
    'ledger': None,            # trade_ledger module
    'snapshot': None,          # exchange_snapshot module
    'intent_queue': None,      # intent_queue module
    'close_trade_fn': None,    # close_trade(trade_id, reason, exit_price, ...)
    'execute_close_fn': None,  # market-close a live position on exchange → returns fill_px or None
    'find_fill_fn': None,      # (coin, since_ts, direction) -> {oid, px, sz} or None
    'log_fn': print,
    'state': None,             # precog state dict (read-only references)
}

# Config
_INTERVAL_SEC = 15
_MAX_INTENTS_PER_CYCLE = 50
_DRIFT_UNSAFE_THRESHOLD = 0.05    # 5% drift → halt
_DRIFT_HEALTHY_THRESHOLD = 0.01   # 1% drift → healthy
_DRIFT_HALT_RECOVERY_CYCLES = 3   # N consecutive healthy cycles to clear halt
_RECONCILER_STALE_SEC = 60         # if daemon stalls > this, halt new entries

_METRICS = {
    'mode': 'observe',
    'cycles_total': 0,
    'intents_processed': 0,
    'closes_executed': 0,
    'closes_skipped_idempotent': 0,
    'closes_skipped_stale_snapshot': 0,
    'exchange_fills_matched': 0,
    'exchange_fills_unmatched': 0,
    'orphans_adopted': 0,
    'missing_closes_recorded': 0,
    'last_cycle_ts': 0.0,
    'last_cycle_duration_ms': 0,
    'last_error': None,
    'errors_total': 0,
    'halt_flag': False,
    'halt_since_ts': 0.0,
    'healthy_streak': 0,
    'last_drift_pct': None,
    'observe_would_close_count': 0,
}


def _is_authoritative():
    return os.environ.get('RECONCILER_AUTHORITATIVE', '0') == '1'


def _log(msg):
    fn = _deps.get('log_fn') or print
    try:
        fn(f"[reconciler] {msg}")
    except Exception:
        pass


def _resolve_trade_id(intent):
    """If intent lacks trade_id, look up from ledger by coin."""
    tid = intent.get('trade_id')
    if tid:
        return tid
    coin = intent.get('coin')
    ledger = _deps.get('ledger')
    if not ledger:
        return None
    return ledger.latest_open_trade_id_for_coin(coin) if hasattr(ledger, 'latest_open_trade_id_for_coin') else None


def _find_recent_fill(coin, fills, since_ts=None, max_age_sec=30):
    """Pick the most recent Close fill for this coin."""
    if not fills:
        return None
    now_ms = int(time.time() * 1000)
    cutoff_ms = (since_ts * 1000) if since_ts else (now_ms - max_age_sec * 1000)
    best = None
    best_ts = 0
    for f in fills:
        if f.get('coin') != coin:
            continue
        d = (f.get('dir') or '').lower()
        if 'close' not in d:
            continue
        try:
            ts = int(f.get('time', 0))
        except (TypeError, ValueError):
            continue
        if ts < cutoff_ms:
            continue
        if ts > best_ts:
            best_ts = ts
            best = f
    return best


def _compute_drift(snap, ledger_stats):
    """drift = |exch_open - ledger_open| / max(exch_open, 1)"""
    try:
        exch = len(snap.get('positions', {}))
        led = ledger_stats.get('open_trades_count', 0)
        denom = max(exch, 1)
        return abs(exch - led) / denom
    except Exception:
        return None


def _update_halt_flag(drift_pct):
    """Update halt flag based on drift."""
    if drift_pct is None:
        return
    with _LOCK:
        if drift_pct >= _DRIFT_UNSAFE_THRESHOLD:
            if not _METRICS['halt_flag']:
                _METRICS['halt_flag'] = True
                _METRICS['halt_since_ts'] = time.time()
                _METRICS['healthy_streak'] = 0
                _log(f"⚠ HALT: drift {drift_pct*100:.2f}% >= {_DRIFT_UNSAFE_THRESHOLD*100:.0f}% — blocking new entries")
        elif drift_pct < _DRIFT_HEALTHY_THRESHOLD:
            _METRICS['healthy_streak'] += 1
            if _METRICS['halt_flag'] and _METRICS['healthy_streak'] >= _DRIFT_HALT_RECOVERY_CYCLES:
                _METRICS['halt_flag'] = False
                _METRICS['halt_since_ts'] = 0.0
                _log(f"✓ HALT CLEARED after {_METRICS['healthy_streak']} healthy cycles")
        else:
            _METRICS['healthy_streak'] = 0


def _process_intent(intent, snap, authoritative):
    """Handle one intent. Returns outcome string."""
    tid = _resolve_trade_id(intent)
    coin = intent.get('coin', '')
    reason = intent.get('reason') or intent.get('type', '').lower()
    ledger = _deps['ledger']
    close_trade_fn = _deps['close_trade_fn']
    execute_close_fn = _deps['execute_close_fn']

    if not tid:
        _log(f"INTENT NO-TID: {coin} {intent.get('type')} (no trade_id resolved)")
        return 'no_tid'

    # Idempotency check
    if ledger and ledger.is_closed(tid):
        with _LOCK:
            _METRICS['closes_skipped_idempotent'] += 1
        return 'already_closed'

    position = snap.get('positions', {}).get(coin)

    # Case A: position already gone from exchange → late detection of native fill
    if not position:
        fill = _find_recent_fill(coin, snap.get('fills', []))
        if not authoritative:
            with _LOCK:
                _METRICS['observe_would_close_count'] += 1
            _log(f"OBSERVE would-close {coin} trade_id={tid[:8]} reason=exchange_fill_late "
                 f"exit={fill['px'] if fill else 'unknown'}")
            return 'observe_exchange_fill'
        ok = close_trade_fn(
            trade_id=tid,
            close_reason='exchange_fill_late',
            exit_price=float(fill['px']) if fill else None,
            exchange_fill_id=fill.get('oid') if fill else None,
            source='reconcile_fill_match',
        )
        with _LOCK:
            if fill:
                _METRICS['exchange_fills_matched'] += 1
            else:
                _METRICS['exchange_fills_unmatched'] += 1
            if ok:
                _METRICS['closes_executed'] += 1
        return 'closed_via_exchange_fill'

    # Case B: position still open — execute the close
    if not authoritative:
        with _LOCK:
            _METRICS['observe_would_close_count'] += 1
        _log(f"OBSERVE would-close {coin} trade_id={tid[:8]} reason={reason} "
             f"(position still live, would market-close)")
        return 'observe_market_close'

    # Execute market close
    try:
        fill_px = execute_close_fn(coin) if execute_close_fn else None
    except Exception as e:
        with _LOCK:
            _METRICS['errors_total'] += 1
            _METRICS['last_error'] = f"execute_close err {coin}: {e}"
        _log(f"EXECUTE ERR {coin}: {e}")
        return 'execute_err'

    # Brief settle delay before reading fill
    time.sleep(0.3)
    # Refresh snapshot for fresh fills
    try:
        _deps['snapshot'].force_refresh()
    except Exception:
        pass
    fresh = _deps['snapshot'].get()
    fill = _find_recent_fill(coin, fresh.get('fills', []), since_ts=time.time() - 10)

    ok = close_trade_fn(
        trade_id=tid,
        close_reason=reason,
        exit_price=float(fill['px']) if fill else (fill_px if fill_px else None),
        exchange_fill_id=fill.get('oid') if fill else None,
        source='reconcile_intent',
    )
    with _LOCK:
        if ok:
            _METRICS['closes_executed'] += 1
    return 'closed_via_intent'


def _detect_orphans(snap, ledger_stats):
    """Coins on exchange but not in ledger as open."""
    ledger = _deps['ledger']
    if not ledger:
        return
    exch_coins = set(snap.get('positions', {}).keys())
    try:
        ledger_open_coins = set(
            t.get('coin') for t in ledger.open_trades() if t.get('coin')
        )
    except Exception:
        ledger_open_coins = set()
    for coin in exch_coins - ledger_open_coins:
        _log(f"ORPHAN DETECTED: {coin} on exchange, not in ledger open set")
        # NOTE: main loop's reconcile block in precog.py handles adoption already.
        # We just count + log here.
        with _LOCK:
            _METRICS['orphans_adopted'] += 1


def _detect_missing_closes(snap, authoritative):
    """Trades open in ledger but position absent on exchange."""
    ledger = _deps['ledger']
    close_trade_fn = _deps['close_trade_fn']
    if not ledger:
        return
    try:
        ledger_opens = ledger.open_trades()
    except Exception:
        return
    exch_coins = set(snap.get('positions', {}).keys())
    for trade in ledger_opens:
        coin = trade.get('coin', '')
        tid = trade.get('trade_id', '')
        if not coin or not tid:
            continue
        if coin in exch_coins:
            continue
        # Ledger says open, exchange says closed → missing close event
        fill = _find_recent_fill(coin, snap.get('fills', []))
        if not authoritative:
            with _LOCK:
                _METRICS['observe_would_close_count'] += 1
            _log(f"OBSERVE missing-close {coin} trade_id={tid[:8]} "
                 f"exit={fill['px'] if fill else 'unknown'}")
            continue
        ok = close_trade_fn(
            trade_id=tid,
            close_reason='exchange_fill',
            exit_price=float(fill['px']) if fill else None,
            exchange_fill_id=fill.get('oid') if fill else None,
            source='reconcile_missing_close',
        )
        with _LOCK:
            if ok:
                _METRICS['missing_closes_recorded'] += 1
                if fill:
                    _METRICS['exchange_fills_matched'] += 1
                else:
                    _METRICS['exchange_fills_unmatched'] += 1


def _cycle():
    """One reconcile cycle."""
    t0 = time.time()
    authoritative = _is_authoritative()
    with _LOCK:
        _METRICS['mode'] = 'authoritative' if authoritative else 'observe'
        _METRICS['cycles_total'] += 1

    try:
        snapshot = _deps.get('snapshot')
        ledger = _deps.get('ledger')
        iq = _deps.get('intent_queue')

        if not snapshot or not ledger or not iq:
            _log("SKIP: missing deps")
            return

        snap = snapshot.get()
        if snap.get('stale'):
            with _LOCK:
                _METRICS['closes_skipped_stale_snapshot'] += 1
            _log(f"SKIP cycle: snapshot stale (age={snap.get('age_sec'):.1f}s)")
            return

        ledger_stats = ledger.stats()

        # 1. Process intents (by unique trade_id; last-wins)
        intents = iq.drain(max_items=_MAX_INTENTS_PER_CYCLE)
        if intents:
            by_tid = {}
            for intent in intents:
                tid = _resolve_trade_id(intent) or f"_no_tid_{intent.get('coin')}"
                by_tid[tid] = intent  # last-wins dedup
            for tid_key, intent in by_tid.items():
                try:
                    _process_intent(intent, snap, authoritative)
                    with _LOCK:
                        _METRICS['intents_processed'] += 1
                except Exception as e:
                    with _LOCK:
                        _METRICS['errors_total'] += 1
                        _METRICS['last_error'] = f"process_intent: {e}"
                    _log(f"process_intent err {intent}: {e}")

        # 2. Detect orphans (advisory only — precog.py main loop handles adoption)
        _detect_orphans(snap, ledger_stats)

        # 3. Detect missing-closes (reconciler-owned in authoritative mode)
        _detect_missing_closes(snap, authoritative)

        # 4. Drift metric + halt flag
        drift_pct = _compute_drift(snap, ledger_stats)
        _update_halt_flag(drift_pct)
        with _LOCK:
            _METRICS['last_drift_pct'] = drift_pct

        with _LOCK:
            _METRICS['last_cycle_ts'] = time.time()
            _METRICS['last_cycle_duration_ms'] = int((time.time() - t0) * 1000)
    except Exception as e:
        with _LOCK:
            _METRICS['errors_total'] += 1
            _METRICS['last_error'] = f"cycle: {e}"
        _log(f"cycle err: {e}")


def _run():
    _log(f"daemon started (interval={_INTERVAL_SEC}s, mode={'authoritative' if _is_authoritative() else 'observe'})")
    while not _STOP.is_set():
        _cycle()
        _STOP.wait(_INTERVAL_SEC)


def start(deps=None, interval_sec=None):
    """Start reconciler daemon. Injected deps should include:
       ledger, snapshot, intent_queue, close_trade_fn, execute_close_fn, log_fn, state"""
    global _DAEMON, _INTERVAL_SEC
    if deps:
        _deps.update(deps)
    if interval_sec:
        _INTERVAL_SEC = interval_sec
    if _DAEMON and _DAEMON.is_alive():
        return
    _DAEMON = threading.Thread(target=_run, name='lifecycle-reconciler', daemon=True)
    _DAEMON.start()


def stop():
    _STOP.set()


def is_halted():
    with _LOCK:
        return _METRICS['halt_flag']


def status():
    """For /lifecycle endpoint."""
    with _LOCK:
        m = dict(_METRICS)
    m['authoritative'] = _is_authoritative()
    m['daemon_alive'] = _DAEMON.is_alive() if _DAEMON else False
    m['interval_sec'] = _INTERVAL_SEC
    last_ts = m.get('last_cycle_ts') or 0
    m['cycle_stale'] = (time.time() - last_ts > _RECONCILER_STALE_SEC) if last_ts else True
    return m
