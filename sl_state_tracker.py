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

Public API:
  mark_sent(coin, order_id, size, side, ts) — call after exchange.order returns
  confirm(coin) — call when fetch_orders shows SL exists
  check_state(coin) — returns ('CONFIRMED'|'PENDING'|'MISSING', reason)
  cleanup(coin) — clear state when position closes (no SL needed)
  status() — dict for /health diagnostics

Behavior:
  - mark_sent: state=SENT, grace_cycles_remaining=2
  - confirm: state=CONFIRMED, idempotent
  - check_state called every reconciler cycle:
      if state==CONFIRMED → return CONFIRMED
      if state==SENT/PENDING:
          if recently sent (<5s): return PENDING (always — let exchange propagate)
          poll fetch_orders via callback
          if visible: state=CONFIRMED, return CONFIRMED
          else: grace_cycles_remaining -= 1
                if remaining > 0: state=PENDING, return PENDING
                else: state=MISSING, return MISSING (real failure)
"""

import time
import threading


# State enum
SENT = 'SENT'
PENDING = 'PENDING'
CONFIRMED = 'CONFIRMED'
MISSING = 'MISSING'

# Configuration
GRACE_CYCLES = 2          # tolerate 2 invisible polls before declaring MISSING
PROPAGATION_GRACE_SEC = 5.0  # always treat as PENDING for first 5s after send

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
                if log_fn:
                    log_fn(f"[sl_state] {coin_u} CONFIRMED on poll (was {state} after {elapsed:.1f}s)")
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
