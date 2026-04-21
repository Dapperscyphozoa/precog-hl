"""Hot-path read API for signal engines.

Signal engines call get_param() and get_veto() at every signal tick.
This module adds a 30-second in-memory cache so we don't hit SQLite
200+ times per minute across 50 coins × 20 components.

Usage from signal engine:
    from postmortem import get_param, get_veto
    rsi_hi = get_param(coin, 'rsi', 'sell_threshold', default=75.0)
    if get_veto(coin, 'rsi'):
        return None  # coin is vetoed on this component
"""
import time
import threading

from . import db, bounds

_CACHE = {}           # key: (coin, component, param_name) -> (value, expires_at)
_VETO_CACHE = {}      # key: (coin, component) -> (is_vetoed, expires_at)
_LOCK = threading.Lock()
_TTL = 30.0           # seconds


def _now():
    return time.time()


def get_param(coin, component, param_name, default=None):
    """Return tuned param value. Falls back to bounds default, then caller default.

    Never raises. Never blocks. Safe to call thousands of times per minute.
    """
    if coin is None:
        return _fallback(component, param_name, default)
    key = (coin, component, param_name)
    now = _now()
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and cached[1] > now:
            v = cached[0]
            return v if v is not None else _fallback(component, param_name, default)
    # miss — read and populate
    try:
        v = db.read_param(coin, component, param_name)
    except Exception:
        v = None
    with _LOCK:
        _CACHE[key] = (v, now + _TTL)
    return v if v is not None else _fallback(component, param_name, default)


def _fallback(component, param_name, default):
    b = bounds.get_default(component, param_name)
    if b is not None:
        return b
    return default


def get_veto(coin, component):
    """Return True if signal engine should reject this component for this coin."""
    if coin is None or component is None:
        return False
    key = (coin, component)
    now = _now()
    with _LOCK:
        cached = _VETO_CACHE.get(key)
        if cached and cached[1] > now:
            return cached[0]
    try:
        v = db.read_veto(coin, component)
    except Exception:
        v = False
    with _LOCK:
        _VETO_CACHE[key] = (v, now + _TTL)
    return v


def invalidate(coin=None):
    """Clear cache. Called when tuner writes a new value so next read is fresh."""
    with _LOCK:
        if coin is None:
            _CACHE.clear()
            _VETO_CACHE.clear()
        else:
            for k in list(_CACHE.keys()):
                if k[0] == coin:
                    del _CACHE[k]
            for k in list(_VETO_CACHE.keys()):
                if k[0] == coin:
                    del _VETO_CACHE[k]


def params_summary(coin=None):
    """Dashboard helper — return current param snapshot."""
    try:
        return db.list_params(coin=coin)
    except Exception:
        return []
