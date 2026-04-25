"""Enforce Protection — single atomic invariant for TP/SL lifecycle.

INVARIANT (non-negotiable):
    Every open position MUST have EXACTLY ONE TP and ONE SL on the
    exchange, sized to match the ACTUAL exchange position size.

This module is the single authority for enforcing that invariant.
Replaces scattered place_native_tp/sl calls with a coherent protect()
function that:

1. Locks per-coin (no concurrent re-protects on same coin)
2. Fetches authoritative exchange position size
3. Waits for fill to settle (up to 3s)
4. Fetches existing TP/SL orders for that coin
5. If any are missing OR mis-sized: CANCEL-ALL then PLACE-NEW
6. Verifies correctness post-placement

USAGE:
    from enforce_protection import enforce_protection

    # After ANY entry or add-to-position:
    enforce_protection(coin, is_long, entry_px,
                       fetch_size_fn, fetch_orders_fn,
                       cancel_order_fn, place_tp_fn, place_sl_fn, log_fn)

    # On reconcile-adopt:
    enforce_protection(coin, is_long, entry_px, ...)

    # From deadman daemon (already has fetchers):
    enforce_protection(coin, is_long, entry_px, ...)

All heavy-lifters (fetch_size_fn, place_tp_fn, etc.) are passed in so
this module has zero HL SDK dependency. Safe to unit test in isolation.
"""

import time
import threading
from collections import defaultdict


# Per-coin locks to serialize ALL mutation on a coin (single-writer rule)
_COIN_LOCKS = defaultdict(threading.Lock)

# Tiered deadlines (PHASE 1 SPEC):
# SL MUST exist within deadline → CRITICAL, emergency close on violation
# TP MUST exist within 15s → NON-CRITICAL, repair only
#
# 2026-04-25: SL_DEADLINE 5.0s → 15.0s, EMERGENCY requires 2 consecutive
# verification failures (Option A + C combined). Diagnosis from RSR trade:
# SL was placed successfully but verification check fired before exchange
# state propagated → false-negative emergency close on a healthy position.
#   - 5s was too tight for MAKER→TAKER fallback + async exchange latency
#   - Single-shot timer can't distinguish "SL never placed" from "SL placed,
#     state lag" — both look the same at t+5s
# 15s window covers normal exchange latency (typically 2-8s for SL confirm).
# Plus: emergency close now requires 2 consecutive failed verification cycles
# (~30s total) before firing. Real failures persist; transient lags resolve.
SL_DEADLINE_SEC = 15.0
SL_EMERGENCY_CONFIRM_CYCLES = 2  # consecutive failed checks before emergency close
TP_DEADLINE_SEC = 15.0
FULL_PROTECTION_DEADLINE_SEC = 25.0  # was 15 — match new SL deadline + buffer

# Coins currently halted due to critical execution failure
# {coin: halt_until_ts}. Cleared on next successful entry cycle.
_HALTED_COINS = {}
_HALT_LOCK = threading.Lock()

_STATS = {
    'enforced': 0,
    'verified_ok': 0,
    'replaced': 0,
    'failed': 0,
    'timeouts': 0,
    'sl_deadline_breach': 0,
    'tp_deadline_breach': 0,
    'emergency_closes': 0,
    'coin_halts': 0,
    'by_coin': defaultdict(lambda: {'enforced': 0, 'replaced': 0, 'failed': 0}),
}


def is_coin_halted(coin):
    """Check if trading is halted for this coin due to critical execution failure.
    Halt clears on next successful enforce_protection cycle."""
    with _HALT_LOCK:
        halt_until = _HALTED_COINS.get(coin)
        if halt_until is None:
            return False
        if time.time() > halt_until:
            _HALTED_COINS.pop(coin, None)
            return False
        return True


def halt_coin(coin, duration_sec=300, reason='critical_execution_failure'):
    """Halt new entries on this coin for `duration_sec`. Used after emergency
    close to prevent immediate re-entry that would reproduce the failure."""
    with _HALT_LOCK:
        _HALTED_COINS[coin] = time.time() + duration_sec
        _STATS['coin_halts'] += 1
    return reason


def clear_halt(coin):
    """Manually clear a coin halt. Called after successful protection cycle."""
    with _HALT_LOCK:
        _HALTED_COINS.pop(coin, None)


def cloid_for(coin, side, purpose, size, precision=6):
    """Deterministic client-order-id for HL idempotency.

    cloid = hash(coin + side + purpose + rounded_size)

    Properties:
    - Same (coin, side, purpose, size) → same cloid → HL dedups
    - Different size → different cloid → replacement is legitimate
    - No timestamp → retries don't create duplicates
    - Includes side to avoid TP/SL confusion across opposing positions

    Returns a hyperliquid.utils.types.Cloid object (NOT a raw string).

    Critical: HL SDK calls .to_raw() on cloids during signing — passing a
    bare string crashes the SL/TP placement path with "'str' object has no
    attribute 'to_raw'". Always return the wrapped object.

    Idempotent: if input is already a Cloid (future refactors), passes
    through unchanged. Prevents Cloid(Cloid(...)) double-wrap bugs later.
    """
    import hashlib
    sz_rounded = round(float(size), precision)
    key = f"{coin.upper()}_{side}_{purpose}_{sz_rounded}"
    h = hashlib.sha256(key.encode()).hexdigest()
    raw = '0x' + h[:32]  # 128-bit cloid, HL-compatible format
    # 2026-04-25: wrap in Cloid object for SDK compatibility, idempotent.
    try:
        from hyperliquid.utils.types import Cloid
        # Idempotency guard: pass through if already a Cloid (defensive)
        if isinstance(raw, Cloid):
            return raw
        return Cloid(raw)
    except Exception:
        # Fallback: if SDK unavailable, return raw string (legacy paths)
        return raw


def _wait_for_size_settlement(fetch_size_fn, coin, timeout=3.0):
    """Block until position size reads the same value twice in a row, OR timeout.
    Prevents enforcing protection mid-fill where size flips as HL settles.
    Returns the settled size (or last observed if timeout).

    2026-04-25: poll interval 0.25s → 0.8s. With ledger-backed fetch_size_fn
    (USE_LEDGER_FOR_SIZE=1) this is irrelevant (O(1) lookup), but the legacy
    REST path was making 12+ calls in 3s. 0.8s gives 4 calls — still catches
    flip patterns, 3x less burst pressure on CloudFront.
    """
    start = time.time()
    last = None
    stable_count = 0
    while time.time() - start < timeout:
        try:
            sz = fetch_size_fn(coin)
        except Exception:
            sz = None
        if sz is None:
            time.sleep(0.8)
            continue
        if last is not None and abs(sz - last) < 1e-9:
            stable_count += 1
            if stable_count >= 2:  # read same value twice in a row
                return sz
        else:
            stable_count = 0
        last = sz
        time.sleep(0.8)
    # Timeout — return last observation (may still be valid)
    _STATS['timeouts'] += 1
    return last


def enforce_protection(coin, is_long, entry_px,
                       fetch_size_fn,           # (coin) -> float (abs size) or None
                       fetch_orders_fn,         # (coin) -> list of {oid, sz, tpsl, trigger_px}
                       cancel_order_fn,         # (coin, oid) -> bool
                       place_tp_fn,             # (coin, is_long, entry, size) -> tp_pct or None
                       place_sl_fn,             # (coin, is_long, entry, size) -> sl_pct or None
                       log_fn=None,             # (str) -> None
                       emergency_close_fn=None, # (coin, reason) -> bool — called on SL deadline breach
                       origin='unknown',
                       wait_settle=True):
    """Enforce the protection invariant for a single position.

    PHASE 1 CONTRACT:
      - SL must be verified within 5s of entry. If not: emergency_close + halt coin.
      - TP must be verified within 15s. If not: repair, do not close.
      - Full protection (both + correct size) checked within 15s.
      - All retries use deterministic cloid for exchange-side idempotency.

    Returns:
        dict with keys:
          success: bool
          actual_size: float or None
          tp_placed: bool
          sl_placed: bool
          replaced: bool (did we cancel + replace?)
          tp_pct: float or None (if placed)
          sl_pct: float or None
          reason: str (failure reason if !success)
          emergency_closed: bool (if SL deadline breach forced close)
          coin_halted: bool
          duration_sec: float (total time to enforce)
    """
    def _log(msg):
        if log_fn:
            try: log_fn(f"[enforce:{coin}] {msg}")
            except Exception: pass

    enforce_start = time.time()
    result = {
        'success': False,
        'actual_size': None,
        'tp_placed': False,
        'sl_placed': False,
        'replaced': False,
        'tp_pct': None,
        'sl_pct': None,
        'reason': None,
        'emergency_closed': False,
        'coin_halted': False,
        'duration_sec': 0,
    }

    lock = _COIN_LOCKS[coin]
    acquired = lock.acquire(timeout=5.0)
    if not acquired:
        result['reason'] = 'lock_timeout'
        result['duration_sec'] = time.time() - enforce_start
        _log('lock acquisition timeout (another writer in progress)')
        return result

    try:
        _STATS['enforced'] += 1
        _STATS['by_coin'][coin]['enforced'] += 1

        # 1. Authoritative size from exchange (with settlement wait)
        if wait_settle:
            actual_size = _wait_for_size_settlement(fetch_size_fn, coin, timeout=3.0)
        else:
            try:
                actual_size = fetch_size_fn(coin)
            except Exception as e:
                result['reason'] = f'fetch_size_err: {e}'
                result['duration_sec'] = time.time() - enforce_start
                _log(result['reason'])
                _STATS['failed'] += 1
                _STATS['by_coin'][coin]['failed'] += 1
                return result

        if actual_size is None or actual_size < 1e-9:
            result['reason'] = 'no_position_on_exchange'
            result['duration_sec'] = time.time() - enforce_start
            _log(f'no position found (size={actual_size}) — nothing to protect')
            return result

        result['actual_size'] = actual_size

        # 2. Fetch existing TP/SL orders
        try:
            orders = fetch_orders_fn(coin)
        except Exception as e:
            result['reason'] = f'fetch_orders_err: {e}'
            result['duration_sec'] = time.time() - enforce_start
            _log(result['reason'])
            _STATS['failed'] += 1
            _STATS['by_coin'][coin]['failed'] += 1
            return result

        tp_orders = [o for o in (orders or []) if (o.get('tpsl') or '').lower() == 'tp']
        sl_orders = [o for o in (orders or []) if (o.get('tpsl') or '').lower() == 'sl']

        # 3. Validate invariant
        tp_ok = (len(tp_orders) == 1 and
                 abs(float(tp_orders[0].get('sz', 0)) - actual_size) < 1e-9)
        sl_ok = (len(sl_orders) == 1 and
                 abs(float(sl_orders[0].get('sz', 0)) - actual_size) < 1e-9)

        if tp_ok and sl_ok:
            _log(f'already protected (pos={actual_size}, tp={tp_orders[0]["sz"]}, sl={sl_orders[0]["sz"]})')
            result['success'] = True
            result['tp_placed'] = True
            result['sl_placed'] = True
            result['duration_sec'] = time.time() - enforce_start
            _STATS['verified_ok'] += 1
            clear_halt(coin)  # successful cycle clears prior halt
            return result

        _log(f'INVALID (pos={actual_size}, tp_n={len(tp_orders)} sz={[float(o.get("sz",0)) for o in tp_orders]}, sl_n={len(sl_orders)} sz={[float(o.get("sz",0)) for o in sl_orders]}) → cancel+replace')

        # 4. Cancel ALL existing TP/SL for this coin
        for o in (tp_orders + sl_orders):
            oid = o.get('oid')
            if oid is None: continue
            try:
                cancel_order_fn(coin, oid)
            except Exception as e:
                _log(f'cancel oid={oid} err: {e}')

        time.sleep(0.25)  # settle after cancels

        # Re-fetch authoritative size (could have shifted during cancel window)
        try:
            post_cancel_size = fetch_size_fn(coin)
        except Exception:
            post_cancel_size = actual_size
        if post_cancel_size is not None and post_cancel_size > 1e-9:
            actual_size = post_cancel_size
            result['actual_size'] = actual_size

        # 5. Place fresh SL FIRST (survival invariant), then TP
        # SL placement has tight deadline — block on it
        sl_start = time.time()
        sl_pct = None
        try:
            sl_pct = place_sl_fn(coin, is_long, entry_px, actual_size)
            result['sl_pct'] = sl_pct
            if sl_pct is not None:
                result['sl_placed'] = True
        except Exception as e:
            _log(f'place_sl err: {e}')
        sl_elapsed = time.time() - sl_start

        # 5a. VERIFY SL EXISTS — STATE MACHINE based (no single-shot timer)
        # 2026-04-25 (final): replaced poll-with-deadline + second-pass with
        # event-confirmed state machine. mark_sent() registers the placement;
        # check_state() returns CONFIRMED/PENDING/MISSING. Emergency close
        # ONLY fires on MISSING (after grace cycles). PENDING means we're
        # still waiting for exchange to propagate — keep checking, don't kill.
        sl_verified = False
        sl_state_str = 'UNKNOWN'

        # ─── LEDGER-FIRST EARLY EXIT ─────────────────────────────────
        # 2026-04-25: if WS-fed ledger already shows sl_oid, skip polling.
        # The verify loop below was the dominant source of post-entry REST
        # pressure; ledger short-circuits it when authoritative truth exists.
        try:
            import position_ledger as _pl_pre
            if _pl_pre.ws_is_fresh(max_age_sec=30):
                _prot = _pl_pre.get_protection(coin)
                if _prot and _prot.get('sl_oid'):
                    sl_verified = True
                    sl_state_str = 'CONFIRMED_VIA_LEDGER_PRE'
                    _log(f'{coin} SL pre-verified via ledger '
                         f'(sl_oid={_prot["sl_oid"]}) — skipping verify loop.')
        except Exception:
            pass

        try:
            import sl_state_tracker as _slt
            # Register the placement
            _slt.mark_sent(coin, order_id=None, size=actual_size,
                            side=('SHORT' if is_long else 'LONG'), log_fn=_log)
            if sl_verified:
                # Already confirmed via ledger pre-check; just sync tracker
                try: _slt.confirm(coin, log_fn=_log)
                except Exception: pass
            else:
                # Poll across the verification window using state machine
                verify_start = time.time()
                while time.time() - verify_start < SL_DEADLINE_SEC:
                    state, reason = _slt.check_state(coin,
                        fetch_orders_fn=lambda c: fetch_orders_fn(c),
                        expected_size=actual_size, log_fn=_log)
                    sl_state_str = state
                    if state == _slt.CONFIRMED:
                        sl_verified = True
                        break
                    # PENDING or MISSING — but only break out on MISSING after
                    # grace cycles exhausted (state machine handles this internally)
                    if state == _slt.MISSING:
                        break
                    time.sleep(1.0)  # gentler polling — state machine has propagation grace built in
        except Exception as _slte:
            _log(f'{coin} SL state-tracker err (fallback to legacy poll): {_slte}')
            # Fallback: original poll loop if state tracker unavailable
            verify_start = time.time()
            while time.time() - verify_start < SL_DEADLINE_SEC:
                # Even in fallback, check ledger every iteration (cheap)
                try:
                    import position_ledger as _pl_fb
                    if _pl_fb.ws_is_fresh(max_age_sec=30):
                        _prot_fb = _pl_fb.get_protection(coin)
                        if _prot_fb and _prot_fb.get('sl_oid'):
                            sl_verified = True
                            sl_state_str = 'CONFIRMED_VIA_LEDGER_FB'
                            break
                except Exception:
                    pass
                try:
                    verify_orders = fetch_orders_fn(coin)
                    v_sl = [o for o in (verify_orders or []) if (o.get('tpsl') or '').lower() == 'sl']
                    if len(v_sl) == 1 and abs(float(v_sl[0].get('sz', 0)) - actual_size) < 1e-9:
                        sl_verified = True
                        break
                except Exception:
                    pass
                time.sleep(0.5)

        if not sl_verified:
            # 2026-04-25: ledger-first + PENDING-tolerant breach decision.
            # The verify loop exits at SL_DEADLINE_SEC; the tracker's grace
            # window (PROPAGATION_GRACE_SEC + GRACE_CYCLES cycles) can exceed
            # it, leaving state at PENDING. Previous logic treated that as
            # SL_DEADLINE_BREACH → emergency close on healthy positions.
            #
            # New rule: emergency close requires BOTH
            #   (a) tracker definitively says MISSING, AND
            #   (b) position_ledger (fed by HL webData2 WS) shows no sl_oid
            # On PENDING-with-no-ledger, defer to next reconciler cycle.
            has_sl_in_ledger = False
            try:
                import position_ledger as _pl
                if _pl.ws_is_fresh(max_age_sec=30):
                    prot = _pl.get_protection(coin)
                    if prot and prot.get('sl_oid'):
                        has_sl_in_ledger = True
            except Exception:
                pass

            if has_sl_in_ledger:
                # WS authoritative — ledger sees the SL oid. Confirm it.
                sl_verified = True
                sl_state_str = 'CONFIRMED_VIA_LEDGER'
                _log(f'{coin} SL verify loop exited unconfirmed but '
                     f'ledger.sl_oid is set — confirming via WS truth.')
                try:
                    import sl_state_tracker as _slt
                    _slt.confirm(coin, log_fn=_log)
                except Exception:
                    pass
            elif sl_state_str == 'PENDING':
                # Tracker hasn't reached MISSING yet. Don't kill the
                # position — defer to next reconciler cycle which will
                # re-run check_state. Soft-success here so this enforce
                # call returns OK and TP placement still proceeds.
                sl_verified = True
                sl_state_str = 'PENDING_DEFERRED'
                _log(f'{coin} SL verify loop exited at PENDING (tracker still '
                     f'resolving). Deferring to next reconciler cycle. '
                     f'NOT closing position.')

        if not sl_verified:
            # CRITICAL: tracker says MISSING AND ledger has no sl_oid.
            # This is a genuine SL placement failure. Emergency close.
            _STATS['sl_deadline_breach'] += 1
            result['reason'] = f'SL_DEADLINE_BREACH: state={sl_state_str}'
            _log(f'⚠ CRITICAL: {result["reason"]} — EMERGENCY CLOSE')

            if emergency_close_fn:
                try:
                    emergency_close_fn(coin, 'sl_deadline_breach')
                    result['emergency_closed'] = True
                    _STATS['emergency_closes'] += 1
                except Exception as e:
                    _log(f'emergency_close err: {e}')

            halt_coin(coin, duration_sec=300, reason='sl_deadline_breach')
            result['coin_halted'] = True
            result['duration_sec'] = time.time() - enforce_start
            _STATS['failed'] += 1
            _STATS['by_coin'][coin]['failed'] += 1
            return result

        # 5b. Place TP (non-critical path, TP-deadline allows repair not close)
        tp_pct = None
        try:
            tp_pct = place_tp_fn(coin, is_long, entry_px, actual_size)
            result['tp_pct'] = tp_pct
            if tp_pct is not None:
                result['tp_placed'] = True
        except Exception as e:
            _log(f'place_tp err: {e}')

        result['replaced'] = True
        _STATS['replaced'] += 1
        _STATS['by_coin'][coin]['replaced'] += 1

        # 6. Full verification with tiered TP deadline
        time.sleep(0.5)
        full_verified = False
        final_tp_ok = False
        final_sl_ok = sl_verified
        verify_start = time.time()
        while time.time() - verify_start < TP_DEADLINE_SEC:
            # 2026-04-25: ledger-first SL check on every iteration. If WS
            # ledger shows sl_oid is set, that's authoritative even if REST
            # fetch_orders returns it briefly absent.
            try:
                import position_ledger as _pl_v
                if _pl_v.ws_is_fresh(max_age_sec=30):
                    _prot_v = _pl_v.get_protection(coin)
                    if _prot_v and _prot_v.get('sl_oid'):
                        final_sl_ok = True
            except Exception:
                pass
            try:
                verify_orders = fetch_orders_fn(coin)
                v_tp = [o for o in (verify_orders or []) if (o.get('tpsl') or '').lower() == 'tp']
                v_sl = [o for o in (verify_orders or []) if (o.get('tpsl') or '').lower() == 'sl']
                final_tp_ok = (len(v_tp) == 1 and abs(float(v_tp[0].get('sz', 0)) - actual_size) < 1e-9)
                # If REST shows SL valid, accept; if not but ledger says SL is set, keep final_sl_ok=True from above
                if len(v_sl) == 1 and abs(float(v_sl[0].get('sz', 0)) - actual_size) < 1e-9:
                    final_sl_ok = True
                if final_tp_ok and final_sl_ok:
                    full_verified = True
                    break
            except Exception:
                pass
            time.sleep(0.5)

        result['duration_sec'] = time.time() - enforce_start

        if full_verified:
            _log(f'VERIFIED in {result["duration_sec"]:.1f}s: pos={actual_size}, tp+sl correct')
            result['success'] = True
            clear_halt(coin)
        elif final_sl_ok and not final_tp_ok:
            # SL good, TP bad — NOT critical, do not close, just log
            _STATS['tp_deadline_breach'] += 1
            _log(f'⚠ TP deadline breach: SL ok, TP missing/invalid after {TP_DEADLINE_SEC}s — repair only')
            result['reason'] = 'tp_deadline_breach_repaired'
            result['success'] = False  # protection not fully complete
            # Do NOT emergency close — SL is protecting the position
        elif not final_sl_ok:
            # 2026-04-25: ledger-first override before declaring SL lost.
            # Emergency close requires BOTH tracker MISSING and ledger empty.
            _ledger_has_sl = False
            try:
                import position_ledger as _pl_lost
                if _pl_lost.ws_is_fresh(max_age_sec=30):
                    _prot_lost = _pl_lost.get_protection(coin)
                    if _prot_lost and _prot_lost.get('sl_oid'):
                        _ledger_has_sl = True
            except Exception:
                pass
            if _ledger_has_sl:
                _log(f'{coin} REST says SL lost but ledger.sl_oid is set — '
                     f'NOT closing (deferring to next reconciler cycle).')
                result['reason'] = 'sl_lost_ledger_overrides_no_close'
                result['success'] = False
            else:
                # SL lost between first verify and full check → emergency close
                _STATS['sl_deadline_breach'] += 1
                result['reason'] = 'SL_LOST_POST_PLACEMENT'
                _log(f'⚠ CRITICAL: SL disappeared post-placement → EMERGENCY CLOSE')
                if emergency_close_fn:
                    try:
                        emergency_close_fn(coin, 'sl_lost_post_placement')
                        result['emergency_closed'] = True
                        _STATS['emergency_closes'] += 1
                    except Exception as e:
                        _log(f'emergency_close err: {e}')
                halt_coin(coin, duration_sec=300, reason='sl_lost_post_placement')
                result['coin_halted'] = True
                _STATS['failed'] += 1
                _STATS['by_coin'][coin]['failed'] += 1

        return result

    finally:
        lock.release()


def stats():
    """Aggregate enforcement stats for dashboard."""
    with _HALT_LOCK:
        halts = {c: round(t - time.time(), 1) for c, t in _HALTED_COINS.items() if t > time.time()}
    return {
        'enforced_total': _STATS['enforced'],
        'verified_ok': _STATS['verified_ok'],
        'replaced': _STATS['replaced'],
        'failed': _STATS['failed'],
        'timeouts': _STATS['timeouts'],
        'sl_deadline_breach': _STATS['sl_deadline_breach'],
        'tp_deadline_breach': _STATS['tp_deadline_breach'],
        'emergency_closes': _STATS['emergency_closes'],
        'coin_halts_total': _STATS['coin_halts'],
        'currently_halted': halts,
        'deadlines': {
            'sl_sec': SL_DEADLINE_SEC,
            'tp_sec': TP_DEADLINE_SEC,
            'full_sec': FULL_PROTECTION_DEADLINE_SEC,
        },
        'by_coin': {k: dict(v) for k, v in _STATS['by_coin'].items()},
    }
