"""Structural zone detection for MT4 filter: Order Blocks, FVG, key swing levels.
Runs on Yahoo candle data (same source we already use for ATR).
No orderbook needed — pure price action / structure.
"""
import time
import urllib.request
import json as _json

_zone_cache = {}  # {ticker: (ts, zones_dict)}
_ZONE_TTL = 1800  # 30 min

_YAHOO_MAP = {
    'XAUUSD':'GC=F','XAGUSD':'SI=F','XPTUSD':'PL=F','XPDUSD':'PA=F',
    'SPOTCRUDE':'CL=F','SPOTBRENT':'BZ=F','NATGAS':'NG=F','COPPER':'HG=F',
    'CORN':'ZC=F','WHEAT':'ZW=F','SOYBEANS':'ZS=F','SUGAR':'SB=F','COFFEE':'KC=F',
    'US30':'^DJI','US500':'^GSPC','NAS100':'^NDX','US2000':'^RUT',
    'GER40':'^GDAXI','UK100':'^FTSE','JPN225':'^N225','HK50':'^HSI',
    'VIX':'^VIX','USDX':'DX-Y.NYB',
}

def _fetch_candles(ticker, interval='1h', days=5):
    """Fetch OHLC from Yahoo. Returns list of {t,o,h,l,c} or None."""
    sym = _YAHOO_MAP.get(ticker)
    if not sym:
        if len(ticker)==6 and ticker.isalpha(): sym = f'{ticker}=X'
        else: return None
    try:
        now_ts = int(time.time())
        start = now_ts - 86400*days
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={start}&period2={now_ts}&interval={interval}'
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        data = _json.loads(urllib.request.urlopen(req, timeout=5).read())
        r = data['chart']['result'][0]
        ts = r.get('timestamp',[])
        q = r['indicators']['quote'][0]
        out = []
        for i, t in enumerate(ts):
            c = q.get('close',[None]*len(ts))[i]
            o = q.get('open',[None]*len(ts))[i]
            h = q.get('high',[None]*len(ts))[i]
            l = q.get('low',[None]*len(ts))[i]
            if None in (c,o,h,l): continue
            out.append({'t':t*1000,'o':o,'h':h,'l':l,'c':c})
        return out if len(out) >= 10 else None
    except Exception:
        return None

def detect_order_blocks(candles, lookback=50):
    """Find bullish/bearish Order Blocks.
    Bullish OB: last DOWN candle before a strong UP move (>0.5% next candle).
    Bearish OB: last UP candle before a strong DOWN move.
    Returns list of {type, top, bottom, time}.
    """
    obs = []
    cs = candles[-lookback:] if len(candles) > lookback else candles
    for i in range(1, len(cs)-1):
        prev = cs[i-1]; cur = cs[i]; nxt = cs[i+1]
        # Bullish OB: cur is down candle, next is strong up
        if cur['c'] < cur['o'] and nxt['c'] > nxt['o']:
            next_move = (nxt['c'] - nxt['o']) / nxt['o']
            if next_move > 0.005:  # 0.5% bullish move after
                obs.append({'type':'bullish','top':cur['h'],'bottom':cur['l'],'time':cur['t']})
        # Bearish OB
        if cur['c'] > cur['o'] and nxt['c'] < nxt['o']:
            next_move = (nxt['o'] - nxt['c']) / nxt['o']
            if next_move > 0.005:
                obs.append({'type':'bearish','top':cur['h'],'bottom':cur['l'],'time':cur['t']})
    return obs

def detect_fvg(candles, lookback=50):
    """Find Fair Value Gaps. A bullish FVG is where candle[i-1].high < candle[i+1].low.
    A bearish FVG is where candle[i-1].low > candle[i+1].high.
    """
    fvgs = []
    cs = candles[-lookback:] if len(candles) > lookback else candles
    for i in range(1, len(cs)-1):
        # Bullish FVG (unfilled gap up)
        if cs[i-1]['h'] < cs[i+1]['l']:
            fvgs.append({'type':'bullish','top':cs[i+1]['l'],'bottom':cs[i-1]['h'],'time':cs[i]['t']})
        # Bearish FVG
        if cs[i-1]['l'] > cs[i+1]['h']:
            fvgs.append({'type':'bearish','top':cs[i-1]['l'],'bottom':cs[i+1]['h'],'time':cs[i]['t']})
    return fvgs

def detect_key_levels(candles, lookback=100):
    """Recent swing highs/lows = key resistance/support."""
    cs = candles[-lookback:] if len(candles) > lookback else candles
    levels = []
    for i in range(2, len(cs)-2):
        # Pivot high: higher than 2 on each side
        if cs[i]['h'] > cs[i-1]['h'] and cs[i]['h'] > cs[i-2]['h'] and \
           cs[i]['h'] > cs[i+1]['h'] and cs[i]['h'] > cs[i+2]['h']:
            levels.append({'type':'resistance','price':cs[i]['h'],'time':cs[i]['t']})
        if cs[i]['l'] < cs[i-1]['l'] and cs[i]['l'] < cs[i-2]['l'] and \
           cs[i]['l'] < cs[i+1]['l'] and cs[i]['l'] < cs[i+2]['l']:
            levels.append({'type':'support','price':cs[i]['l'],'time':cs[i]['t']})
    return levels

def get_zones(ticker):
    """Cache-wrapped. Returns {obs, fvgs, levels, candles_count}."""
    now = time.time()
    cached = _zone_cache.get(ticker)
    if cached and (now - cached[0] < _ZONE_TTL):
        return cached[1]
    cs = _fetch_candles(ticker, interval='1h', days=5)
    if not cs:
        return None
    z = {
        'obs': detect_order_blocks(cs),
        'fvgs': detect_fvg(cs),
        'levels': detect_key_levels(cs),
        'last_price': cs[-1]['c'],
        'candles_count': len(cs),
    }
    _zone_cache[ticker] = (now, z)
    return z

def zone_confluence(ticker, direction, price, proximity_pct=0.3):
    """Check if signal price is near a confluent zone.
    Returns: dict with {aligned, zones_hit, distance_pct, size_boost}.
    BUY near bullish OB/FVG/support = aligned (boost size)
    BUY near bearish zone = contradicted (reduce size)
    Fail-safe: returns 1.0 multiplier if no data.
    """
    z = get_zones(ticker)
    if not z or price <= 0:
        return {'aligned': None, 'zones_hit': [], 'size_boost': 1.0, 'zone_available': False}
    
    hits = []
    boost_factors = []
    is_buy = direction.upper() == 'BUY'
    
    # Check OBs
    for ob in z['obs']:
        if ob['bottom'] <= price <= ob['top']:
            aligned = (is_buy and ob['type']=='bullish') or (not is_buy and ob['type']=='bearish')
            hits.append({'kind':'OB','type':ob['type'],'aligned':aligned})
            boost_factors.append(1.3 if aligned else 0.5)
    
    # Check FVGs
    for fvg in z['fvgs']:
        if fvg['bottom'] <= price <= fvg['top']:
            aligned = (is_buy and fvg['type']=='bullish') or (not is_buy and fvg['type']=='bearish')
            hits.append({'kind':'FVG','type':fvg['type'],'aligned':aligned})
            boost_factors.append(1.2 if aligned else 0.6)
    
    # Check key levels (within proximity)
    for lvl in z['levels']:
        dist_pct = abs(price - lvl['price']) / price * 100
        if dist_pct <= proximity_pct:
            # BUY near support = aligned. BUY near resistance = contradicted.
            aligned = (is_buy and lvl['type']=='support') or (not is_buy and lvl['type']=='resistance')
            hits.append({'kind':'LEVEL','type':lvl['type'],'dist_pct':round(dist_pct,2),'aligned':aligned})
            boost_factors.append(1.25 if aligned else 0.7)
    
    if not hits:
        return {'aligned': 'no_zone', 'zones_hit': [], 'size_boost': 1.0, 'zone_available': True}
    
    # Aggregate boost — geometric mean of all factors
    import math
    product = 1.0
    for f in boost_factors: product *= f
    boost = product ** (1.0/len(boost_factors))
    # Hard cap
    boost = max(0.3, min(2.5, boost))
    
    any_aligned = any(h['aligned'] for h in hits)
    any_contra = any(not h['aligned'] for h in hits)
    if any_aligned and not any_contra: status = 'aligned'
    elif any_contra and not any_aligned: status = 'contradicted'
    else: status = 'mixed'
    
    return {
        'aligned': status,
        'zones_hit': hits,
        'size_boost': round(boost, 2),
        'zone_available': True,
    }

def slippage_pct(wh_price, current_price):
    """How far has market moved since signal fired."""
    if wh_price <= 0 or current_price <= 0: return 0
    return abs(current_price - wh_price) / wh_price * 100
