"""Intent Queue — strict-schema event bus for trade lifecycle intents.

Components emit intents; reconciler drains + executes.
No module outside the reconciler is permitted to call close_trade() directly.

INTENT TYPES (enforced at emit time):
    TP              — take profit hit
    SL              — stop loss hit
    TIMEOUT         — max hold reached
    FORCE_CLOSE     — any non-TP/SL reason: funding_cut, trail_exit, wall_exit,
                      manual, protection_breach, signal_reversal-flip, etc.
    SIGNAL_REVERSAL — opposite-side signal fired on existing position
    PROTECTION_HALT — enforce_protection SL breach; emergency close required

PUBLIC API:
    emit(intent_type, coin, trade_id, reason, **ctx) -> bool
    drain(max_items=100)                              -> list[dict]
    peek(max_items=100)                               -> list[dict]  (non-destructive)
    status()                                          -> dict         (for /lifecycle)
"""
import threading
import time
from queue import Queue, Empty

INTENT_TYPES = {
    'TP',
    'SL',
    'TIMEOUT',
    'FORCE_CLOSE',
    'SIGNAL_REVERSAL',
    'PROTECTION_HALT',
}

_QUEUE = Queue()
_LOCK = threading.Lock()
_METRICS = {
    'emitted_total': 0,
    'drained_total': 0,
    'rejected_invalid': 0,
    'by_type': {t: 0 for t in INTENT_TYPES},
    'by_reason': {},
    'queue_depth': 0,
    'last_emit_ts': 0.0,
    'last_drain_ts': 0.0,
}
_RECENT = []  # rolling window of last N intents for visibility
_RECENT_MAX = 100


def emit(intent_type, coin, trade_id=None, reason=None, **context):
    """Emit an intent. Returns True if accepted, False if rejected (invalid type)."""
    if intent_type not in INTENT_TYPES:
        with _LOCK:
            _METRICS['rejected_invalid'] += 1
        return False

    item = {
        'type': intent_type,
        'coin': coin,
        'trade_id': trade_id,
        'reason': reason or intent_type.lower(),
        'ts': time.time(),
        'context': context or {},
    }
    _QUEUE.put(item)

    with _LOCK:
        _METRICS['emitted_total'] += 1
        _METRICS['by_type'][intent_type] = _METRICS['by_type'].get(intent_type, 0) + 1
        rk = item['reason']
        _METRICS['by_reason'][rk] = _METRICS['by_reason'].get(rk, 0) + 1
        _METRICS['queue_depth'] = _QUEUE.qsize()
        _METRICS['last_emit_ts'] = time.time()
        _RECENT.append(item)
        if len(_RECENT) > _RECENT_MAX:
            del _RECENT[0]
    return True


def drain(max_items=100):
    """Remove up to max_items intents from queue. Returns list."""
    items = []
    for _ in range(max_items):
        try:
            items.append(_QUEUE.get_nowait())
        except Empty:
            break
    with _LOCK:
        _METRICS['drained_total'] += len(items)
        _METRICS['queue_depth'] = _QUEUE.qsize()
        _METRICS['last_drain_ts'] = time.time()
    return items


def peek(max_items=100):
    """Non-destructive view of queue contents (for dashboards). Returns up to max_items."""
    # Queue has no peek — we pop all, record, push back.
    with _LOCK:
        items = []
        buffer = []
        for _ in range(max_items):
            try:
                it = _QUEUE.get_nowait()
                buffer.append(it)
                items.append(dict(it))
            except Empty:
                break
        for it in buffer:
            _QUEUE.put(it)
        _METRICS['queue_depth'] = _QUEUE.qsize()
        return items


def status():
    """Snapshot of metrics for /lifecycle endpoint."""
    with _LOCK:
        return {
            'queue_depth': _QUEUE.qsize(),
            'emitted_total': _METRICS['emitted_total'],
            'drained_total': _METRICS['drained_total'],
            'rejected_invalid': _METRICS['rejected_invalid'],
            'by_type': dict(_METRICS['by_type']),
            'by_reason': dict(_METRICS['by_reason']),
            'last_emit_age_sec': (time.time() - _METRICS['last_emit_ts']) if _METRICS['last_emit_ts'] else None,
            'last_drain_age_sec': (time.time() - _METRICS['last_drain_ts']) if _METRICS['last_drain_ts'] else None,
            'recent_intents': list(_RECENT[-20:]),
        }


def reset_metrics():
    """For testing / admin. Does not clear queue."""
    with _LOCK:
        _METRICS['emitted_total'] = 0
        _METRICS['drained_total'] = 0
        _METRICS['rejected_invalid'] = 0
        _METRICS['by_type'] = {t: 0 for t in INTENT_TYPES}
        _METRICS['by_reason'] = {}
        _RECENT.clear()
