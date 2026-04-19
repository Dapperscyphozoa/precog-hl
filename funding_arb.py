"""Cross-venue funding divergence. When HL funding differs from Binance >0.05%/8h, directional bias.
HL funding > Binance → HL longs paying more → shorts on HL have positive carry vs binance longs.
"""
import threading, time, urllib.request, json

_CACHE = {'hl': {}, 'binance': {}, 'ts': 0}
_LOCK = threading.Lock()
REFRESH_SEC = 900  # 15min

def _refresh_hl():
    try:
        req = urllib.request.Request('https://api.hyperliquid.xyz/info',
            data=json.dumps({'type':'metaAndAssetCtxs'}).encode(),
            headers={'Content-Type':'application/json'})
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if not isinstance(r, list) or len(r) < 2: return
        universe = r[0].get('universe', [])
        ctxs = r[1]
        with _LOCK:
            for i, asset in enumerate(universe):
                if i >= len(ctxs): break
                name = asset.get('name')
                f = ctxs[i].get('funding')
                if name and f is not None: _CACHE['hl'][name] = float(f)
    except Exception: pass

def _refresh_binance():
    try:
        r = json.loads(urllib.request.urlopen(
            'https://fapi.binance.com/fapi/v1/premiumIndex', timeout=15).read())
        with _LOCK:
            for item in r:
                sym = item.get('symbol','')
                if sym.endswith('USDT'):
                    coin = sym[:-4]
                    f = item.get('lastFundingRate')
                    if f is not None: _CACHE['binance'][coin] = float(f)
    except Exception: pass

def refresh():
    _refresh_hl(); _refresh_binance()
    with _LOCK: _CACHE['ts'] = time.time()

def divergence(coin):
    """Returns HL_funding - Binance_funding. Positive = HL funding higher."""
    with _LOCK:
        h = _CACHE['hl'].get(coin); b = _CACHE['binance'].get(coin)
    if h is None or b is None: return None
    return h - b

def arb_bias(coin, threshold=0.0005):
    """If HL funding much higher than Binance → HL longs paying too much → short bias on HL."""
    d = divergence(coin)
    if d is None: return 0
    if d > threshold: return -1   # HL funding rich, short HL
    if d < -threshold: return 1   # HL funding cheap, long HL
    return 0

def status():
    with _LOCK:
        return {'hl_coins': len(_CACHE['hl']), 'binance_coins': len(_CACHE['binance']),
                'last_refresh': int(time.time() - _CACHE['ts']) if _CACHE['ts'] else None}

def needs_refresh():
    with _LOCK:
        return time.time() - _CACHE['ts'] > REFRESH_SEC
