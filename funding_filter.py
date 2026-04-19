"""Funding rate filter. Block longs when funding > +0.1%/8h (expensive carry).
Block shorts when funding < -0.1%/8h (you'd pay to short). Cached 5min via HL API.
"""
import time, threading, urllib.request, json

CACHE_TTL = 300  # 5min
THRESHOLD_HIGH = 0.001   # 0.1% per 8h — expensive
_CACHE = {}  # coin -> {rate, ts}
_LOCK = threading.Lock()

def refresh_all(coins):
    """Bulk pull funding for all coins via HL metaAndAssetCtxs."""
    try:
        req = urllib.request.Request('https://api.hyperliquid.xyz/info',
            data=json.dumps({'type':'metaAndAssetCtxs'}).encode(),
            headers={'Content-Type':'application/json'})
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if not isinstance(r, list) or len(r) < 2: return
        universe = r[0].get('universe', [])
        ctxs = r[1]
        now = time.time()
        with _LOCK:
            for i, asset in enumerate(universe):
                if i >= len(ctxs): break
                name = asset.get('name')
                funding = ctxs[i].get('funding')
                if name and funding is not None:
                    _CACHE[name] = {'rate': float(funding), 'ts': now}
    except Exception as e:
        print(f"[funding] refresh err: {e}", flush=True)

def get_rate(coin):
    with _LOCK:
        c = _CACHE.get(coin)
    if not c or time.time() - c['ts'] > CACHE_TTL: return None
    return c['rate']

def allow_side(coin, side):
    """Returns True if trade allowed, False if funding makes it expensive."""
    r = get_rate(coin)
    if r is None: return True  # no data = allow
    if side == 'BUY' and r > THRESHOLD_HIGH: return False  # paying to hold long
    if side == 'SELL' and r < -THRESHOLD_HIGH: return False  # paying to hold short
    return True

def needs_refresh():
    with _LOCK:
        if not _CACHE: return True
        oldest = min(v['ts'] for v in _CACHE.values())
    return time.time() - oldest > CACHE_TTL
