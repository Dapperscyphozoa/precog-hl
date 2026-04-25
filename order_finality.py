"""
order_finality.py — Order finality tracker. SL/TP only valid when:
  exchange_ack == TRUE AND state == VERIFIED_ACTIVE

Architecture rule:
  An "in-flight" SL/TP that has been sent but not exchange-acknowledged
  is NOT a real protection. Risk engine must NOT treat it as such.
  This module is the single source of truth for whether a coin's SL/TP
  is exchange-confirmed and active.

API:
  register_sent(coin, side, order_id, kind='sl'|'tp')
  register_acked(coin, kind, exchange_order_id)
  register_active(coin, kind, exchange_order_id)
  is_sl_active(coin) → bool
  is_tp_active(coin) → bool
  is_fully_protected(coin) → bool   # both SL and TP active
  invalidate(coin, kind)            # cancel/expire
  status() → diagnostic
"""

import time
import threading


KIND_SL = 'sl'
KIND_TP = 'tp'

STATE_NONE = 'NONE'
STATE_SENT = 'SENT'
STATE_ACKED = 'ACKED'
STATE_ACTIVE = 'ACTIVE'      # VERIFIED_ACTIVE — exchange confirmed live
STATE_INVALID = 'INVALID'

# {coin: {'sl': {state, order_id, ex_oid, ts}, 'tp': {...}}}
_ORDERS = {}
_LOCK = threading.Lock()

_STATS = {
    'sl_active_total': 0,
    'tp_active_total': 0,
    'invalidated': 0,
    'rejected_bad_kind': 0,
    'currently_sl_active': 0,
    'currently_tp_active': 0,
}


def _now():
    return time.time()


def _validate_kind(kind):
    if kind not in (KIND_SL, KIND_TP):
        _STATS['rejected_bad_kind'] += 1
        raise ValueError(f"bad kind {kind!r}, must be 'sl' or 'tp'")


def _ensure(coin):
    coin_u = coin.upper()
    if coin_u not in _ORDERS:
        _ORDERS[coin_u] = {
            KIND_SL: {'state': STATE_NONE, 'order_id': None, 'ex_oid': None, 'ts': 0},
            KIND_TP: {'state': STATE_NONE, 'order_id': None, 'ex_oid': None, 'ts': 0},
        }
    return _ORDERS[coin_u]


def register_sent(coin, kind, order_id):
    """Order was submitted to exchange. Not yet acknowledged."""
    _validate_kind(kind)
    with _LOCK:
        rec = _ensure(coin)[kind]
        rec.update({'state': STATE_SENT, 'order_id': order_id, 'ts': _now()})


def register_acked(coin, kind, exchange_order_id):
    """Exchange acknowledged the order (returned an order id)."""
    _validate_kind(kind)
    with _LOCK:
        rec = _ensure(coin)[kind]
        rec.update({'state': STATE_ACKED, 'ex_oid': exchange_order_id, 'ts': _now()})


def register_active(coin, kind, exchange_order_id=None):
    """Exchange confirmed the order is live and resting on the book."""
    _validate_kind(kind)
    with _LOCK:
        rec = _ensure(coin)[kind]
        was_active = (rec['state'] == STATE_ACTIVE)
        rec.update({'state': STATE_ACTIVE, 'ts': _now()})
        if exchange_order_id:
            rec['ex_oid'] = exchange_order_id
        if not was_active:
            if kind == KIND_SL:
                _STATS['sl_active_total'] += 1
                _STATS['currently_sl_active'] += 1
            else:
                _STATS['tp_active_total'] += 1
                _STATS['currently_tp_active'] += 1


def invalidate(coin, kind):
    """Cancel/expire a tracked order (canceled, rejected, position closed)."""
    _validate_kind(kind)
    with _LOCK:
        if coin.upper() not in _ORDERS:
            return
        rec = _ORDERS[coin.upper()][kind]
        was_active = (rec['state'] == STATE_ACTIVE)
        rec.update({'state': STATE_INVALID, 'ts': _now()})
        if was_active:
            _STATS['invalidated'] += 1
            if kind == KIND_SL:
                _STATS['currently_sl_active'] = max(0, _STATS['currently_sl_active'] - 1)
            else:
                _STATS['currently_tp_active'] = max(0, _STATS['currently_tp_active'] - 1)


def cleanup(coin):
    """Drop tracking for a coin (e.g. position closed)."""
    with _LOCK:
        rec = _ORDERS.pop(coin.upper(), None)
        if not rec:
            return
        for kind in (KIND_SL, KIND_TP):
            if rec[kind]['state'] == STATE_ACTIVE:
                if kind == KIND_SL:
                    _STATS['currently_sl_active'] = max(0, _STATS['currently_sl_active'] - 1)
                else:
                    _STATS['currently_tp_active'] = max(0, _STATS['currently_tp_active'] - 1)


def is_sl_active(coin):
    with _LOCK:
        rec = _ORDERS.get(coin.upper())
        return bool(rec and rec[KIND_SL]['state'] == STATE_ACTIVE)


def is_tp_active(coin):
    with _LOCK:
        rec = _ORDERS.get(coin.upper())
        return bool(rec and rec[KIND_TP]['state'] == STATE_ACTIVE)


def is_fully_protected(coin):
    """Both SL and TP exchange-confirmed active."""
    return is_sl_active(coin) and is_tp_active(coin)


def status():
    with _LOCK:
        n_coins = len(_ORDERS)
        sl_active = sum(1 for r in _ORDERS.values() if r[KIND_SL]['state'] == STATE_ACTIVE)
        tp_active = sum(1 for r in _ORDERS.values() if r[KIND_TP]['state'] == STATE_ACTIVE)
        return {
            'active': True,
            'tracked_coins': n_coins,
            'sl_active_now': sl_active,
            'tp_active_now': tp_active,
            **_STATS,
        }
