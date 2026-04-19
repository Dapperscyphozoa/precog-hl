"""Per-coin leverage detection. Reads HL meta.universe maxLev and caches.
BTC/ETH = 20x, majors = 10x, alts = 3-10x. Use actual instead of blanket 10x.
"""
import time, threading

_LEV_CACHE = {}
_CACHE_TS = 0
_CACHE_TTL = 3600  # 1h
_LOCK = threading.Lock()

def refresh(info_obj):
    """Call with HL Info() instance. Populates cache from meta."""
    global _CACHE_TS
    try:
        meta = info_obj.meta()
        with _LOCK:
            _LEV_CACHE.clear()
            for asset in meta.get('universe', []):
                name = asset.get('name')
                max_lev = asset.get('maxLeverage', 10)
                if name: _LEV_CACHE[name] = int(max_lev)
            _CACHE_TS = time.time()
    except Exception as e:
        print(f"[lev] refresh err: {e}", flush=True)

def get_max(coin, default=10):
    with _LOCK:
        return _LEV_CACHE.get(coin, default)

def get_cache():
    with _LOCK:
        return dict(_LEV_CACHE)

def needs_refresh():
    return (time.time() - _CACHE_TS) > _CACHE_TTL
