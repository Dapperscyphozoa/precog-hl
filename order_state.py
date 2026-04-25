"""
order_state.py — Per-trade lifecycle lock to prevent duplicate dispatch.

Problem:
  A single trade decision can have multiple paths reaching exchange.order:
    - direct dispatch
    - retry-once on transient failure
    - maker→taker fallback
    - webhook fill confirmation re-firing handler
  Without a lifecycle lock, two paths can fire concurrent writes for the
  same trade_id, causing duplicate fills and double-cancel races.

Fix:
  Acquire(trade_id) returns False if already in flight. Caller skips.
  Release(trade_id) clears the lock. Use try/finally to guarantee release.

Notes:
  - Lock is keyed by trade_id (caller-supplied), NOT by coin. Different
    trades on the same coin should not block each other.
  - This is in-memory only — process restart clears all locks. That's fine
    because restart implies all in-flight state is gone anyway.
"""

from threading import Lock


class OrderState:
    def __init__(self):
        self._lock = Lock()
        self._active = set()
        self.stats = {
            'acquired': 0,
            'rejected_already_active': 0,
            'released': 0,
        }

    def acquire(self, trade_id):
        """Returns True if this trade_id wasn't already active. False otherwise."""
        if trade_id is None:
            return True  # missing trade_id can't be deduped — pass through
        with self._lock:
            if trade_id in self._active:
                self.stats['rejected_already_active'] += 1
                return False
            self._active.add(trade_id)
            self.stats['acquired'] += 1
            return True

    def release(self, trade_id):
        if trade_id is None:
            return
        with self._lock:
            if trade_id in self._active:
                self._active.discard(trade_id)
                self.stats['released'] += 1

    def is_active(self, trade_id):
        with self._lock:
            return trade_id in self._active

    def status(self):
        with self._lock:
            return {
                'currently_active': len(self._active),
                **self.stats,
            }


# Singleton
_STATE = OrderState()
acquire   = _STATE.acquire
release   = _STATE.release
is_active = _STATE.is_active
status    = _STATE.status
