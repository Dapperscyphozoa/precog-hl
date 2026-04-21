"""Live regime detector. Determines current market regime from BTC 4h data.
Caches result for 5 minutes (regime can't shift faster than that with hysteresis)."""
import time, urllib.request, json
import numpy as np

_CACHE = {'regime': None, 'ts': 0, 'history': []}
CACHE_SEC = 300  # 5 min
TREND_BUFFER = 0.005  # ±0.5% from EMA9 = chop
HYSTERESIS = 3  # bars
VOL_MEDIAN = 0.00957  # from 90d historical (regenerate weekly)

def _ema(vals, period):
    if len(vals) < period: return None
    e = sum(vals[:period])/period
    k = 2/(period+1)
    for v in vals[period:]:
        e = v*k + e*(1-k)
    return e

def _fetch_btc_4h(n_bars=40):
    """Fetch last n_bars of BTC 4h candles."""
    end_ms = int(time.time()*1000)
    start_ms = end_ms - n_bars*4*3600*1000
    req = urllib.request.Request('https://api.hyperliquid.xyz/info',
        data=json.dumps({"type":"candleSnapshot",
                         "req":{"coin":"BTC","interval":"4h","startTime":start_ms,"endTime":end_ms}}).encode(),
        headers={'Content-Type':'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

def get_regime(force=False):
    """Returns current regime: bull-calm/bull-storm/bear-calm/bear-storm/chop, or None on error."""
    now = time.time()
    if not force and _CACHE['regime'] and (now - _CACHE['ts']) < CACHE_SEC:
        return _CACHE['regime']
    
    try:
        bars = _fetch_btc_4h(40)
        if len(bars) < 12: return _CACHE['regime']  # fallback to last known
        closes = [float(b['c']) for b in bars]
        ema9 = _ema(closes, 9)
        if ema9 is None: return _CACHE['regime']
        
        last_px = closes[-1]
        dist = (last_px - ema9) / ema9
        
        # Vol: stdev of last 30 returns (or what we have)
        rets = [(closes[i]/closes[i-1]-1) for i in range(1, len(closes))]
        if len(rets) < 5: return _CACHE['regime']
        vol = float(np.std(rets[-min(30,len(rets)):]))
        
        # Classify
        if abs(dist) < TREND_BUFFER:
            raw = 'chop'
        else:
            trend = 'bull' if dist > 0 else 'bear'
            vol_label = 'storm' if vol > VOL_MEDIAN else 'calm'
            raw = f"{trend}-{vol_label}"
        
        # Hysteresis: don't flip unless persistent
        history = _CACHE.get('history', [])
        history.append(raw)
        if len(history) > HYSTERESIS: history = history[-HYSTERESIS:]
        
        prev = _CACHE['regime']
        if prev is None:
            _CACHE['regime'] = raw
        elif raw == prev:
            pass  # confirm
        elif len(history) >= HYSTERESIS and all(h == raw for h in history):
            _CACHE['regime'] = raw  # flip
        # else: keep previous regime (hysteresis active)
        
        _CACHE['ts'] = now
        _CACHE['history'] = history
        _CACHE['btc_dist'] = round(dist*100, 2)
        _CACHE['btc_vol'] = round(vol*100, 3)
        return _CACHE['regime']
    
    except Exception as e:
        return _CACHE.get('regime')  # silent fallback to last known

def status():
    """Return full regime status for /regime endpoint."""
    return {
        'current_regime': _CACHE.get('regime'),
        'last_check_age_sec': int(time.time() - _CACHE.get('ts', 0)) if _CACHE.get('ts') else None,
        'btc_dist_from_ema9_pct': _CACHE.get('btc_dist'),
        'btc_vol_pct': _CACHE.get('btc_vol'),
        'recent_history': _CACHE.get('history', []),
        'vol_median_split': VOL_MEDIAN,
        'trend_buffer_pct': TREND_BUFFER * 100,
        'hysteresis_bars': HYSTERESIS,
    }
