"""
sl_state_tracker.py — Event-confirmed SL state machine.

Replaces poll-with-timer SL verification (which produced false-negative
emergency closes when exchange visibility lagged behind placement) with
an explicit state machine:

  SENT       → SL order returned successfully from exchange.order()
  PENDING    → still being verified across multiple cycles
  CONFIRMED  → SL visible in subsequent fetch_orders() poll
  MISSING    → SL never appeared after grace cycles

Emergency close fires ONLY when state = MISSING (after grace_cycles confirmed
absences). Single-shot timer never triggers close directly.

2026-04-25: ledger-first confirmation. position_ledger consumes the HL
webData2 WS stream and tracks live trigger oids per coin. check_state now
queries the ledger BEFORE polling REST — if the ledger sees a trigger
on this coin, that's a confirmation and we skip the REST round-trip.
This eliminates the false-negative MISSING→emergency-close cascade where
SL was placed successfully but REST visibility lagged 5–10s behind.

Public API:
  mark_sent(coin, order_id, size, side, ts) — call after exchange.order returns
  confirm(coin) — call when fetch_orders shows SL exists
  check_state(coin) — returns ('CONFIRMED'|'PENDING'|'MISSING', reason)
  cleanup(coin) — clear state when position closes (no SL needed)
  status() — dict for /health diagnostics

Behavior:
  - mark_sent: state=SENT, grace_cycles_remaining=4
  - confirm: state=CONFIRMED, idempotent
  - check_state called every reconciler cycle:
      if state==CONFIRMED → return CONFIRMED
      if ledger fresh AND ledger.sl_oid set → CONFIRMED
      if state==SENT/PENDING:
          if recently sent (<12s): return PENDING (always — let exchange propagate)
          poll fetch_orders via callback (REST fallback)
          if visible: state=CONFIRMED, return CONFIRMED
          else: grace_cycles_remaining -= 1
                if remaining > 0: state=PENDING, return PENDING
                else: state=MISSING, return MISSING (real failure)
"""

import os
import time
import threading


# State enum
SENT = 'SENT'
PENDING = 'PENDING'
CONFIRMED = 'CONFIRMED'
MISSING = 'MISSING'

# Configuration — env-overridable. Defaults raised 2026-04-25 to absorb HL
# REST visibility lag (5-10s typical, 15s+ during congestion). Lower numbers
# were producing false MISSING → false emergency close → killed valid trades.
GRACE_CYCLES = int(os.environ.get('SL_GRACE_CYCLES', '4'))
PROPAGATION_GRACE_SEC = float(os.environ.get('SL_PROPAGATION_GRACE_SEC', '12.0'))

# Ledger integration — when True, query position_ledger first. Only falls
# through to REST poll if ledger is stale or doesn't know about this coin.
USE_LEDGER_FOR_SL_CONFIRM = os.environ.get('USE_LEDGER_FOR_SL_CONFIRM', '1') == '1'

# Lazy import — position_ledger may not be available in older deploys
_ledger_mod = None
def _get_ledger():
    global _ledger_mod
    if _ledger_mod is None:
        try:
            import position_ledger as _pl
            _ledger_mod = _pl
        except Exception:
            _ledger_mod = False  # sentinel — tried and failed
    return _ledger_mod or None


_TRACKER = {}             # {coin: {state, ts_sent, size, side, grace_remaining, last_poll_ts}}
_LOCK = threading.Lock()

_STATS = {
    'marked_sent': 0,
    'confirmed': 0,
    'missing_after_grace': 0,
    'cleaned_up': 0,
    'check_calls': 0,
    'pending_returns': 0,
    'confirmed_returns': 0,
    'missing_returns': 0,
    'ledger_confirms': 0,
    'rest_confirms': 0,
}


def mark_sent(coin, order_id=None, size=None, side=None, log_fn=None):
    """Called after exchange.order returns successfully for an SL placement."""
    coin_u = coin.upper()
    with _LOCK:
        _TRACKER[coin_u] = {
            'state': SENT,
            'order_id': order_id,
            'size': size,
            'side': side,
            'ts_sent': time.time(),
            'grace_remaining': GRACE_CYCLES,
            'last_poll_ts': 0.0,
            'confirmed_ts': None,
        }
    _STATS['marked_sent'] += 1
    if log_fn:
        log_fn(f"[sl_state] {coin_u} SENT order_id={order_id} size={size}")


def confirm(coin, log_fn=None):
    """Called when fetch_orders shows SL exists. Idempotent."""
    coin_u = coin.upper()
    with _LOCK:
        rec = _TRACKER.get(coin_u)
        if not rec:
            return False
        if rec['state'] == CONFIRMED:
            return True  # already confirmed
        rec['state'] = CONFIRMED
        rec['confirmed_ts'] = time.time()
    _STATS['confirmed'] += 1
    if log_fn:
        log_fn(f"[sl_state] {coin_u} CONFIRMED (was {rec.get('state')})")
    return True


def check_state(coin, fetch_orders_fn=None, expected_size=None, log_fn=None):
    """Returns (state_str, reason). Polls fetch_orders if needed.

    Call from reconciler loop, NOT from emergency close path.
    Emergency close should query check_state() and only fire on MISSING.
    """
    _STATS['check_calls'] += 1
    coin_u = coin.upper()

    with _LOCK:
        rec = _TRACKER.get(coin_u)
        if not rec:
            return CONFIRMED, 'no_tracking'  # never tracked = no SL expected
        state = rec['state']
        ts_sent = rec['ts_sent']
        grace = rec['grace_remaining']

    # Already confirmed → done
    if state == CONFIRMED:
        _STATS['confirmed_returns'] += 1
        return CONFIRMED, 'confirmed'

    # Already missing → terminal state
    if state == MISSING:
        _STATS['missing_returns'] += 1
        return MISSING, 'previously_marked_missing'

    # ─── LEDGER-FIRST CHECK ──────────────────────────────────────────
    # 2026-04-25: position_ledger is fed by HL webData2 WS stream which
    # contains open trigger orders per coin. If the ledger sees an SL
    # trigger on this coin, that's authoritative confirmation — no need
    # for REST. This eliminates the 5-10s false-negative window where
    # SL was placed but REST visibility lagged.
    if USE_LEDGER_FOR_SL_CONFIRM:
        led = _get_ledger()
        if led is not None:
            try:
                if led.ws_is_fresh(max_age_sec=30):
                    prot = led.get_protection(coin_u)
                    if prot and prot.get('sl_oid'):
                        with _LOCK:
                            if coin_u in _TRACKER:
                                _TRACKER[coin_u]['state'] = CONFIRMED
                                _TRACKER[coin_u]['confirmed_ts'] = time.time()
                        _STATS['confirmed'] += 1
                        _STATS['confirmed_returns'] += 1
                        _STATS['ledger_confirms'] += 1
                        if log_fn:
                            log_fn(f"[sl_state] {coin_u} CONFIRMED via ledger "
                                   f"(sl_oid={prot['sl_oid']})")
                        return CONFIRMED, 'confirmed_via_ledger'
            except Exception:
                pass  # Fall through to REST poll

    # Within propagation grace window — always pending, never poll
    elapsed = time.time() - ts_sent
    if elapsed < PROPAGATION_GRACE_SEC:
        _STATS['pending_returns'] += 1
        return PENDING, f'propagation_grace ({elapsed:.1f}s < {PROPAGATION_GRACE_SEC}s)'

    # Poll exchange to see if SL is now visible
    if fetch_orders_fn is not None:
        try:
            orders = fetch_orders_fn(coin_u) or []
            sl_orders = [o for o in orders if (o.get('tpsl') or '').lower() == 'sl']
            sl_visible = False
            if expected_size is not None:
                # Match by size if available
                for sl in sl_orders:
                    if abs(float(sl.get('sz', 0)) - expected_size) < 1e-9:
                        sl_visible = True
                        break
            else:
                sl_visible = len(sl_orders) > 0

            if sl_visible:
                with _LOCK:
                    if coin_u in _TRACKER:
                        _TRACKER[coin_u]['state'] = CONFIRMED
                        _TRACKER[coin_u]['confirmed_ts'] = time.time()
                _STATS['confirmed'] += 1
                _STATS['confirmed_returns'] += 1
                _STATS['rest_confirms'] += 1
                if log_fn:
                    log_fn(f"[sl_state] {coin_u} CONFIRMED on REST poll (was {state} after {elapsed:.1f}s)")
                return CONFIRMED, f'confirmed_on_poll_after_{elapsed:.1f}s'
        except Exception as e:
            if log_fn:
                log_fn(f"[sl_state] {coin_u} fetch_orders err (ignoring): {e}")
            # On poll error, return PENDING — don't escalate to MISSING on transient issues
            _STATS['pending_returns'] += 1
            return PENDING, f'poll_error_grace_remaining={grace}'

    # SL not visible on poll → decrement grace
    with _LOCK:
        if coin_u not in _TRACKER:
            return CONFIRMED, 'cleaned_up'
        _TRACKER[coin_u]['grace_remaining'] -= 1
        _TRACKER[coin_u]['last_poll_ts'] = time.time()
        new_grace = _TRACKER[coin_u]['grace_remaining']
        if new_grace > 0:
            _TRACKER[coin_u]['state'] = PENDING
            _STATS['pending_returns'] += 1
            if log_fn:
                log_fn(f"[sl_state] {coin_u} PENDING (grace_remaining={new_grace})")
            return PENDING, f'invisible_grace_remaining={new_grace}'
        else:
            _TRACKER[coin_u]['state'] = MISSING
            _STATS['missing_after_grace'] += 1
            _STATS['missing_returns'] += 1
            if log_fn:
                log_fn(f"[sl_state] {coin_u} MISSING after {GRACE_CYCLES} grace cycles + {elapsed:.1f}s")
            return MISSING, f'missing_after_{GRACE_CYCLES}_grace_cycles'


def cleanup(coin, reason='position_closed'):
    """Remove tracking when position closes (SL no longer needed)."""
    coin_u = coin.upper()
    with _LOCK:
        if coin_u in _TRACKER:
            del _TRACKER[coin_u]
            _STATS['cleaned_up'] += 1


def status():
    """Diagnostic state for /health endpoint."""
    with _LOCK:
        active = {coin: {
            'state': r['state'],
            'age_sec': round(time.time() - r['ts_sent'], 1),
            'grace_remaining': r['grace_remaining'],
        } for coin, r in _TRACKER.items()}
    return {
        'active_tracks': len(active),
        'tracks_by_coin': active,
        'grace_cycles_per_coin': GRACE_CYCLES,
        'propagation_grace_sec': PROPAGATION_GRACE_SEC,
        **_STATS,
    }
