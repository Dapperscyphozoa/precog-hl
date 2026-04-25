"""BTC correlation guard. Multi-timeframe.
Now uses EMA20-based trend classification on 1h/4h (matching mtf_context)
instead of 15-min raw price moves. The old +/-0.5% / 15min threshold
was too strict — in slow-grind bull markets, BTC spends >90% of time in
the 'neutral' zone (|move| < 0.5% over 15min) even when trending +2%
over 4h. That made btc_dir=0 most of the time, which in turn caused
confidence.score() to award 8pts to BOTH sides of every signal,
producing the 90% SELL bias we observed in bull-calm.

New logic: classify by 1h close vs 1h EMA20 (short-term trend) and
4h close vs 4h EMA20 (regime trend), same neutral buffer ±0.3% as
mtf_context. btc_dir is +1 / -1 / 0 based on 1h (fast), btc_1h_dir
and btc_4h_dir exposed for finer-grained use.
"""
import time, threading, urllib.request, json

_CACHE = {
    'ts': 0,
    'btc_dir': 0,       # 1h-based (replaces old 15m classification)
    'btc_move': 0,      # 1h % dist from EMA20
    'btc_1h_move': 0,
    'btc_1h_dir': 0,
    'btc_4h_dir': 0,
    'btc_4h_move': 0,
}
_LOCK = threading.Lock()
_NEUTRAL_BUF = 0.003  # ±0.3% from EMA20 = neutral zone

def _ema(vals, period):
    if len(vals) < period: return None
    e = sum(vals[:period]) / period
    k = 2 / (period + 1)
    for v in vals[period:]: e = v * k + e * (1 - k)
    return e

def _fetch_closes(interval, n=50):
    bar_ms = {'1h': 3600_000, '4h': 4*3600_000}[interval]
    now = int(time.time()*1000)
    # Share precog's HL throttle to prevent burst 429s across modules
    try:
        import precog as _p
        if hasattr(_p, '_hl_throttle'): _p._hl_throttle()
    except Exception: pass
    body = json.dumps({'type':'candleSnapshot','req':{
        'coin':'BTC','interval':interval,
        'startTime': now - n*bar_ms, 'endTime': now}}).encode()
    req = urllib.request.Request('https://api.hyperliquid.xyz/info',
        data=body, headers={'Content-Type':'application/json'})
    r = json.loads(urllib.request.urlopen(req, timeout=8).read())
    return [float(b['c']) for b in r] if r else []

def _refresh():
    try:
        # 1h trend (primary)
        closes_1h = _fetch_closes('1h', 50)
        if len(closes_1h) < 21: return
        ema20_1h = _ema(closes_1h, 20)
        last_1h = closes_1h[-1]
        dist_1h = (last_1h - ema20_1h) / ema20_1h if ema20_1h > 0 else 0
        dir_1h = 1 if dist_1h > _NEUTRAL_BUF else (-1 if dist_1h < -_NEUTRAL_BUF else 0)

        # 4h trend (regime)
        try:
            closes_4h = _fetch_closes('4h', 50)
            if len(closes_4h) >= 21:
                ema20_4h = _ema(closes_4h, 20)
                last_4h = closes_4h[-1]
                dist_4h = (last_4h - ema20_4h) / ema20_4h if ema20_4h > 0 else 0
                dir_4h = 1 if dist_4h > _NEUTRAL_BUF else (-1 if dist_4h < -_NEUTRAL_BUF else 0)
            else:
                dist_4h = 0; dir_4h = 0
        except Exception:
            dist_4h = 0; dir_4h = 0

        with _LOCK:
            _CACHE['ts'] = time.time()
            _CACHE['btc_dir'] = dir_1h           # primary (1h-based now)
            _CACHE['btc_move'] = dist_1h
            _CACHE['btc_1h_dir'] = dir_1h
            _CACHE['btc_1h_move'] = dist_1h
            _CACHE['btc_4h_dir'] = dir_4h
            _CACHE['btc_4h_move'] = dist_4h
    except Exception as e:
        print(f"[btc_corr] err: {e}", flush=True)

def allow_alt_trade(coin, side):
    """Block alt trade if either 1h or 4h BTC trend opposes the trade direction.
    Fail-open: neutral TF passes through (can't determine, don't block)."""
    if coin in ('BTC', 'ETH'): return True
    now = time.time()
    with _LOCK: stale = now - _CACHE['ts'] > 60
    if stale: _refresh()
    with _LOCK:
        d1h = _CACHE['btc_1h_dir']
        d4h = _CACHE['btc_4h_dir']
    want = 1 if side == 'BUY' else -1
    # Block if either HTF explicitly opposes (not neutral)
    if d1h != 0 and want != d1h: return False
    if d4h != 0 and want != d4h: return False
    return True

def get_state():
    with _LOCK: return dict(_CACHE)

