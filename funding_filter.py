"""Funding rate filter. Block longs when funding > +0.05%/8h (expensive carry).
Block shorts when funding < -0.05%/8h (you'd pay to short). Cached 5min via HL API.

2026-04-28: tightened from 0.1%/8h to 0.05%/8h. Live KB had 3 SHORT-CHOP-NEG-FUNDING
SL hits at funding around -0.05 to -0.07%/8h that the looser threshold missed.
Also added regime gate — only enforce in chop/bear-calm where mean-rev signals
shouldn't be paying funding to wait. In trending regimes, funding can be legit
one-sided and we don't want to gate winners.
"""
import os, time, threading, urllib.request, json

CACHE_TTL = 300  # 5min
THRESHOLD_HIGH = float(os.environ.get('FUNDING_FILTER_THRESHOLD', '0.0005'))   # 0.05% per 8h
GATE_REGIMES = set(r.strip().lower() for r in
                   os.environ.get('FUNDING_FILTER_REGIMES', 'chop,bear-calm').split(',')
                   if r.strip())  # only enforce in these regimes; empty = always
ENABLED = os.environ.get('FUNDING_FILTER_ENABLED', '1') == '1'
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
    """Returns True if trade allowed, False if funding makes it expensive.

    Regime-gated: only enforces in regimes listed in FUNDING_FILTER_REGIMES
    (default chop,bear-calm). Trending regimes pass through.
    Fail-soft: no funding data → allow.
    """
    if not ENABLED:
        return True
    # Regime gate — only filter in non-trending regimes where mean-rev
    # signals shouldn't pay funding to wait.
    if GATE_REGIMES:
        try:
            import regime_detector as _rd
            cur = (_rd.get_regime() or '').lower()
            if cur and cur not in GATE_REGIMES:
                return True  # not in a regime we filter
        except Exception:
            pass
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
