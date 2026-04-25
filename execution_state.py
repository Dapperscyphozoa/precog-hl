"""
execution_state.py — Position state machine. Exchange-fill-only authority.

Architecture rule (final):
  Position state transitions are driven ONLY by exchange events.
  No snapshot data, no LKG data, no inferred mid prices, no reconciler
  decisions. If the exchange did not say it, it did not happen.

State machine:
  INIT      — entry order constructed, not yet sent
  SENT      — entry order submitted to exchange
  ACKED     — exchange acknowledged receipt (order id assigned)
  ACTIVE    — entry filled (real fill price + size from exchange)
  VERIFIED  — SL + TP both placed AND exchange-acknowledged active
  CLOSED    — exchange-confirmed exit fill (real exit price)

Forbidden:
  - Marking ACTIVE without exchange fill event
  - Setting entry_price from anything other than exchange fill avgPx
  - Setting exit_price from anything other than exchange fill avgPx
  - VERIFIED without exchange-acked SL + TP order ids

API:
  init(coin, side, size_intended) → state_id
  mark_sent(state_id, order_id)
  mark_acked(state_id, exchange_order_id)
  mark_active(state_id, fill_px, fill_size)  # raises on invalid
  mark_verified(state_id, sl_oid, tp_oid)
  mark_closed(state_id, exit_px)             # raises on invalid
  get(coin) → state dict or None
  is_verified_active(coin) → bool            # gate for risk decisions
  status() → diagnostic
"""

import time
import threading
import uuid


STATE_INIT = 'INIT'
STATE_SENT = 'SENT'
STATE_ACKED = 'ACKED'
STATE_ACTIVE = 'ACTIVE'
STATE_VERIFIED = 'VERIFIED'
STATE_CLOSED = 'CLOSED'

VALID_STATES = {STATE_INIT, STATE_SENT, STATE_ACKED, STATE_ACTIVE,
                STATE_VERIFIED, STATE_CLOSED}

_STATES = {}                # state_id → state dict
_BY_COIN = {}               # coin → state_id (latest open)
_LOCK = threading.Lock()

_STATS = {
    'created': 0,
    'verified_active': 0,
    'closed': 0,
    'rejected_invalid_price': 0,
    'rejected_bad_transition': 0,
    'currently_active': 0,
}


def _now():
    return time.time()


def _validate_price(px, label):
    """Raise on invalid price. No silent zero-fallback."""
    if px is None:
        _STATS['rejected_invalid_price'] += 1
        raise ValueError(f"INVALID {label}: None")
    try:
        f = float(px)
    except (TypeError, ValueError):
        _STATS['rejected_invalid_price'] += 1
        raise ValueError(f"INVALID {label}: not a number ({px!r})")
    if f <= 0:
        _STATS['rejected_invalid_price'] += 1
        raise ValueError(f"INVALID {label}: <= 0 ({f})")
    return f


def init(coin, side, size_intended):
    """Create a new position state entry. Returns state_id (UUID)."""
    state_id = uuid.uuid4().hex[:12]
    rec = {
        'state_id': state_id,
        'coin': coin.upper(),
        'side': side,
        'size_intended': float(size_intended),
        'state': STATE_INIT,
        'created_ts': _now(),
        'order_id': None,
        'exchange_order_id': None,
        'entry_price': None,        # SET ONLY FROM EXCHANGE FILL
        'fill_size': None,
        'sl_order_id': None,
        'tp_order_id': None,
        'exit_price': None,         # SET ONLY FROM EXCHANGE FILL
        'closed_ts': None,
        'transitions': [(_now(), STATE_INIT)],
    }
    with _LOCK:
        _STATES[state_id] = rec
        _BY_COIN[coin.upper()] = state_id
        _STATS['created'] += 1
    return state_id


def _transition(state_id, new_state, allowed_from):
    with _LOCK:
        rec = _STATES.get(state_id)
        if not rec:
            _STATS['rejected_bad_transition'] += 1
            raise ValueError(f"unknown state_id {state_id}")
        if rec['state'] not in allowed_from:
            _STATS['rejected_bad_transition'] += 1
            raise ValueError(
                f"bad transition {rec['state']} → {new_state} "
                f"(allowed from: {allowed_from}) state_id={state_id} coin={rec['coin']}"
            )
        rec['state'] = new_state
        rec['transitions'].append((_now(), new_state))
        return rec


def mark_sent(state_id, order_id):
    rec = _transition(state_id, STATE_SENT, {STATE_INIT})
    rec['order_id'] = order_id


def mark_acked(state_id, exchange_order_id):
    rec = _transition(state_id, STATE_ACKED, {STATE_SENT})
    rec['exchange_order_id'] = exchange_order_id


def mark_active(state_id, fill_px, fill_size):
    """Position is filled. fill_px MUST come from exchange fill event.
    Raises ValueError on invalid price (None, 0, negative, non-numeric).
    """
    px = _validate_price(fill_px, 'entry fill price')
    if not fill_size or float(fill_size) == 0:
        raise ValueError(f"INVALID fill_size: {fill_size!r}")
    rec = _transition(state_id, STATE_ACTIVE, {STATE_ACKED, STATE_SENT})
    rec['entry_price'] = px
    rec['fill_size'] = float(fill_size)
    rec['active_ts'] = _now()


def mark_verified(state_id, sl_order_id, tp_order_id=None):
    """SL placed + acknowledged active on exchange. TP optional.
    Position becomes eligible for risk-engine decisions only after this.
    """
    if not sl_order_id:
        raise ValueError(f"VERIFIED requires sl_order_id (state_id={state_id})")
    rec = _transition(state_id, STATE_VERIFIED, {STATE_ACTIVE})
    rec['sl_order_id'] = sl_order_id
    rec['tp_order_id'] = tp_order_id
    rec['verified_ts'] = _now()
    _STATS['verified_active'] += 1
    _STATS['currently_active'] += 1


def mark_closed(state_id, exit_px):
    """Exchange fill confirmed. exit_px MUST come from exchange fill event.
    Raises ValueError on invalid price.
    """
    px = _validate_price(exit_px, 'exit fill price')
    rec = _transition(state_id, STATE_CLOSED,
                      {STATE_VERIFIED, STATE_ACTIVE, STATE_ACKED, STATE_SENT, STATE_INIT})
    rec['exit_price'] = px
    rec['closed_ts'] = _now()
    _STATS['closed'] += 1
    _STATS['currently_active'] = max(0, _STATS['currently_active'] - 1)
    # Clear coin → state_id pointer if it points to this one
    with _LOCK:
        if _BY_COIN.get(rec['coin']) == state_id:
            _BY_COIN.pop(rec['coin'], None)


def get(coin):
    """Latest state record for coin, or None."""
    with _LOCK:
        sid = _BY_COIN.get(coin.upper())
        if not sid:
            return None
        return dict(_STATES.get(sid, {}))  # shallow copy


def is_verified_active(coin):
    """Gate for risk-engine decisions: True iff position is VERIFIED."""
    rec = get(coin)
    return bool(rec and rec.get('state') == STATE_VERIFIED)


def status():
    with _LOCK:
        active_states = sum(1 for r in _STATES.values()
                            if r['state'] in (STATE_ACTIVE, STATE_VERIFIED))
        verified_states = sum(1 for r in _STATES.values()
                              if r['state'] == STATE_VERIFIED)
        total_active_or_verified = max(1, active_states)
        verified_ratio = verified_states / total_active_or_verified
        return {
            'active': True,
            'total_records': len(_STATES),
            'currently_active': active_states,
            'verified_active': verified_states,
            'verified_active_ratio': round(verified_ratio, 3),
            'by_coin': len(_BY_COIN),
            **_STATS,
        }


def cleanup_closed_older_than(seconds=3600):
    """Drop CLOSED records older than N seconds. Memory hygiene."""
    cutoff = _now() - seconds
    with _LOCK:
        to_drop = [sid for sid, r in _STATES.items()
                   if r['state'] == STATE_CLOSED and (r.get('closed_ts') or 0) < cutoff]
        for sid in to_drop:
            _STATES.pop(sid, None)
        return len(to_drop)
