"""
flight_guard.py — Per-coin write spacing for HL execution writes.

Problem (from logs):
  Cancel + taker on the same coin within ~600ms triggers CloudFront 429.
  Existing _rate_limited_order wrapper covers exchange.order but NOT
  exchange.cancel. So cancel→taker pairs slip past the global throttle.

Fix:
  Per-coin spacing — any HL write touching coin X must be ≥350ms after
  the previous write on coin X. Enforced via sleep, not rejection, because
  every write is intentional and we'd rather queue than drop.

Design choice — sleep vs return False:
  The original spec proposed allow()→bool with caller-side skip. We use
  a sleep-until-safe variant (acquire) for cancel/order wrappers because
  every cancel/order is part of a committed lifecycle decision; dropping
  one creates orphaned state. The bool variant is exposed as allow() for
  callers that genuinely want non-blocking semantics.
"""

import time
from threading import Lock

# 350ms spacing per coin — empirically, CloudFront's burst-trip window for
# this endpoint family appears to be ~300-500ms based on the 619ms cancel→
# taker collision observed in production logs.
DEFAULT_COOLDOWN_SEC = float(__import__('os').environ.get('FLIGHT_COOLDOWN_MS', '350')) / 1000.0


class FlightGuard:
    def __init__(self, cooldown_sec=DEFAULT_COOLDOWN_SEC):
        self.cooldown = cooldown_sec
        self._lock = Lock()
        self._last_call = {}  # coin -> last write timestamp
        # Counters for /health diagnostics
        self.stats = {
            'acquires': 0,
            'waits':    0,
            'wait_total_sec': 0.0,
            'allows_true':  0,
            'allows_false': 0,
        }

    def acquire(self, coin):
        """Block until safe to write to `coin`. Updates last-call timestamp.
        Use this for committed writes (cancel, order) where we MUST proceed."""
        with self._lock:
            now = time.time()
            last = self._last_call.get(coin, 0.0)
            wait_for = (last + self.cooldown) - now
            self.stats['acquires'] += 1
            if wait_for > 0:
                self.stats['waits'] += 1
                self.stats['wait_total_sec'] += wait_for
            else:
                wait_for = 0
        if wait_for > 0:
            time.sleep(wait_for)
        with self._lock:
            self._last_call[coin] = time.time()

    def allow(self, coin):
        """Non-blocking check — returns True if safe to write now,
        False if too soon. Updates last-call timestamp on True."""
        now = time.time()
        with self._lock:
            last = self._last_call.get(coin, 0.0)
            if now - last < self.cooldown:
                self.stats['allows_false'] += 1
                return False
            self._last_call[coin] = now
            self.stats['allows_true'] += 1
            return True

    def status(self):
        with self._lock:
            return {
                'cooldown_sec': self.cooldown,
                'tracked_coins': len(self._last_call),
                **self.stats,
                'avg_wait_ms': round((self.stats['wait_total_sec'] /
                                      max(1, self.stats['waits'])) * 1000, 1),
            }


# Singleton
_GUARD = FlightGuard()
acquire = _GUARD.acquire
allow   = _GUARD.allow
status  = _GUARD.status
