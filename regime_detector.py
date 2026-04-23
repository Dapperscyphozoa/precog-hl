"""Live regime detector — BTC 1h primary + 30m confirm.

Classifies market regime from BTC 1h candles. Requires 30m agreement before
flipping. Hysteresis + caching prevents oscillation.

Regimes:  bull-calm | bull-storm | bear-calm | bear-storm | chop

Input: HL candle API (1h and 30m BTC).
Cache: 5 min.
"""
import time, urllib.request, json
import numpy as np

_CACHE = {'regime': None, 'ts': 0, 'history_1h': [], 'history_30m': []}
CACHE_SEC = 300
EMA_PERIOD = 20
VOL_WINDOW = 24       # 24h of 1h returns
TREND_BUF = 0.005     # 0.5% from EMA20 = chop band
HYSTERESIS = 2        # 2 confirming 1h closes to flip
VOL_SPLIT = 0.00957   # 1h return stdev split


def _ema(vals, period):
    if len(vals) < period: return None
    e = sum(vals[:period]) / period
    k = 2 / (period + 1)
    for v in vals[period:]:
        e = v * k + e * (1 - k)
    return e


def _fetch_btc_candles(interval, n_bars):
    end_ms = int(time.time() * 1000)
    ms_per_bar = {'1h': 3600_000, '30m': 1800_000}[interval]
    start_ms = end_ms - n_bars * ms_per_bar
    body = json.dumps({
        'type': 'candleSnapshot',
        'req': {'coin': 'BTC', 'interval': interval,
                'startTime': start_ms, 'endTime': end_ms}
    }).encode()
    req = urllib.request.Request('https://api.hyperliquid.xyz/info',
        data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def _classify(closes):
    """Return (regime, dist_pct, vol_pct). None tuple if insufficient data."""
    if len(closes) < EMA_PERIOD + VOL_WINDOW:
        return (None, None, None)
    ema = _ema(closes, EMA_PERIOD)
    if ema is None: return (None, None, None)
    dist = (closes[-1] - ema) / ema
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(len(closes) - VOL_WINDOW, len(closes))]
    vol = float(np.std(rets))
    if abs(dist) < TREND_BUF:
        return ('chop', dist, vol)
    trend = 'bull' if dist > 0 else 'bear'
    vol_label = 'storm' if vol > VOL_SPLIT else 'calm'
    return (f'{trend}-{vol_label}', dist, vol)


def get_regime(force=False):
    now = time.time()
    if not force and _CACHE['regime'] and (now - _CACHE['ts']) < CACHE_SEC:
        return _CACHE['regime']
    try:
        bars_1h = _fetch_btc_candles('1h', 60)
        bars_30m = _fetch_btc_candles('30m', 60)
        c_1h = [float(b['c']) for b in bars_1h]
        c_30m = [float(b['c']) for b in bars_30m]

        raw_1h, dist_1h, vol_1h = _classify(c_1h)
        raw_30m, _, _ = _classify(c_30m)
        if raw_1h is None:
            return _CACHE['regime']

        # 30m must confirm before a regime flip is considered
        new_candidate = raw_1h
        if raw_30m is not None and raw_30m != raw_1h:
            if _CACHE['regime']:
                new_candidate = _CACHE['regime']  # disagreement = hold prior

        # 2-bar hysteresis on 1h
        history = _CACHE.get('history_1h', [])
        history.append(raw_1h)
        if len(history) > HYSTERESIS: history = history[-HYSTERESIS:]

        prev = _CACHE['regime']
        if prev is None:
            _CACHE['regime'] = new_candidate
        elif new_candidate == prev:
            pass
        elif len(history) >= HYSTERESIS and all(h == new_candidate for h in history):
            _CACHE['regime'] = new_candidate

        _CACHE['ts'] = now
        _CACHE['history_1h'] = history
        _CACHE['btc_dist_1h'] = round(dist_1h * 100, 2) if dist_1h is not None else None
        _CACHE['btc_vol_1h'] = round(vol_1h * 100, 3) if vol_1h is not None else None
        _CACHE['raw_30m'] = raw_30m
        _CACHE['raw_1h'] = raw_1h
        return _CACHE['regime']
    except Exception:
        return _CACHE.get('regime')


def status():
    return {
        'current_regime': _CACHE.get('regime'),
        'raw_1h': _CACHE.get('raw_1h'),
        'raw_30m': _CACHE.get('raw_30m'),
        'btc_dist_from_ema20_1h_pct': _CACHE.get('btc_dist_1h'),
        'btc_vol_1h_pct': _CACHE.get('btc_vol_1h'),
        'last_check_age_sec': int(time.time() - _CACHE.get('ts', 0)) if _CACHE.get('ts') else None,
        'recent_history_1h': _CACHE.get('history_1h', []),
        'vol_median_split': VOL_SPLIT,
        'trend_buffer_pct': TREND_BUF * 100,
        'ema_period': EMA_PERIOD,
        'hysteresis_bars_1h': HYSTERESIS,
    }
