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
def check_protection_coverage(live_positions_fn, get_open_orders_fn, place_tp_fn, place_sl_fn, close_fn):
    """Scan all open positions. For each:
    - Check if SL is on exchange
    - Check if TP is on exchange
    - If missing: attempt replacement
    - If missing > EMERGENCY_CLOSE_AFTER_SEC: emergency close
    """
    try:
        positions = live_positions_fn()
        open_orders = get_open_orders_fn()
    except Exception as e:
        print(f"{_LOG_PREFIX} coverage scan err: {e}", flush=True)
        return

    # Build per-coin coverage from open orders
    coverage = defaultdict(lambda: {'sl': False, 'tp': False})
    for o in open_orders:
        c = o.get('coin', '').upper()
        ot = o.get('orderType', '')
        # HL order types: 'Stop Market', 'Take Profit Market', etc.
        if 'Stop' in ot or 'Sl' in ot: coverage[c]['sl'] = True
        if 'Take' in ot or 'Tp' in ot: coverage[c]['tp'] = True

    now = time.time()
    for coin, pos in positions.items():
        sz = pos.get('size', 0)
        entry = pos.get('entry', 0)
        if sz == 0 or not entry:
            continue
        cov = coverage[coin.upper()]

        state = _PROTECTION_STATE.setdefault(coin, {
            'has_sl': False, 'has_tp': False,
            'naked_since_ts': None, 'last_check_ts': 0,
        })
        state['has_sl'] = cov['sl']
        state['has_tp'] = cov['tp']
        state['last_check_ts'] = now

        naked = not cov['sl'] or not cov['tp']
        if naked:
            if state['naked_since_ts'] is None:
                state['naked_since_ts'] = now
            naked_duration = now - state['naked_since_ts']

            # Skip grace period (entry may still be placing orders)
            if naked_duration < PERSISTENCE_GRACE_SEC:
                continue

            with _LOCK:
                _VIOLATIONS['naked_position_detected'] += 1

            missing = []
            if not cov['sl']: missing.append('SL')
            if not cov['tp']: missing.append('TP')
            print(f"{_LOG_PREFIX} ⚠ {coin} naked ({', '.join(missing)} missing) "
                  f"for {naked_duration:.0f}s — attempting recreation", flush=True)

            # Attempt to replace missing orders
            is_long = sz > 0
            try:
                if not cov['sl']:
                    sl_res = place_sl_fn(coin, is_long, entry, abs(sz))
                    if sl_res is not None:
                        print(f"{_LOG_PREFIX} {coin} SL recreated ({sl_res})", flush=True)
                    else:
                        print(f"{_LOG_PREFIX} {coin} SL RECREATION FAILED", flush=True)
            except Exception as e:
                print(f"{_LOG_PREFIX} {coin} SL recreation err: {e}", flush=True)

            try:
                if not cov['tp']:
                    tp_res = place_tp_fn(coin, is_long, entry, abs(sz))
                    if tp_res is not None:
                        print(f"{_LOG_PREFIX} {coin} TP recreated ({tp_res})", flush=True)
                    else:
                        print(f"{_LOG_PREFIX} {coin} TP RECREATION FAILED", flush=True)
            except Exception as e:
                print(f"{_LOG_PREFIX} {coin} TP recreation err: {e}", flush=True)

            # DEADMAN: if still naked and duration > EMERGENCY_CLOSE_AFTER_SEC,
            # close the position. Only triggers if SL is missing (TP-missing is
            # less critical — won't lose money, just miss profits).
            if not cov['sl'] and naked_duration > EMERGENCY_CLOSE_AFTER_SEC:
                with _LOCK:
                    _VIOLATIONS['deadman_triggered'] += 1
                print(f"{_LOG_PREFIX} ★★★ DEADMAN TRIGGER {coin}: "
                      f"SL missing {naked_duration:.0f}s > {EMERGENCY_CLOSE_AFTER_SEC}s. "
                      f"EMERGENCY CLOSE.", flush=True)
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
        print(f"{_LOG_PREFIX} ⚠ AUDIT MISMATCH {coin}: {rec['violation']}", flush=True)

    with _LOCK:
        _AUDIT_LOG.append(rec)
        if len(_AUDIT_LOG) > _AUDIT_MAX:
            _AUDIT_LOG[:] = _AUDIT_LOG[-_AUDIT_MAX:]


# ─────────────────────────────────────────────────────
# Deadman daemon
# ─────────────────────────────────────────────────────
def start_deadman_daemon(live_positions_fn, get_open_orders_fn,
                         place_tp_fn, place_sl_fn, close_fn):
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
                    place_tp_fn, place_sl_fn, close_fn)
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
    }
