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
_INTERVAL_SEC_DEGRADED = 5       # faster reconcile when drift degraded
_MAX_INTENTS_PER_CYCLE = 50
_DRIFT_DEGRADED_THRESHOLD = 0.01  # 1% drift → degraded (spec: reduce entries, skip high-risk, faster cycle)
_DRIFT_UNSAFE_THRESHOLD = 0.05    # 5% drift → unsafe (halt new entries)
_DRIFT_CRITICAL_CYCLES = 5        # sustained unsafe for N cycles → emergency flag
_DRIFT_HEALTHY_THRESHOLD = 0.01   # <1% drift → healthy
_DRIFT_HALT_RECOVERY_CYCLES = 3   # N consecutive healthy cycles to clear halt
_RECONCILER_STALE_SEC = 60         # if daemon stalls > this, halt new entries
_RECONCILER_EMERGENCY_STALL_SEC = 120  # if daemon stalls > this, emergency flag

# Backpressure (spec §6)
_INTENT_BACKLOG_PAUSE_THRESHOLD = 50

# Circuit breaker config
_CB_WINDOW = 20
_CB_ERROR_RATE_TRIP = 0.30

# Idempotency ring
_RECENT_CLOSED_RING_SIZE = 100
_RECENT_CLOSED_COIN_TTL_SEC = 45   # skip orphan-adopt for coins closed within this window
_recent_closed_trade_ids = []
_recent_closed_coins = {}          # coin -> ts of most recent close
_cycle_error_flags = []

_METRICS = {
    'mode': 'observe',
    'cycles_total': 0,
    'intents_processed': 0,
    'closes_executed': 0,
    'closes_skipped_idempotent': 0,
    'closes_skipped_stale_snapshot': 0,
    'closes_skipped_ring_dedup': 0,
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
    # Step 4 additions
    'drift_tier': 'unknown',           # healthy | degraded | unsafe | critical (per spec §1)
    'unsafe_streak': 0,                # consecutive cycles at unsafe drift
    'emergency_flatten_authorized': False,
    'circuit_breaker_tripped': False,
    'circuit_breaker_tripped_ts': 0.0,
    'circuit_breaker_error_rate': 0.0,
    'forced_snapshot_refreshes': 0,
    'recent_closed_ring_size': 0,
    # Spec §2 autonomous behaviors
    'entry_limiter': 'full',           # full | reduced | halted
    'skip_high_risk_coins': False,
    'pause_new_intents': False,        # backpressure flag
    'reconciler_lag_s': 0.0,           # seconds since last cycle
    'orphans_repaired_authoritatively': 0,  # reconciler-owned adoption count
}


def _is_authoritative():
    """Returns True only if env=1 AND circuit breaker not tripped."""
    if _METRICS.get('circuit_breaker_tripped'):
        return False  # circuit breaker forces observe mode
    return os.environ.get('RECONCILER_AUTHORITATIVE', '0') == '1'


def _record_cycle_error(is_error):
    """Track error rate for circuit breaker."""
    global _cycle_error_flags
    _cycle_error_flags.append(1 if is_error else 0)
    if len(_cycle_error_flags) > _CB_WINDOW:
        _cycle_error_flags = _cycle_error_flags[-_CB_WINDOW:]

    if len(_cycle_error_flags) >= _CB_WINDOW:
        rate = sum(_cycle_error_flags) / len(_cycle_error_flags)
        with _LOCK:
            _METRICS['circuit_breaker_error_rate'] = round(rate, 3)
            if rate >= _CB_ERROR_RATE_TRIP and not _METRICS['circuit_breaker_tripped']:
                _METRICS['circuit_breaker_tripped'] = True
                _METRICS['circuit_breaker_tripped_ts'] = time.time()
                _log(f"⚠⚠ CIRCUIT BREAKER TRIPPED: error rate {rate*100:.1f}% >= {_CB_ERROR_RATE_TRIP*100:.0f}% "
                     f"— forcing observe mode (manual reset via /lifecycle/emergency)")


def _in_recent_closed_ring(trade_id):
    """Idempotency guard — was this trade_id closed very recently?"""
    return trade_id in _recent_closed_trade_ids


def _add_to_recent_closed_ring(trade_id, coin=None):
    """Add trade_id to ring buffer of recent closes; also track coin with timestamp."""
    global _recent_closed_trade_ids
    if trade_id in _recent_closed_trade_ids:
        return
    _recent_closed_trade_ids.append(trade_id)
    if len(_recent_closed_trade_ids) > _RECENT_CLOSED_RING_SIZE:
        _recent_closed_trade_ids = _recent_closed_trade_ids[-_RECENT_CLOSED_RING_SIZE:]
    if coin:
        _recent_closed_coins[coin] = time.time()
    with _LOCK:
        _METRICS['recent_closed_ring_size'] = len(_recent_closed_trade_ids)


def _coin_recently_closed(coin):
    """True if we closed this coin within the last _RECENT_CLOSED_COIN_TTL_SEC.

    Checks BOTH:
      1. Local recent-closed ring (closes that went through the reconciler's
         own intent path)
      2. The trade_ledger directly (closes that bypassed the reconciler —
         e.g. confluence_close, precog close_trade, webhook close)

    Without (2), confluence-side closes left the reconciler unaware, so
    the post-close exchange-snapshot lag caused the reconciler to adopt
    the still-flattening position as a fresh RECONCILED trade — the
    duplicate-booking pattern observed on XRP/WLFI/UNI/etc after the
    WR-fix deploy.
    """
    ts = _recent_closed_coins.get(coin)
    if ts and (time.time() - ts) < _RECENT_CLOSED_COIN_TTL_SEC:
        return True
    # Ledger fallback — catches close paths that don't touch the local ring
    try:
        ledger = _deps.get('ledger')
        if ledger and hasattr(ledger, 'recent_close_ts'):
            led_ts = ledger.recent_close_ts(coin, max_age_sec=_RECENT_CLOSED_COIN_TTL_SEC)
            if led_ts is not None:
                return True
    except Exception:
        pass
    return False


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
    """drift = |exch_open - ledger_open| / max(exch_open, 1).

    2026-04-26: also returns abs_diff so callers can apply a small-N guard.
    With ~25 open positions, off-by-one is 4% — falsely tripping the halt.
    """
    try:
        exch = len(snap.get('positions', {}))
        led = ledger_stats.get('open_trades_count', 0)
        abs_diff = abs(exch - led)
        denom = max(exch, 1)
        return abs_diff / denom
    except Exception:
        return None


def _update_drift_tier(drift_pct, intent_backlog=0):
    """Step 4 spec §2: multi-tier drift handling with autonomous controls.

    healthy  (<1%)    → normal
    degraded (1-5%)   → reduce entries, skip high-risk coins, faster reconcile
    unsafe   (≥5%)    → halt new entries
    critical (unsafe sustained for N cycles) → emergency flatten authorized

    2026-04-26: degraded tier no longer RESETS healthy_streak. Original logic
    deadlocked the halt: at our position count (~25 concurrent), any 1-position
    discrepancy puts drift at 4% (degraded), which reset the streak and prevented
    halt recovery. Halt clear now requires 3 consecutive NON-UNSAFE cycles.
    Degraded counts as non-unsafe → recovery accumulates.
    """
    if drift_pct is None:
        return

    with _LOCK:
        prev_halt = _METRICS['halt_flag']

        if drift_pct < _DRIFT_HEALTHY_THRESHOLD:
            tier = 'healthy'
            _METRICS['healthy_streak'] += 1
            _METRICS['unsafe_streak'] = 0
            _METRICS['entry_limiter'] = 'full'
            _METRICS['skip_high_risk_coins'] = False
            if _METRICS['halt_flag'] and _METRICS['healthy_streak'] >= _DRIFT_HALT_RECOVERY_CYCLES:
                _METRICS['halt_flag'] = False
                _METRICS['halt_since_ts'] = 0.0
                _METRICS['emergency_flatten_authorized'] = False
                _log(f"✓ HALT CLEARED after {_METRICS['healthy_streak']} healthy cycles")
        elif drift_pct < _DRIFT_UNSAFE_THRESHOLD:
            tier = 'degraded'
            # Recovery counter still ticks while degraded — only unsafe resets it.
            _METRICS['healthy_streak'] += 1
            _METRICS['unsafe_streak'] = 0
            _METRICS['entry_limiter'] = 'reduced'
            _METRICS['skip_high_risk_coins'] = True
            if _METRICS['halt_flag'] and _METRICS['healthy_streak'] >= _DRIFT_HALT_RECOVERY_CYCLES:
                _METRICS['halt_flag'] = False
                _METRICS['halt_since_ts'] = 0.0
                _METRICS['emergency_flatten_authorized'] = False
                _log(f"✓ HALT CLEARED after {_METRICS['healthy_streak']} non-unsafe cycles (last drift {drift_pct*100:.2f}%)")
            _log(f"drift DEGRADED: {drift_pct*100:.2f}% — reduce_entries=on, skip_high_risk=on, faster_reconcile=on")
        else:
            tier = 'unsafe'
            _METRICS['healthy_streak'] = 0
            _METRICS['unsafe_streak'] += 1
            _METRICS['entry_limiter'] = 'halted'
            _METRICS['skip_high_risk_coins'] = True
            if not prev_halt:
                _METRICS['halt_flag'] = True
                _METRICS['halt_since_ts'] = time.time()
                _log(f"⚠ HALT: drift {drift_pct*100:.2f}% >= {_DRIFT_UNSAFE_THRESHOLD*100:.0f}% — blocking new entries")
            if _METRICS['unsafe_streak'] >= _DRIFT_CRITICAL_CYCLES:
                tier = 'critical'
                if not _METRICS['emergency_flatten_authorized']:
                    _METRICS['emergency_flatten_authorized'] = True
                    _log(f"⚠⚠⚠ CRITICAL: drift unsafe for {_METRICS['unsafe_streak']} cycles — "
                         f"emergency flatten authorized (manual trigger via /lifecycle/emergency)")

        # Backpressure — spec §6
        _METRICS['pause_new_intents'] = (intent_backlog > _INTENT_BACKLOG_PAUSE_THRESHOLD)

        _METRICS['drift_tier'] = tier

    # Staged remediation — force snapshot refresh on degraded/unsafe/critical
    if tier in ('degraded', 'unsafe', 'critical'):
        try:
            snap = _deps.get('snapshot')
            if snap:
                snap.force_refresh()
                with _LOCK:
                    _METRICS['forced_snapshot_refreshes'] += 1
        except Exception as e:
            _log(f"forced refresh err: {e}")


def _update_halt_flag(drift_pct):
    """Legacy alias — routes to multi-tier handler."""
    _update_drift_tier(drift_pct)


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

    # Step 4 idempotency ring — prevents double-close race with native fills
    if _in_recent_closed_ring(tid):
        with _LOCK:
            _METRICS['closes_skipped_ring_dedup'] += 1
        return 'ring_dedup'

    # Idempotency check (ledger authoritative)
    if ledger and ledger.is_closed(tid):
        with _LOCK:
            _METRICS['closes_skipped_idempotent'] += 1
        _add_to_recent_closed_ring(tid)  # keep ring warm
        return 'already_closed'

    position = snap.get('positions', {}).get(coin)

    # Min-age guard for fill_match path — same lag protection as _detect_missing_closes.
    # If the trade is fresh, exchange snapshot may not yet show the position,
    # leading to false 'position already gone' detection in Case A.
    if not position:
        try:
            import os
            from datetime import datetime, timezone
            MIN_AGE_TO_CLOSE_SEC = int(os.environ.get('RECONCILE_MIN_CLOSE_AGE_SEC', '60'))
            trade_row = ledger.get_by_trade_id(tid) if hasattr(ledger, 'get_by_trade_id') else None
            if trade_row:
                ts_str = trade_row.get('timestamp', '')
                if ts_str:
                    trade_dt = datetime.fromisoformat(ts_str)
                    if trade_dt.tzinfo is None:
                        trade_dt = trade_dt.replace(tzinfo=timezone.utc)
                    age_sec = (datetime.now(timezone.utc) - trade_dt).total_seconds()
                    if age_sec < MIN_AGE_TO_CLOSE_SEC:
                        with _LOCK:
                            _METRICS['intent_close_too_fresh'] = \
                                _METRICS.get('intent_close_too_fresh', 0) + 1
                        return 'too_fresh_skip'
        except Exception:
            pass  # fall through to normal flow

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
        if ok:
            _add_to_recent_closed_ring(tid, coin=coin)
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

    # If execute_close_fn returned None, the exchange close FAILED.
    # Do NOT mark ledger closed — that's what caused the orphan re-adoption loop.
    # Ledger stays open, position stays on exchange, next cycle retries.
    if fill_px is None:
        with _LOCK:
            _METRICS['errors_total'] += 1
            _METRICS['last_error'] = f"execute_close returned None {coin}"
        _log(f"EXECUTE FAILED {coin} trade_id={tid[:8]} — skipping ledger write (retry next cycle)")
        return 'execute_failed_no_ledger_write'

    # Brief settle delay before reading fill
    time.sleep(0.3)
    # Refresh snapshot for fresh fills
    try:
        _deps['snapshot'].force_refresh()
    except Exception:
        pass
    fresh = _deps['snapshot'].get()

    # CRITICAL VERIFICATION: confirm exchange position is actually gone.
    # If still present, do NOT mark ledger closed. Retry next cycle.
    # This is the backstop against the HMSTR-12-closes-in-4-min loop.
    still_on_exchange = fresh.get('positions', {}).get(coin)
    if still_on_exchange:
        with _LOCK:
            _METRICS['errors_total'] += 1
            _METRICS['last_error'] = f"post-close verify failed {coin}"
        _log(f"VERIFY FAILED {coin} trade_id={tid[:8]} — position still on exchange after close attempt "
             f"(size={still_on_exchange.get('size')}) — NOT writing ledger close")
        return 'post_close_verify_failed'

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
    if ok:
        _add_to_recent_closed_ring(tid, coin=coin)
    return 'closed_via_intent'


def _detect_orphans(snap, ledger_stats):
    """Spec §5: Orphan repair engine.
    If exchange has position but ledger doesn't, reconciler ADOPTS (authoritatively).
    Prevents ghost exposure and untracked risk.

    Note: precog.py main loop also does adoption; both paths now use
    latest_open_trade_id_for_coin() first to prevent duplicates (Step 4 fix).
    """
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
        # Skip coins we just closed — exchange snapshot lag would re-adopt otherwise
        if _coin_recently_closed(coin):
            _log(f"ORPHAN SKIP: {coin} was closed in last {_RECENT_CLOSED_COIN_TTL_SEC}s — snapshot lag")
            continue
        pos = snap['positions'].get(coin, {})
        side_char = pos.get('side', 'L')  # 'L' or 'S' from snapshot
        entry_px = pos.get('entry', 0) or 0
        try:
            new_tid = ledger.new_trade_id()
            ledger.append_entry(
                coin=coin,
                side='BUY' if side_char == 'L' else 'SELL',
                entry_price=entry_px,
                engine='RECONCILED',
                source='reconcile_orphan_adopt',
                cloid=None,
                trade_id=new_tid,
            )
            _log(f"ORPHAN ADOPTED: {coin} trade_id={new_tid[:8]} side={side_char} entry={entry_px}")
            with _LOCK:
                _METRICS['orphans_repaired_authoritatively'] += 1
                _METRICS['orphans_adopted'] += 1
        except Exception as e:
            _log(f"orphan adopt err {coin}: {e}")


def _detect_missing_closes(snap, authoritative):
    """Trades open in ledger but position absent on exchange.

    Min-age guard: skip trades opened in the last MIN_AGE_TO_CLOSE_SEC seconds.
    HL exchange snapshot can lag the ledger by 30-60s after entry — closing
    immediately would cause the false 'missing close → orphan adopt' churn
    pattern that was polluting the trade log.
    """
    import os
    from datetime import datetime, timezone
    MIN_AGE_TO_CLOSE_SEC = int(os.environ.get('RECONCILE_MIN_CLOSE_AGE_SEC', '60'))
    ledger = _deps['ledger']
    close_trade_fn = _deps['close_trade_fn']
    if not ledger:
        return
    try:
        ledger_opens = ledger.open_trades()
    except Exception:
        return
    exch_coins = set(snap.get('positions', {}).keys())
    now_dt = datetime.now(timezone.utc)
    for trade in ledger_opens:
        coin = trade.get('coin', '')
        tid = trade.get('trade_id', '')
        if not coin or not tid:
            continue
        if coin in exch_coins:
            continue
        # Min-age guard — newly-entered trades may not yet be in exchange snapshot
        try:
            ts_str = trade.get('timestamp', '')
            if ts_str:
                trade_dt = datetime.fromisoformat(ts_str)
                if trade_dt.tzinfo is None:
                    trade_dt = trade_dt.replace(tzinfo=timezone.utc)
                age_sec = (now_dt - trade_dt).total_seconds()
                if age_sec < MIN_AGE_TO_CLOSE_SEC:
                    with _LOCK:
                        _METRICS['missing_close_too_fresh'] = \
                            _METRICS.get('missing_close_too_fresh', 0) + 1
                    continue
        except Exception:
            pass  # fall through if timestamp parse fails
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
            close_size=float(fill['sz']) if fill and fill.get('sz') else None,
        )
        with _LOCK:
            if ok:
                _METRICS['missing_closes_recorded'] += 1
                if fill:
                    _METRICS['exchange_fills_matched'] += 1
                else:
                    _METRICS['exchange_fills_unmatched'] += 1
        if ok:
            _add_to_recent_closed_ring(tid, coin=coin)


def _cycle():
    """One reconcile cycle. Spec §7 order:
       1. refresh snapshot
       2. compute drift
       3. apply drift controls
       4. process intents
       5. repair orphans
       6. repair missing closes (reconcile_lifecycle)
    """
    t0 = time.time()
    last_ts_before = _METRICS.get('last_cycle_ts', 0) or 0
    if last_ts_before:
        lag_before = t0 - last_ts_before
        with _LOCK:
            _METRICS['reconciler_lag_s'] = round(lag_before, 2)

    authoritative = _is_authoritative()
    cycle_had_error = False
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

        # 1. Refresh snapshot (implicitly — daemon runs independently, but we can poke)
        snap = snapshot.get()
        if snap.get('stale'):
            # Try forced refresh before giving up
            try:
                snapshot.force_refresh()
                snap = snapshot.get()
            except Exception: pass

        if snap.get('stale'):
            with _LOCK:
                _METRICS['closes_skipped_stale_snapshot'] += 1
                # Stale snapshot counts as halt condition
                _METRICS['halt_flag'] = True
            _log(f"SKIP cycle: snapshot stale (age={snap.get('age_sec'):.1f}s) — halt asserted")
            return

        ledger_stats = ledger.stats()

        # 2026-04-26: auto-dedupe duplicate open trade_ids. The 3-source
        # _in_position fix landed earlier this session, but pre-fix duplicate
        # entries persist in the ledger and inflate open_trades_count, driving
        # drift artificially high (4-8% degraded → entry_limiter='reduced' →
        # all trades fire at 0.5x size). Auto-dedupe is safe: keeps earliest
        # event_seq trade per coin, closes the rest with reason=
        # 'reconcile_duplicate_entry'. No real divergence is hidden because
        # exchange position count is the authoritative side; ledger duplicates
        # are pure bookkeeping noise.
        try:
            if hasattr(ledger, 'dedupe_open_trades'):
                _dd = ledger.dedupe_open_trades()
                if _dd and _dd.get('dupes_closed', 0) > 0:
                    _log(f"AUTO-DEDUPE: closed {_dd['dupes_closed']} duplicate open trades "
                         f"across {_dd['coins_affected']} coins — {_dd.get('details', [])[:5]}")
                    # Re-read stats so this cycle's drift reflects the cleanup
                    ledger_stats = ledger.stats()
        except Exception as _de:
            _log(f"auto-dedupe err: {_de}")

        # 2. Compute drift + get intent backlog
        drift_pct = _compute_drift(snap, ledger_stats)
        intent_backlog = iq.status().get('queue_depth', 0)
        with _LOCK:
            _METRICS['last_drift_pct'] = drift_pct

        # 3. Apply drift controls (updates halt_flag, entry_limiter, skip_high_risk, etc.)
        _update_drift_tier(drift_pct, intent_backlog)

        # 4. Process intents (spec §7)
        intents = iq.drain(max_items=_MAX_INTENTS_PER_CYCLE)
        if intents:
            by_tid = {}
            for intent in intents:
                tid = _resolve_trade_id(intent) or f"_no_tid_{intent.get('coin')}"
                by_tid[tid] = intent
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

        # 5. Repair orphans (authoritative per spec §5)
        _detect_orphans(snap, ledger_stats)

        # 6. Repair missing closes (reconcile lifecycle)
        _detect_missing_closes(snap, authoritative)

        with _LOCK:
            _METRICS['last_cycle_ts'] = time.time()
            _METRICS['last_cycle_duration_ms'] = int((time.time() - t0) * 1000)
    except Exception as e:
        cycle_had_error = True
        with _LOCK:
            _METRICS['errors_total'] += 1
            _METRICS['last_error'] = f"cycle: {e}"
        _log(f"cycle err: {e}")
    finally:
        _record_cycle_error(cycle_had_error)


def _run():
    _log(f"daemon started (interval={_INTERVAL_SEC}s, mode={'authoritative' if _is_authoritative() else 'observe'})")
    while not _STOP.is_set():
        _cycle()
        # Spec §2B: increase_reconcile_frequency when degraded
        with _LOCK:
            tier = _METRICS.get('drift_tier', 'unknown')
        interval = _INTERVAL_SEC_DEGRADED if tier in ('degraded', 'unsafe', 'critical') else _INTERVAL_SEC
        _STOP.wait(interval)


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
        if _METRICS['halt_flag']:
            return True
        last_ts = _METRICS.get('last_cycle_ts') or 0
        if last_ts and (time.time() - last_ts > _RECONCILER_STALE_SEC):
            return True
        return False


def entry_limiter():
    """Spec §2B: 'full' | 'reduced' | 'halted'. process() reads this."""
    with _LOCK:
        if _METRICS['halt_flag']:
            return 'halted'
        return _METRICS.get('entry_limiter', 'full')


def should_skip_high_risk():
    """Spec §2B.skip_high_risk_coins."""
    with _LOCK:
        return _METRICS.get('skip_high_risk_coins', False)


def should_pause_intents():
    """Spec §6 backpressure."""
    with _LOCK:
        return _METRICS.get('pause_new_intents', False)


def is_stall_emergency():
    """Reconciler has not cycled in _RECONCILER_EMERGENCY_STALL_SEC seconds."""
    with _LOCK:
        last_ts = _METRICS.get('last_cycle_ts') or 0
        if not last_ts:
            return False
        return (time.time() - last_ts) > _RECONCILER_EMERGENCY_STALL_SEC


def emergency_reset(action):
    """Admin controls for emergency endpoint.

    action:
      'clear_halt'       — force-clear halt flag and unsafe streak
      'clear_breaker'    — reset circuit breaker
      'clear_emergency'  — clear emergency_flatten_authorized
      'clear_ring'       — empty recent-closed ring buffer
      'clear_all'        — all of the above
    """
    global _cycle_error_flags, _recent_closed_trade_ids
    with _LOCK:
        if action in ('clear_halt', 'clear_all'):
            _METRICS['halt_flag'] = False
            _METRICS['halt_since_ts'] = 0.0
            _METRICS['unsafe_streak'] = 0
            _METRICS['healthy_streak'] = 0
            _log("ADMIN: halt flag cleared")
        if action in ('clear_breaker', 'clear_all'):
            _METRICS['circuit_breaker_tripped'] = False
            _METRICS['circuit_breaker_tripped_ts'] = 0.0
            _METRICS['circuit_breaker_error_rate'] = 0.0
            _cycle_error_flags = []
            _log("ADMIN: circuit breaker reset")
        if action in ('clear_emergency', 'clear_all'):
            _METRICS['emergency_flatten_authorized'] = False
            _log("ADMIN: emergency flatten authorization cleared")
        if action in ('clear_ring', 'clear_all'):
            _recent_closed_trade_ids = []
            _METRICS['recent_closed_ring_size'] = 0
            _log("ADMIN: recent-closed ring buffer cleared")
    return {'action': action, 'ok': True, 'at': time.time()}


def status():
    """For /lifecycle endpoint."""
    with _LOCK:
        m = dict(_METRICS)
    m['authoritative'] = _is_authoritative()
    m['daemon_alive'] = _DAEMON.is_alive() if _DAEMON else False
    m['interval_sec'] = _INTERVAL_SEC
    last_ts = m.get('last_cycle_ts') or 0
    m['cycle_stale'] = (time.time() - last_ts > _RECONCILER_STALE_SEC) if last_ts else True
    m['stall_emergency'] = (time.time() - last_ts > _RECONCILER_EMERGENCY_STALL_SEC) if last_ts else False
    m['thresholds'] = {
        'drift_degraded': _DRIFT_DEGRADED_THRESHOLD,
        'drift_unsafe': _DRIFT_UNSAFE_THRESHOLD,
        'drift_critical_cycles': _DRIFT_CRITICAL_CYCLES,
        'cb_error_rate_trip': _CB_ERROR_RATE_TRIP,
        'stall_sec': _RECONCILER_STALE_SEC,
        'emergency_stall_sec': _RECONCILER_EMERGENCY_STALL_SEC,
        'intent_backlog_pause': _INTENT_BACKLOG_PAUSE_THRESHOLD,
        'interval_sec_normal': _INTERVAL_SEC,
        'interval_sec_degraded': _INTERVAL_SEC_DEGRADED,
    }
    return m
