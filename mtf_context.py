"""Multi-timeframe context cache.

Provides per-coin bias on 1h and 4h timeframes for use as a confluence gate
on top of 15m signal engines. 15m is the entry trigger, 1h defines the
trade thesis, 4h confirms the regime.

Why this matters: the 51-trade audit (2026-04-22) showed 100% of dust-swept
outcomes within ±1% of entry. The bot was trading 15m micro-reversals with
no higher-timeframe context. Counter-trend shorts on 15m into a 4h bull
structure were guaranteed losers. MTF confluence is the strategic fix.

API:
  get_bias(coin, interval) -> ('up', 'down', 'neutral', dict_details)
  aligned(coin, side) -> (bool, str)  # both 1h AND 4h agree with side

Implementation:
- HL candleSnapshot API for 1h/4h (same as regime_detector.py)
- 5-minute cache per (coin, interval)
- EMA20 bias classifier with 0.3% buffer for neutral zone
- Fails open on API/data error (returns neutral so trade isn't blocked by infra)
"""
import time
import json
import urllib.request
import threading

_LOCK = threading.Lock()
_CACHE = {}          # (coin, interval) -> {'bias', 'ts', 'close', 'ema20', 'dist_pct'}
_CACHE_TTL_SEC = 300 # 5 min
_NEUTRAL_BUF = 0.003 # ±0.3% from EMA20 = neutral zone

# Interval → (bars to fetch, bar duration in ms)
_INTERVAL_MS = {
    '15m': 15 * 60 * 1000,
    '1h': 60 * 60 * 1000,
    '4h': 4 * 60 * 60 * 1000,
}

def _ema(vals, period):
    if len(vals) < period:
        return None
    e = sum(vals[:period]) / period
    k = 2 / (period + 1)
    for v in vals[period:]:
        e = v * k + e * (1 - k)
    return e

def _fetch_candles(coin, interval, n_bars=50):
    """Fetch n_bars most recent candles from HL for the given interval."""
    bar_ms = _INTERVAL_MS.get(interval)
    if not bar_ms:
        raise ValueError(f"unsupported interval: {interval}")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - n_bars * bar_ms
    body = json.dumps({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}
    }).encode()
    req = urllib.request.Request(
        'https://api.hyperliquid.xyz/info',
        data=body,
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())

def get_bias(coin, interval='1h'):
    """Return ('up'|'down'|'neutral', detail_dict).
    Cached for 5 min per (coin, interval). Fails open on error (returns neutral)."""
    key = (coin, interval)
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and (now - cached['ts']) < _CACHE_TTL_SEC:
            return cached['bias'], cached
    try:
        bars = _fetch_candles(coin, interval, n_bars=50)
        if not bars or len(bars) < 21:
            return 'neutral', {'err': 'insufficient_data', 'n': len(bars) if bars else 0}
        closes = [float(b['c']) for b in bars]
        ema20 = _ema(closes, 20)
        if ema20 is None or ema20 <= 0:
            return 'neutral', {'err': 'ema_calc_failed'}
        last = closes[-1]
        dist = (last - ema20) / ema20
        if dist > _NEUTRAL_BUF:
            bias = 'up'
        elif dist < -_NEUTRAL_BUF:
            bias = 'down'
        else:
            bias = 'neutral'
        detail = {
            'bias': bias, 'ts': now, 'close': last, 'ema20': ema20,
            'dist_pct': round(dist * 100, 3), 'interval': interval, 'coin': coin,
        }
        with _LOCK:
            _CACHE[key] = detail
        return bias, detail
    except Exception as e:
        return 'neutral', {'err': str(e)}

def aligned(coin, side):
    """Check 15m signal `side` ('BUY'/'SELL') against 1h + 4h bias.
    Returns (bool aligned, str detail).
    Fail-open: if any TF is 'neutral' the trade is allowed (no blocker).
    Only blocks when HTF explicitly OPPOSES the signal direction.
    """
    bias_1h, d_1h = get_bias(coin, '1h')
    bias_4h, d_4h = get_bias(coin, '4h')
    needed = 'up' if side == 'BUY' else 'down'
    opposite = 'down' if needed == 'up' else 'up'
    # Reject only when HTF is explicitly opposite. Neutral passes through.
    if bias_1h == opposite:
        return False, f"1h={bias_1h} (dist={d_1h.get('dist_pct','?')}%) opposes {side}"
    if bias_4h == opposite:
        return False, f"4h={bias_4h} (dist={d_4h.get('dist_pct','?')}%) opposes {side}"
    return True, f"1h={bias_1h} 4h={bias_4h} aligned_with={side}"

def status():
    """Snapshot of cache for /mtf endpoint."""
    with _LOCK:
        items = []
        for (c, iv), d in list(_CACHE.items()):
            items.append({
                'coin': c, 'interval': iv, 'bias': d.get('bias'),
                'dist_pct': d.get('dist_pct'), 'age_sec': int(time.time() - d.get('ts', 0)),
            })
    return {'cached': len(items), 'ttl_sec': _CACHE_TTL_SEC, 'neutral_buffer_pct': _NEUTRAL_BUF*100, 'entries': items}
