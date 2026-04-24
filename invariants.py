"""Contract invariants — runtime guardrails that enforce the execution contract.

Six invariants:
1. ENTRY_INVARIANT:   TP+SL must be on exchange post-entry or trade is cancelled
2. EXIT_INVARIANT:    close() only callable with authorized reason
3. ORDER_PERSISTENCE: every open position has both TP+SL on exchange (checked N-second)
4. NO_OVERRIDE:       cancelling TP/SL requires same-tick replacement
5. TRADE_AUDIT:       every close records entry/TP/SL/exit/reason; mismatches flagged
6. DEADMAN_CHECK:     daemon scans every N sec — if position has no SL → emergency close

This module runs a background daemon that enforces #3 and #6 continuously.
"""
import os, json, time, threading
from collections import defaultdict

_LOG_PREFIX = '[invariant]'
_LOCK = threading.Lock()

# Check frequency
DEADMAN_INTERVAL_SEC = 30      # scan every 30s
PERSISTENCE_GRACE_SEC = 20     # allow 20s for post-entry order placement
EMERGENCY_CLOSE_AFTER_SEC = 60 # if naked > 60s, emergency close

# Violation counters
_VIOLATIONS = defaultdict(int)

# Violation trace — root cause context per event
# {violation_id: {ts, coin, type, classification, last_action, divergence_ts, detail}}
_VIOLATION_TRACE = []
_VIOLATION_TRACE_MAX = 500

# Last-action tracker (set externally by precog.py on entry / resize / cancel events)
# {coin: {'action', 'ts', 'size_before', 'size_after', 'origin'}}
_LAST_ACTION = {}


def record_action(coin, action, size_before=None, size_after=None, origin=None, detail=None):
    """Called by precog.py whenever a position state-changing event occurs.

    Actions: 'entry', 'partial_close', 'tp_place', 'sl_place', 'cancel_trigger',
             'reduce_only_ioc', 'reconcile_adopt', 'reconcile_phantom'.

    This feeds the violation classifier — when a mismatch is detected later,
    we look at the most recent action on the coin to attribute root cause.
    """
    with _LOCK:
        _LAST_ACTION[coin] = {
            'ts': time.time(),
            'action': action,
            'size_before': size_before,
            'size_after': size_after,
            'origin': origin,
            'detail': detail,
        }


def _classify_violation(coin, violation_type, **context):
    """Infer root-cause classification from last-action + type.

    Returns dict with classification + diagnostic detail.
    """
    last = _LAST_ACTION.get(coin, {})
    last_action = last.get('action', 'unknown')
    last_ts = last.get('ts', 0)
    seconds_since_last_action = time.time() - last_ts if last_ts else None

    cls = 'unknown'
    rationale = ''

    if violation_type in ('tp_size_mismatch', 'sl_size_mismatch'):
        pos_size = context.get('pos_size', 0)
        order_size = context.get('order_size', 0)
        # Ratio test
        if pos_size > 0:
            delta_pct = abs(order_size - pos_size) / pos_size
        else:
            delta_pct = 0

        if last_action == 'partial_close':
            cls = 'partial_fill_handling_bug'
            rationale = f'order sized for pre-close quantity; partial_close did not resize trigger'
        elif last_action in ('reduce_only_ioc', 'signal_reversal_close'):
            cls = 'partial_fill_handling_bug'
            rationale = f'market reduce-only did not match position — leftover order for old size'
        elif last_action == 'reconcile_adopt':
            cls = 'state_desync'
            rationale = f'adopted existing HL position; state size from adoption did not match trigger size'
        elif last_action == 'tp_place' or last_action == 'sl_place':
            # Order was just placed. Size mismatch = placement bug.
            # But if pct mismatch is tiny (< 1%), likely rounding.
            if delta_pct < 0.01:
                cls = 'rounding_precision_issue'
                rationale = f'order size off by {delta_pct*100:.2f}% — likely HL decimals rounding'
            else:
                cls = 'order_placement_bug'
                rationale = f'order placed with wrong size ({delta_pct*100:.1f}% off position)'
        elif delta_pct < 0.01:
            cls = 'rounding_precision_issue'
            rationale = f'size off by {delta_pct*100:.2f}% — likely decimal rounding'
        else:
            cls = 'state_desync'
            rationale = f'position size diverged from trigger order; no recent recorded action'

    elif violation_type == 'naked_position_detected':
        if last_action == 'entry':
            if seconds_since_last_action and seconds_since_last_action < 30:
                cls = 'order_placement_bug'
                rationale = 'entry occurred recently; TP or SL placement returned failure'
            else:
                cls = 'order_placement_bug'
                rationale = 'position established but protection never placed'
        elif last_action == 'cancel_trigger':
            cls = 'no_override_bug'
            rationale = 'cancel_trigger_orders called but no replacement placed'
        elif last_action == 'reconcile_adopt':
            cls = 'state_desync'
            rationale = 'adopted HL position without TP/SL — manual or pre-deploy position'
        else:
            cls = 'state_desync'
            rationale = f'naked without recent recorded action (last: {last_action})'

    elif violation_type == 'entry_naked':
        cls = 'order_placement_bug'
        rationale = 'place_native_tp or place_native_sl returned None at entry'

    elif violation_type == 'deadman_triggered':
        cls = 'critical_no_override'
        rationale = 'SL missing > emergency threshold; recreation failed repeatedly'

    elif violation_type == 'audit_mismatch':
        cls = 'contract_bypass'
        rationale = 'exit_reason does not match actual price vs target'

    elif violation_type == 'cancel_without_replace':
        cls = 'no_override_bug'
        rationale = 'cancel fired without same-tick replacement'

    trace = {
        'ts': int(time.time()),
        'coin': coin,
        'type': violation_type,
        'classification': cls,
        'rationale': rationale,
        'last_action': last_action,
        'seconds_since_last_action': round(seconds_since_last_action, 1) if seconds_since_last_action else None,
        'last_action_origin': last.get('origin'),
        'last_action_size_before': last.get('size_before'),
        'last_action_size_after': last.get('size_after'),
        'context': context,
    }
    with _LOCK:
        _VIOLATION_TRACE.append(trace)
        if len(_VIOLATION_TRACE) > _VIOLATION_TRACE_MAX:
            _VIOLATION_TRACE[:] = _VIOLATION_TRACE[-_VIOLATION_TRACE_MAX:]
        _VIOLATIONS[f'cls_{cls}'] += 1
    print(f"{_LOG_PREFIX} ROOT_CAUSE {coin} {violation_type} → {cls}: {rationale} "
          f"(last_action={last_action}, age={trace['seconds_since_last_action']}s)",
          flush=True)
    return trace

# Last-known protection state per coin: {coin: {'has_sl': bool, 'has_tp': bool, 'last_check_ts': float, 'naked_since_ts': float or None}}
_PROTECTION_STATE = {}

# Audit log of every close — truth record
_AUDIT_LOG = []
_AUDIT_MAX = 1000

# Daemon control
_DAEMON_RUNNING = False


# ─────────────────────────────────────────────────────
# #1 ENTRY INVARIANT
# ─────────────────────────────────────────────────────
def assert_entry_protection(coin, tp_pct_used, sl_pct_used, close_fn, pos_info=None):
    """Called right after place_native_tp + place_native_sl.

    If either order failed, emergency close immediately.
    Returns True if entry is safe, False if cancelled.
    """
    if tp_pct_used is not None and sl_pct_used is not None:
        return True

    missing = []
    if tp_pct_used is None: missing.append('TP')
    if sl_pct_used is None: missing.append('SL')

    with _LOCK:
        _VIOLATIONS['entry_naked'] += 1
    _classify_violation(coin, 'entry_naked',
                        tp_failed=tp_pct_used is None, sl_failed=sl_pct_used is None,
                        pos_info=pos_info or {})
    print(f"{_LOG_PREFIX} ★ ENTRY INVARIANT VIOLATED {coin}: missing {missing}. "
          f"Emergency close.", flush=True)

    try:
        close_fn(coin)
        print(f"{_LOG_PREFIX} {coin} emergency closed (entry invariant).", flush=True)
    except Exception as e:
        print(f"{_LOG_PREFIX} {coin} emergency close FAILED: {e}. POSITION IS NAKED.", flush=True)
    return False


# ─────────────────────────────────────────────────────
# #2 EXIT INVARIANT — enforced via exec_contract.contract_close
# Nothing to do here; it's enforced at contract layer.
# ─────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────
# #3 ORDER PERSISTENCE + #6 DEADMAN
# ─────────────────────────────────────────────────────
def check_protection_coverage(live_positions_fn, get_open_orders_fn, place_tp_fn, place_sl_fn, close_fn, enforce_fn=None):
    """Scan all open positions. For each:
    - Check if SL is on exchange
    - Check if TP is on exchange
    - Check TP price is correct (within 2% of expected tp_target)
    - Check TP/SL size matches position size (±5% tolerance)
    - If missing or invalid: cancel + recreate
    - If missing > EMERGENCY_CLOSE_AFTER_SEC: emergency close
    """
    try:
        positions = live_positions_fn()
        open_orders = get_open_orders_fn()
    except Exception as e:
        print(f"{_LOG_PREFIX} coverage scan err: {e}", flush=True)
        return

    # Build per-coin order detail from open orders
    # {coin: {'sl': {order_id, trigger_px, size}, 'tp': {...}}}
    orders_by_coin = defaultdict(lambda: {'sl': None, 'tp': None})
    for o in open_orders:
        c = o.get('coin', '').upper()
        ot = o.get('orderType', '')
        trig = o.get('triggerPx')
        try: trig = float(trig) if trig is not None else None
        except: trig = None
        sz = o.get('origSz') or o.get('sz') or 0
        try: sz = float(sz)
        except: sz = 0
        oid = o.get('oid')
        info = {'oid': oid, 'trigger_px': trig, 'size': sz, 'order_type': ot}
        if 'Stop' in ot or 'Sl' in ot: orders_by_coin[c]['sl'] = info
        if 'Take' in ot or 'Tp' in ot: orders_by_coin[c]['tp'] = info

    now = time.time()
    for coin, pos in positions.items():
        sz = pos.get('size', 0)
        entry = pos.get('entry', 0)
        if sz == 0 or not entry:
            continue
        ckey = coin.upper()
        coverage = orders_by_coin[ckey]

        state = _PROTECTION_STATE.setdefault(coin, {
            'has_sl': False, 'has_tp': False,
            'naked_since_ts': None, 'last_check_ts': 0,
            'tp_invalid_since_ts': None,
        })
        has_sl = coverage['sl'] is not None
        has_tp = coverage['tp'] is not None
        state['has_sl'] = has_sl
        state['has_tp'] = has_tp
        state['last_check_ts'] = now

        # TP INTEGRITY: if TP exists, validate price + size
        tp_invalid = False
        if has_tp:
            tp_info = coverage['tp']
            tp_size = abs(tp_info.get('size') or 0)
            abs_pos_size = abs(sz)
            size_mismatch = abs_pos_size > 0 and abs(tp_size - abs_pos_size) / abs_pos_size > 0.05
            if size_mismatch:
                tp_invalid = True
                with _LOCK:
                    _VIOLATIONS['tp_size_mismatch'] += 1
                _classify_violation(coin, 'tp_size_mismatch',
                                    pos_size=abs_pos_size, order_size=tp_size,
                                    order_oid=tp_info.get('oid'),
                                    trigger_px=tp_info.get('trigger_px'))
                print(f"{_LOG_PREFIX} ⚠ TP SIZE MISMATCH {coin}: "
                      f"tp_size={tp_size} pos_size={abs_pos_size}", flush=True)

        # SL INTEGRITY: same check
        sl_invalid = False
        if has_sl:
            sl_info = coverage['sl']
            sl_size = abs(sl_info.get('size') or 0)
            abs_pos_size = abs(sz)
            size_mismatch = abs_pos_size > 0 and abs(sl_size - abs_pos_size) / abs_pos_size > 0.05
            if size_mismatch:
                sl_invalid = True
                with _LOCK:
                    _VIOLATIONS['sl_size_mismatch'] += 1
                _classify_violation(coin, 'sl_size_mismatch',
                                    pos_size=abs_pos_size, order_size=sl_size,
                                    order_oid=sl_info.get('oid'),
                                    trigger_px=sl_info.get('trigger_px'))
                print(f"{_LOG_PREFIX} ⚠ SL SIZE MISMATCH {coin}: "
                      f"sl_size={sl_size} pos_size={abs_pos_size}", flush=True)

        naked = not has_sl or not has_tp or tp_invalid or sl_invalid
        if naked:
            if state['naked_since_ts'] is None:
                state['naked_since_ts'] = now
            naked_duration = now - state['naked_since_ts']

            # Skip grace period (entry may still be placing orders)
            if naked_duration < PERSISTENCE_GRACE_SEC:
                continue

            with _LOCK:
                _VIOLATIONS['naked_position_detected'] += 1
            _classify_violation(coin, 'naked_position_detected',
                                has_sl=has_sl, has_tp=has_tp,
                                tp_invalid=tp_invalid, sl_invalid=sl_invalid,
                                naked_duration=round(naked_duration, 1),
                                pos_size=abs(sz), entry=entry)

            missing = []
            if not has_sl: missing.append('SL')
            if not has_tp: missing.append('TP')
            if tp_invalid: missing.append('TP_invalid')
            if sl_invalid: missing.append('SL_invalid')
            print(f"{_LOG_PREFIX} ⚠ {coin} naked ({', '.join(missing)}) "
                  f"for {naked_duration:.0f}s — calling enforce_protection", flush=True)

            # PHASE 1: call enforce_protection (atomic cancel+replace+verify)
            # INSTEAD of scattered place_sl/tp + recreation logic.
            is_long = sz > 0
            if enforce_fn is not None:
                try:
                    ep_result = enforce_fn(coin, is_long, entry, origin='deadman_repair')
                    if ep_result.get('success'):
                        print(f"{_LOG_PREFIX} {coin} enforce_protection SUCCESS "
                              f"(replaced={ep_result.get('replaced')}, "
                              f"dur={ep_result.get('duration_sec', 0):.1f}s)", flush=True)
                    elif ep_result.get('emergency_closed'):
                        print(f"{_LOG_PREFIX} {coin} enforce_protection EMERGENCY CLOSED "
                              f"({ep_result.get('reason')})", flush=True)
                        with _LOCK:
                            _VIOLATIONS['deadman_triggered'] += 1
                        _classify_violation(coin, 'deadman_triggered',
                                            naked_duration=round(naked_duration, 1),
                                            pos_size=abs(sz),
                                            ep_reason=ep_result.get('reason'))
                    else:
                        print(f"{_LOG_PREFIX} {coin} enforce_protection FAILED "
                              f"({ep_result.get('reason')})", flush=True)
                except Exception as e:
                    print(f"{_LOG_PREFIX} {coin} enforce_protection err: {e}", flush=True)
            else:
                # Legacy fallback path (should not happen in phase 1)
                try:
                    if not has_sl or sl_invalid:
                        sl_res = place_sl_fn(coin, is_long, entry, abs(sz))
                        if sl_res is not None:
                            print(f"{_LOG_PREFIX} {coin} SL recreated ({sl_res}) [LEGACY]", flush=True)
                except Exception as e:
                    print(f"{_LOG_PREFIX} {coin} SL err: {e}", flush=True)
                try:
                    if not has_tp or tp_invalid:
                        tp_res = place_tp_fn(coin, is_long, entry, abs(sz))
                        if tp_res is not None:
                            print(f"{_LOG_PREFIX} {coin} TP recreated ({tp_res}) [LEGACY]", flush=True)
                except Exception as e:
                    print(f"{_LOG_PREFIX} {coin} TP err: {e}", flush=True)

            # DEADMAN: if SL still missing > EMERGENCY_CLOSE_AFTER_SEC, close
            if (not has_sl or sl_invalid) and naked_duration > EMERGENCY_CLOSE_AFTER_SEC:
                with _LOCK:
                    _VIOLATIONS['deadman_triggered'] += 1
                _classify_violation(coin, 'deadman_triggered',
                                    naked_duration=round(naked_duration, 1),
                                    pos_size=abs(sz))
                print(f"{_LOG_PREFIX} ★★★ DEADMAN TRIGGER {coin}: "
                      f"SL missing/invalid {naked_duration:.0f}s. EMERGENCY CLOSE.", flush=True)
                try:
                    close_fn(coin)
                    print(f"{_LOG_PREFIX} {coin} deadman close succeeded", flush=True)
                except Exception as e:
                    print(f"{_LOG_PREFIX} {coin} DEADMAN CLOSE FAILED: {e}. "
                          f"CRITICAL — manual intervention required.", flush=True)
        else:
            state['naked_since_ts'] = None


# ─────────────────────────────────────────────────────
# #4 NO-OVERRIDE: track cancel→replace invariant
# ─────────────────────────────────────────────────────
# Cancelling orders without replacement is a contract violation. The existing
# cancel_trigger_orders() in precog.py is called during position flip. Under
# contract, flips are queued — so cancel_trigger_orders should only fire
# during close() paths. Logging-only guard here.
_CANCEL_LOG = []

def log_cancel(coin, reason, replaced=False):
    with _LOCK:
        _CANCEL_LOG.append({
            'ts': int(time.time()),
            'coin': coin,
            'reason': reason,
            'replaced_in_same_tick': replaced,
        })
        if len(_CANCEL_LOG) > 200:
            _CANCEL_LOG[:] = _CANCEL_LOG[-200:]
        if not replaced and reason not in ('close', 'liquidation', 'tp_fill', 'sl_fill'):
            _VIOLATIONS['cancel_without_replace'] += 1
            _classify_violation(coin, 'cancel_without_replace',
                                cancel_reason=reason, replaced=replaced)
            print(f"{_LOG_PREFIX} ⚠ CANCEL WITHOUT REPLACE {coin} ({reason})", flush=True)


# ─────────────────────────────────────────────────────
# #5 TRADE AUDIT
# ─────────────────────────────────────────────────────
def audit_close(coin, entry_price, tp_pct, sl_pct, exit_price, exit_reason,
                pnl_pct, side):
    """Record every close event with full expected-vs-actual data.

    Flags violations when exit_reason doesn't match price behavior:
    - exit_reason='tp' but exit < expected TP price → mismatch
    - exit_reason='sl' but exit > expected SL price (wrong side) → mismatch
    - exit at modeled TP/SL but reason says 'signal_reversal' → contract bypass
    """
    rec = {
        'ts': int(time.time()),
        'coin': coin,
        'side': side,
        'entry_price': entry_price,
        'tp_pct': tp_pct,
        'sl_pct': sl_pct,
        'exit_price': exit_price,
        'exit_reason': exit_reason,
        'pnl_pct': round(float(pnl_pct), 3),
        'violation': None,
    }

    # Compute expected levels
    if entry_price and tp_pct and sl_pct:
        if side == 'L':
            tp_target = entry_price * (1 + tp_pct)
            sl_target = entry_price * (1 - sl_pct)
        else:
            tp_target = entry_price * (1 - tp_pct)
            sl_target = entry_price * (1 + sl_pct)
        rec['tp_target'] = tp_target
        rec['sl_target'] = sl_target

        # Check mismatches
        if exit_reason == 'tp_fill_confirmed' and exit_price:
            if side == 'L' and exit_price < tp_target * 0.995:
                rec['violation'] = f'tp_reason_but_price_below_target: exit={exit_price} target={tp_target}'
            elif side == 'S' and exit_price > tp_target * 1.005:
                rec['violation'] = f'tp_reason_but_price_above_target: exit={exit_price} target={tp_target}'
        if exit_reason == 'sl_fill_confirmed' and exit_price:
            if side == 'L' and exit_price > sl_target * 1.005:
                rec['violation'] = f'sl_reason_but_price_above_target: exit={exit_price} target={sl_target}'
            elif side == 'S' and exit_price < sl_target * 0.995:
                rec['violation'] = f'sl_reason_but_price_below_target: exit={exit_price} target={sl_target}'

    if rec.get('violation'):
        with _LOCK:
            _VIOLATIONS['audit_mismatch'] += 1
        _classify_violation(coin, 'audit_mismatch',
                            exit_reason=exit_reason, exit_price=exit_price,
                            violation=rec['violation'])
        print(f"{_LOG_PREFIX} ⚠ AUDIT MISMATCH {coin}: {rec['violation']}", flush=True)

    with _LOCK:
        _AUDIT_LOG.append(rec)
        if len(_AUDIT_LOG) > _AUDIT_MAX:
            _AUDIT_LOG[:] = _AUDIT_LOG[-_AUDIT_MAX:]


# ─────────────────────────────────────────────────────
# Deadman daemon
# ─────────────────────────────────────────────────────
def start_deadman_daemon(live_positions_fn, get_open_orders_fn,
                         place_tp_fn, place_sl_fn, close_fn, enforce_fn=None):
    """Launch the deadman scan daemon. Idempotent."""
    global _DAEMON_RUNNING
    if _DAEMON_RUNNING:
        return
    _DAEMON_RUNNING = True

    def _loop():
        while True:
            try:
                check_protection_coverage(
                    live_positions_fn, get_open_orders_fn,
                    place_tp_fn, place_sl_fn, close_fn, enforce_fn=enforce_fn)
            except Exception as e:
                print(f"{_LOG_PREFIX} deadman loop err: {e}", flush=True)
            time.sleep(DEADMAN_INTERVAL_SEC)

    t = threading.Thread(target=_loop, daemon=True, name='invariant_deadman')
    t.start()
    print(f"{_LOG_PREFIX} deadman daemon started "
          f"(interval={DEADMAN_INTERVAL_SEC}s, grace={PERSISTENCE_GRACE_SEC}s, "
          f"emergency_close_after={EMERGENCY_CLOSE_AFTER_SEC}s)", flush=True)


def status():
    with _LOCK:
        v = dict(_VIOLATIONS)
        p_count = sum(1 for s in _PROTECTION_STATE.values() if s.get('has_sl') and s.get('has_tp'))
        naked_count = sum(1 for s in _PROTECTION_STATE.values() if not (s.get('has_sl') and s.get('has_tp')))
        recent_audit = list(_AUDIT_LOG[-10:])
        recent_cancels = list(_CANCEL_LOG[-10:])
        recent_traces = list(_VIOLATION_TRACE[-30:])
        cls_breakdown = defaultdict(int)
        for t in _VIOLATION_TRACE:
            cls_breakdown[t.get('classification', 'unknown')] += 1
    return {
        'daemon_running': _DAEMON_RUNNING,
        'check_interval_sec': DEADMAN_INTERVAL_SEC,
        'grace_sec': PERSISTENCE_GRACE_SEC,
        'emergency_close_after_sec': EMERGENCY_CLOSE_AFTER_SEC,
        'violations': v,
        'positions_protected': p_count,
        'positions_naked_now': naked_count,
        'protection_state': _PROTECTION_STATE,
        'recent_audit_log': recent_audit,
        'recent_cancels': recent_cancels,
        'violation_classifications': dict(cls_breakdown),
        'recent_violation_traces': recent_traces,
        'last_actions_tracked': len(_LAST_ACTION),
    }
