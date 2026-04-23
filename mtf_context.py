"""Multi-timeframe context cache.

Provides per-coin bias on 30m and 1h timeframes for use as a confluence gate
on top of 15m signal engines. 15m is the entry trigger, 30m + 1h define
the trend context for that trigger TF.

Why 30m/1h (not 1h/4h): 2026-04-22. A 15m trigger with 4h filter is a
16x TF mismatch — 4h holds a bullish bias for hours after intraday
reversals, causing the bot to stack longs into 15m pullbacks of a
4h-still-up regime. Correct hierarchy for a 15m trigger: trend filter
at 2x-4x the trigger TF (= 30m and 1h). This keeps the filter
responsive to the same drift the bot is trading.

API:
  get_bias(coin, interval) -> ('up', 'down', 'neutral', dict_details)
  aligned(coin, side) -> (bool, str, float)  # 30m AND 1h confluence
  conviction_mult(coin, side) -> (float, dict)  # sizing boost if aligned

Implementation:
- HL candleSnapshot API for 30m/1h
- 5-minute cache per (coin, interval)
- EMA20 bias classifier with 1% buffer for neutral zone
- Fails open on API/data error (returns neutral so trade isn't blocked by infra)
"""
import time
import json
import urllib.request
import threading
import os

_LOCK = threading.Lock()
_CACHE = {}          # (coin, interval) -> {'bias', 'ts', 'close', 'ema20', 'dist_pct'}
_CACHE_TTL_SEC = 300 # 5 min

# Neutral zone: ±NEUTRAL_BUF from EMA20 counts as neutral (both sides allowed).
# Widened 2026-04-22 from 0.3% to 1.0% after the regime filter produced
# ZERO trades in a bull-calm market. Coins drifting +0.4% were being
# classified as "bullish" which blocked all shorts — too restrictive.
# At 1% buffer: only clearly-trending coins (>1% from EMA20) block the
# opposing side. Env override: MTF_NEUTRAL_BUF=0.01 (default).
_NEUTRAL_BUF = float(os.environ.get('MTF_NEUTRAL_BUF', '0.01'))

# Conviction scoring ramp. When buffer is 1%, a coin at exactly +1% is
# "directional" but only barely. Full conviction boost requires stronger
# signal. Ramp score from NEUTRAL_BUF (0 score) up to MAX_CONVICTION_DIST
# (1.0 score). Above that: clipped. Env: MTF_CONVICTION_MAX_DIST=0.025
_CONV_MAX_DIST = float(os.environ.get('MTF_CONVICTION_MAX_DIST', '0.025'))

# Interval → (bars to fetch, bar duration in ms)
_INTERVAL_MS = {
    '15m': 15 * 60 * 1000,
    '30m': 30 * 60 * 1000,
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
    """Check 15m signal `side` ('BUY'/'SELL') against 30m + 1h bias.

    2026-04-22: Changed from 1h+4h to 30m+1h. A 15m trigger with a 4h
    filter was structurally mismatched — 4h held a bullish bias for
    hours after intraday reversals, causing the bot to keep taking
    longs into 15m pullbacks of a 4h-still-up regime. Correct hierarchy
    for 15m trades: trend filter at 2x-4x trigger TF = 30m + 1h.

    Returns (ok: bool, detail: str, size_mult: float).
      ok=True, size_mult=1.0: at least one HTF favors or both neutral
      ok=True, size_mult=PARTIAL_MULT: exactly one HTF opposes, other neutral/favors
      ok=False: BOTH HTFs oppose (hard block)

    Env override: MTF_PARTIAL_SIZE=0.3 (default 30% size on partial mismatch).
    """
    bias_30m, d_30m = get_bias(coin, '30m')
    bias_1h, d_1h = get_bias(coin, '1h')
    needed = 'up' if side == 'BUY' else 'down'
    opposite = 'down' if needed == 'up' else 'up'
    op_30m = bias_30m == opposite
    op_1h = bias_1h == opposite
    partial_mult = float(os.environ.get('MTF_PARTIAL_SIZE', '0.3'))
    if op_30m and op_1h:
        return False, f"BOTH HTFs oppose {side}: 30m={bias_30m} 1h={bias_1h}", 0.0
    if op_30m or op_1h:
        which = '30m' if op_30m else '1h'
        return True, f"partial: {which}={opposite} opposes {side}, other={bias_1h if which=='30m' else bias_30m} (size_mult={partial_mult})", partial_mult
    return True, f"30m={bias_30m} 1h={bias_1h} aligned_with={side}", 1.0

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

def conviction_mult(coin, side, max_mult=2.5):
    """Return a sizing multiplier (1.0 to max_mult) based on 30m + 1h alignment strength.

    2026-04-22: Changed from 1h+4h to 30m+1h to match aligned() fix.

    Called AFTER aligned() has already passed — here we only reward FAVORABLE
    alignment strength. A trade where both TFs are strongly aligned (at or
    beyond _CONV_MAX_DIST in correct direction) gets close to max_mult.
    A trade where both TFs sit within _NEUTRAL_BUF gets 1.0x (neutral).

    Ramp: score_tf = clamp((fav_dist - NEUTRAL_BUF) / (CONV_MAX - NEUTRAL_BUF), 0, 1)
      fav_dist within neutral buffer  → 0 score
      fav_dist at CONV_MAX (2.5% default) → 1.0 score (max)

    Combined: 0.5 × score_30m + 0.5 × score_1h (equal weighting — both
    timeframes are close to the trigger TF, neither dominates).
    Multiplier: 1.0 + (max_mult − 1.0) × combined.

    Fail-soft: on any error, returns 1.0 (no sizing change).
    """
    try:
        _, d30 = get_bias(coin, '30m')
        _, d1 = get_bias(coin, '1h')
        raw_30m_pct = d30.get('dist_pct', 0)
        raw_1h_pct = d1.get('dist_pct', 0)
        needed_sign = 1 if side == 'BUY' else -1
        fav_30m = raw_30m_pct * needed_sign  # positive = favorable
        fav_1h = raw_1h_pct * needed_sign
        # Score ramps from NEUTRAL_BUF_% up to CONV_MAX_DIST_%
        neutral_pct = _NEUTRAL_BUF * 100
        max_pct = _CONV_MAX_DIST * 100
        span = max(1e-9, max_pct - neutral_pct)
        def _score(fav):
            excess = fav - neutral_pct
            if excess <= 0: return 0.0
            return min(1.0, excess / span)
        score_30m = _score(fav_30m)
        score_1h = _score(fav_1h)
        combined = 0.5 * score_30m + 0.5 * score_1h
        mult = 1.0 + (max_mult - 1.0) * combined
        return round(mult, 2), {
            'score_30m': round(score_30m, 2), 'score_1h': round(score_1h, 2),
            'fav_30m_pct': round(fav_30m, 3), 'fav_1h_pct': round(fav_1h, 3),
            'combined': round(combined, 2),
            'neutral_buf_pct': round(neutral_pct, 3),
            'conv_max_pct': round(max_pct, 3),
        }
    except Exception as e:
        return 1.0, {'err': str(e)}
