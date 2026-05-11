"""meta_cache — singleton cache for HL meta_and_asset_ctxs.

Was: 4 separate consumers (precog ×2, confluence_worker, oi_tracker)
each calling info.meta_and_asset_ctxs() on its own schedule. Result was
4× the load on the info endpoint and CloudFront 429s on the OI poller
(slowest cycle, gets squeezed out by faster callers).

Now: one cache, TTL ~30s. Every consumer goes through get_meta_ctxs().
If hot: returns cached. If stale: fetches once, with retry/backoff on 429,
and serves the result to all waiters.

Drop-in: returns the same (meta, ctxs) tuple shape as info.meta_and_asset_ctxs().
"""
from __future__ import annotations
import os
import time
import random
import threading
from typing import Optional, Tuple, Any

_TTL_SEC = float(os.environ.get('META_CACHE_TTL_SEC', '30'))
_MAX_BACKOFF_SEC = float(os.environ.get('META_CACHE_MAX_BACKOFF', '120'))

_LOCK = threading.RLock()
_FETCH_LOCK = threading.Lock()      # only one in-flight fetch at a time
_DATA: Optional[Tuple[Any, Any]] = None
_TS: float = 0.0
_NEXT_OK_AT: float = 0.0            # honoured backoff floor (set by 429)
_LAST_ERR: str = ''

_STATS = {
    'hits': 0,
    'misses': 0,
    'fetches_ok': 0,
    'fetches_429': 0,
    'fetches_err': 0,
    'last_fetch_dur_ms': 0,
    'last_429_at': 0,
}


def _is_429(exc: BaseException) -> bool:
    s = str(exc).lower()
    return '429' in s or 'too many' in s or 'rate' in s and 'limit' in s


def _do_fetch(info_obj) -> Optional[Tuple[Any, Any]]:
    global _NEXT_OK_AT, _LAST_ERR
    if info_obj is None or not hasattr(info_obj, 'meta_and_asset_ctxs'):
        _LAST_ERR = 'info_unset'
        return None
    t0 = time.time()
    try:
        result = info_obj.meta_and_asset_ctxs()
        _STATS['last_fetch_dur_ms'] = int((time.time() - t0) * 1000)
        if not result or len(result) < 2:
            _STATS['fetches_err'] += 1
            _LAST_ERR = 'empty_response'
            return None
        _STATS['fetches_ok'] += 1
        _LAST_ERR = ''
        return (result[0], result[1])
    except Exception as e:
        if _is_429(e):
            _STATS['fetches_429'] += 1
            _STATS['last_429_at'] = int(time.time())
            # Exponential backoff with jitter, capped
            prev_floor = max(0.0, _NEXT_OK_AT - time.time())
            new_floor = min(_MAX_BACKOFF_SEC, max(15.0, prev_floor * 2 if prev_floor else 15.0))
            new_floor += random.uniform(0, 5)
            _NEXT_OK_AT = time.time() + new_floor
            _LAST_ERR = f'429:backoff_{new_floor:.0f}s'
        else:
            _STATS['fetches_err'] += 1
            _LAST_ERR = f'{type(e).__name__}:{e}'
        return None


def get_meta_ctxs(info_obj=None, max_age_sec: Optional[float] = None) -> Optional[Tuple[Any, Any]]:
    """Return (meta, ctxs) tuple. Cached for _TTL_SEC.

    info_obj: pass precog.info (or anything with .meta_and_asset_ctxs()).
              If None, will lazy-import precog and use precog.info.
    max_age_sec: override TTL on this call (e.g. OI tracker accepts older data).
    """
    global _DATA, _TS
    ttl = max_age_sec if max_age_sec is not None else _TTL_SEC

    with _LOCK:
        if _DATA is not None and (time.time() - _TS) < ttl:
            _STATS['hits'] += 1
            return _DATA

    if info_obj is None:
        try:
            import precog as _p
            info_obj = getattr(_p, 'info', None)
        except Exception:
            info_obj = None

    # Respect backoff floor from a recent 429
    if time.time() < _NEXT_OK_AT:
        with _LOCK:
            # Serve last known good if available, even if stale
            if _DATA is not None:
                _STATS['hits'] += 1
                return _DATA
        return None

    # Single-flight fetch: many callers race here, only one actually hits the wire
    with _FETCH_LOCK:
        # Re-check after acquiring lock — another thread may have just refreshed
        with _LOCK:
            if _DATA is not None and (time.time() - _TS) < ttl:
                _STATS['hits'] += 1
                return _DATA
        _STATS['misses'] += 1
        result = _do_fetch(info_obj)
        if result is not None:
            with _LOCK:
                _DATA = result
                _TS = time.time()
            return result
        # Fetch failed; serve stale if we have anything
        with _LOCK:
            if _DATA is not None:
                return _DATA
        return None


def status() -> dict:
    return {
        'ttl_sec': _TTL_SEC,
        'cache_age_sec': round(time.time() - _TS, 1) if _TS else -1,
        'has_data': _DATA is not None,
        'next_ok_in_sec': max(0, round(_NEXT_OK_AT - time.time(), 1)),
        'last_err': _LAST_ERR,
        **_STATS,
    }
