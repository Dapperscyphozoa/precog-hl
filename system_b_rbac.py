"""
system_b_rbac.py — Rate-Based Access Control for System B (confluence_worker).

Wraps the HL API calls originating from System B with a priority-aware
throttle. System A is NOT TOUCHED — its calls go through `precog.info.*`
and `precog.exchange.*` directly with whatever existing throttling is in
place. Only confluence_worker.py imports this module; SA's call paths
remain identical.

DESIGN
======

API priority hierarchy, top → bottom:

    Tier 1  SA trading hot path   precog.exchange.order, place, close
                                  → direct, unmetered (untouched)
    Tier 2  SA state reads        _cached_user_state, _cached_mids in precog
                                  → existing 5s cache (untouched)
    Tier 3  SB API calls          THIS MODULE
                                  → token bucket + per-coin gate
                                    + cached pass-throughs
    Tier 4  Telemetry             telemetry_throttle.acquire (existing)
                                  → 5s/coin gate (untouched)

Token bucket (Tier 3):
    capacity        : 5 tokens
    refill          : 1 token / second  (= 1 RPS sustained, 5 RPS burst)
    SB calls take 1 token; if 0 tokens → return cached value or None.

Per-coin minimum intervals on STATE reads only:
    user_state    : 15 seconds (almost always serve from cache)
    all_mids      : 5 seconds  (almost always serve from cache)
    meta_ctxs     : 60 seconds
    get_balance   : 30 seconds

ORDER calls (place / order / close) always pass-through — these are the
trading hot path even for SB. Throttling them would lose entries.

If HL returns 429, exponential backoff is applied locally and the
result is treated as "skip this iteration." No exceptions propagated.

USAGE
=====

In confluence_worker.py, replace:

    mids = _precog.info.all_mids()           →  rbac.get_mids(_precog)
    us   = _precog.info.user_state(WALLET)   →  rbac.get_user_state(_precog)
    eq   = _precog.get_balance()             →  rbac.get_balance_cached(_precog)
    mctx = _precog.info.meta_and_asset_ctxs() → rbac.get_meta_ctxs(_precog)

Order-placement calls (_precog.place, _precog.exchange.order) stay
untouched — they're trading hot path.

OBSERVABILITY
=============

GET /system_b_rbac (precog.py register the route) returns:
    allowed_total, skipped_total, cache_hits_total,
    last_429_at, current_tokens, per_coin_skip_count
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional


# ─── Configuration (env-overridable) ─────────────────────────────────────

CAPACITY = int(os.environ.get('SB_RBAC_CAPACITY', '5'))                # token bucket size
REFILL_PER_SEC = float(os.environ.get('SB_RBAC_REFILL_PER_SEC', '1.0'))

USER_STATE_TTL_SEC = int(os.environ.get('SB_USER_STATE_TTL', '15'))
MIDS_TTL_SEC = int(os.environ.get('SB_MIDS_TTL', '5'))
META_TTL_SEC = int(os.environ.get('SB_META_TTL', '60'))
BALANCE_TTL_SEC = int(os.environ.get('SB_BALANCE_TTL', '30'))

ENABLED = os.environ.get('SB_RBAC_ENABLED', '1') != '0'

# 429 backoff: doubles each consecutive 429 up to 60s
_BACKOFF_BASE_SEC = 2.0
_BACKOFF_MAX_SEC = 60.0


# ─── Token bucket ─────────────────────────────────────────────────────────

class _TokenBucket:
    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self.capacity = float(capacity)
        self.refill_per_sec = refill_per_sec
        self._tokens = float(capacity)
        self._last_refill = time.time()
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            now = time.time()
            elapsed = now - self._last_refill
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_sec)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def current(self) -> float:
        with self._lock:
            return self._tokens


_bucket = _TokenBucket(CAPACITY, REFILL_PER_SEC)


# ─── State + 429 backoff ──────────────────────────────────────────────────

_lock = threading.Lock()

_cache: dict = {                # endpoint → {'data': X, 'ts': float}
    'user_state': None,
    'mids': None,
    'meta_ctxs': None,
    'balance': None,
}

_stats: dict = {
    'allowed_total': 0,
    'skipped_no_token': 0,
    'cache_hits_total': 0,
    'fresh_fetches_total': 0,
    'errors_total': 0,
    'last_429_ts': 0.0,
    'consecutive_429s': 0,
    'current_backoff_until': 0.0,
}

_per_coin_skip: dict = {}        # coin → skip_count


def _record_429() -> None:
    with _lock:
        _stats['last_429_ts'] = time.time()
        _stats['consecutive_429s'] += 1
        backoff = min(_BACKOFF_MAX_SEC,
                      _BACKOFF_BASE_SEC * (2 ** (_stats['consecutive_429s'] - 1)))
        _stats['current_backoff_until'] = time.time() + backoff


def _record_success() -> None:
    with _lock:
        _stats['consecutive_429s'] = 0
        _stats['current_backoff_until'] = 0.0


def _in_backoff() -> bool:
    with _lock:
        return time.time() < _stats['current_backoff_until']


def _bump(key: str) -> None:
    with _lock:
        _stats[key] = _stats.get(key, 0) + 1


# ─── Generic "should I make this call?" gate ─────────────────────────────

def acquire(coin: str = '', reason: str = 'sb_state',
            critical: bool = False) -> bool:
    """True = caller may proceed. False = caller should skip and use cache.

    critical=True bypasses the throttle (for SB order placement/closes).
    """
    if not ENABLED:
        return True
    if critical:
        return True   # never throttle SB trading-hot-path calls
    if _in_backoff():
        if coin:
            _per_coin_skip[coin] = _per_coin_skip.get(coin, 0) + 1
        _bump('skipped_no_token')
        return False
    if not _bucket.acquire():
        if coin:
            _per_coin_skip[coin] = _per_coin_skip.get(coin, 0) + 1
        _bump('skipped_no_token')
        return False
    _bump('allowed_total')
    return True


# ─── Cached pass-throughs ────────────────────────────────────────────────

def _is_429(exc: BaseException) -> bool:
    s = str(exc)
    return '429' in s or 'rate' in s.lower() or 'too many' in s.lower()


def _serve(endpoint: str, ttl_sec: int, fetch_fn, coin: str = ''):
    """Generic serve-from-cache OR fetch-fresh helper."""
    with _lock:
        rec = _cache.get(endpoint)
    now = time.time()
    if rec is not None and now - rec['ts'] < ttl_sec:
        _bump('cache_hits_total')
        return rec['data']
    if not acquire(coin=coin, reason=f'sb_{endpoint}'):
        # No token + cache stale → return stale value (better than nothing)
        return rec['data'] if rec else None
    try:
        data = fetch_fn()
        _record_success()
        with _lock:
            _cache[endpoint] = {'data': data, 'ts': now}
        _bump('fresh_fetches_total')
        return data
    except Exception as exc:
        if _is_429(exc):
            _record_429()
        else:
            _bump('errors_total')
        # Fall back to last cached value on any error
        return rec['data'] if rec else None


def get_user_state(precog_module, coin: str = '') -> Optional[dict]:
    """SB-tier user_state read. 15s cache. Falls back to stale on 429."""
    return _serve(
        'user_state', USER_STATE_TTL_SEC,
        lambda: precog_module.info.user_state(precog_module.WALLET),
        coin=coin,
    )


def get_mids(precog_module, coin: str = '') -> dict:
    """SB-tier all_mids read. 5s cache."""
    res = _serve(
        'mids', MIDS_TTL_SEC,
        lambda: precog_module.info.all_mids() or {},
        coin=coin,
    )
    return res or {}


def get_meta_ctxs(precog_module) -> Optional[list]:
    """SB-tier meta_and_asset_ctxs. 60s cache."""
    return _serve(
        'meta_ctxs', META_TTL_SEC,
        lambda: precog_module.info.meta_and_asset_ctxs(),
    )


def get_balance_cached(precog_module) -> float:
    """SB-tier get_balance. 30s cache. Returns 0.0 on full failure."""
    res = _serve(
        'balance', BALANCE_TTL_SEC,
        lambda: float(precog_module.get_balance()),
    )
    return float(res) if res is not None else 0.0


# ─── Critical pass-throughs (trading hot path — NOT throttled) ──────────

def place_order(precog_module, coin: str, is_buy: bool,
                size: float, cloid=None) -> Optional[float]:
    """Trading hot path. Not throttled. Records 429 for backoff state only."""
    try:
        return precog_module.place(coin, is_buy, size, cloid=cloid)
    except Exception as exc:
        if _is_429(exc):
            _record_429()
        raise


def cancel_close(precog_module, coin: str, reason: str = 'sb_close') -> Any:
    """Trading hot path. Not throttled."""
    try:
        return precog_module.close_position(coin, reason)
    except Exception as exc:
        if _is_429(exc):
            _record_429()
        raise


# ─── Status endpoint payload ─────────────────────────────────────────────

def status() -> dict:
    with _lock:
        s = dict(_stats)
    s['enabled'] = ENABLED
    s['capacity'] = CAPACITY
    s['refill_per_sec'] = REFILL_PER_SEC
    s['current_tokens'] = round(_bucket.current(), 2)
    s['ttls'] = {
        'user_state': USER_STATE_TTL_SEC,
        'mids': MIDS_TTL_SEC,
        'meta_ctxs': META_TTL_SEC,
        'balance': BALANCE_TTL_SEC,
    }
    s['cache_age_sec'] = {
        k: round(time.time() - v['ts'], 1) if v else None
        for k, v in _cache.items()
    }
    s['top_skipped_coins'] = sorted(
        _per_coin_skip.items(), key=lambda x: -x[1])[:10]
    s['backoff_active'] = _in_backoff()
    s['backoff_remaining_sec'] = max(
        0.0, _stats['current_backoff_until'] - time.time())
    return s


# ─── Convenience: register Flask route ───────────────────────────────────

def register_flask(app) -> None:
    """If precog.py wants the /system_b_rbac endpoint:
        from system_b_rbac import register_flask
        register_flask(app)
    """
    @app.route('/system_b_rbac', methods=['GET'])
    def _rbac_status():
        from flask import jsonify
        return jsonify(status())
