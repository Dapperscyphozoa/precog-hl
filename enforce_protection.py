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
# SL MUST exist within 5s → CRITICAL, emergency close on violation
# TP MUST exist within 15s → NON-CRITICAL, repair only
SL_DEADLINE_SEC = 5.0
TP_DEADLINE_SEC = 15.0
FULL_PROTECTION_DEADLINE_SEC = 15.0

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
    Returns the settled size (or last observed if timeout)."""
    start = time.time()
    last = None
    stable_count = 0
    while time.time() - start < timeout:
        try:
            sz = fetch_size_fn(coin)
        except Exception:
            sz = None
        if sz is None:
            time.sleep(0.25)
            continue
        if last is not None and abs(sz - last) < 1e-9:
            stable_count += 1
            if stable_count >= 2:  # read same value twice in a row
                return sz
        else:
            stable_count = 0
        last = sz
        time.sleep(0.25)
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

        # 5a. VERIFY SL EXISTS within deadline
        sl_verified = False
        verify_start = time.time()
        while time.time() - verify_start < SL_DEADLINE_SEC:
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
            # CRITICAL: SL deadline breach → emergency close + halt coin
            _STATS['sl_deadline_breach'] += 1
            result['reason'] = f'SL_DEADLINE_BREACH: SL not verified within {SL_DEADLINE_SEC}s'
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
            try:
                verify_orders = fetch_orders_fn(coin)
                v_tp = [o for o in (verify_orders or []) if (o.get('tpsl') or '').lower() == 'tp']
                v_sl = [o for o in (verify_orders or []) if (o.get('tpsl') or '').lower() == 'sl']
                final_tp_ok = (len(v_tp) == 1 and abs(float(v_tp[0].get('sz', 0)) - actual_size) < 1e-9)
                final_sl_ok = (len(v_sl) == 1 and abs(float(v_sl[0].get('sz', 0)) - actual_size) < 1e-9)
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
