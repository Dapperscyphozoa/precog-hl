"""Tier-based signal filtering for HL universe.
Re-scans daily, applies different signal quality requirements per tier."""
import json, os, time, urllib.request, threading

TIERS_PATH = '/var/data/tiers.json'
TIER_REFRESH_SEC = 86400  # daily

# Tier thresholds (vol24 USD, OI USD)
T1_MIN_VOL = 200e6
T1_MIN_OI  = 500e6
T2_MIN_VOL = 20e6
T2_MIN_OI  = 80e6
T3_MIN_VOL = 3e6
T3_MIN_OI  = 15e6

# Per-tier confidence thresholds (out of 100)
TIER_CONF_MIN = {1: 0, 2: 30, 3: 45, 4: 60}
TIER_SIZE_MULT = {1: 1.0, 2: 0.8, 3: 0.6, 4: 0.4}

_state = {'tiers': None, 'last_refresh': 0, 'lock': threading.Lock()}

def refresh_tiers():
    """Fetch HL universe + assign tiers."""
    try:
        req = urllib.request.Request('https://api.hyperliquid.xyz/info',
            data=json.dumps({"type":"metaAndAssetCtxs"}).encode(),
            headers={'Content-Type':'application/json'})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        meta, ctxs = data[0], data[1]
        tiers = {}
        for u, c in zip(meta['universe'], ctxs):
            coin = u['name']
            try:
                oi = float(c.get('openInterest','0'))
                vol24 = float(c.get('dayNtlVlm','0'))
                mark = float(c.get('markPx','0'))
                oi_usd = oi * mark
            except: continue
            if vol24 > T1_MIN_VOL or oi_usd > T1_MIN_OI: tiers[coin] = 1
            elif vol24 > T2_MIN_VOL or oi_usd > T2_MIN_OI: tiers[coin] = 2
            elif vol24 > T3_MIN_VOL or oi_usd > T3_MIN_OI: tiers[coin] = 3
            else: tiers[coin] = 4
        with _state['lock']:
            _state['tiers'] = tiers
            _state['last_refresh'] = time.time()
        # Persist
        try:
            os.makedirs(os.path.dirname(TIERS_PATH), exist_ok=True)
            with open(TIERS_PATH,'w') as f: json.dump(tiers, f)
        except: pass
        return tiers
    except Exception as e:
        return None

def get_tier(coin):
    """Returns tier 1-4. Auto-refreshes if stale. Defaults to 4 on unknown."""
    with _state['lock']:
        stale = (time.time() - _state['last_refresh']) > TIER_REFRESH_SEC
        if _state['tiers'] is None or stale:
            # Try load from disk first
            if _state['tiers'] is None and os.path.exists(TIERS_PATH):
                try:
                    _state['tiers'] = json.load(open(TIERS_PATH))
                    _state['last_refresh'] = os.path.getmtime(TIERS_PATH)
                except: pass
            # Refresh in background if stale
            if stale:
                threading.Thread(target=refresh_tiers, daemon=True).start()
        if _state['tiers'] is None: return 4
        return _state['tiers'].get(coin, 4)

def conf_threshold(coin):
    """Per-tier minimum confidence to take a trade."""
    return TIER_CONF_MIN.get(get_tier(coin), 60)

def tier_size_mult(coin):
    """Per-tier size multiplier (preserves capital on lower tiers)."""
    return TIER_SIZE_MULT.get(get_tier(coin), 0.4)

def stats():
    with _state['lock']:
        if not _state['tiers']: return {}
        out = {1:0,2:0,3:0,4:0}
        for c, t in _state['tiers'].items():
            out[t] = out.get(t,0)+1
        return {'tiers': out, 'age_sec': int(time.time()-_state['last_refresh'])}
