"""Open Interest tracker. Polls HL meta_and_asset_ctxs (via meta_cache) every 5min.
Rising OI + rising price = new longs = trend continuation.
Rising OI + falling price = new shorts = trend continuation down.
Falling OI + price move = short/long covering = exhaustion.

2026-04-27: switched from Binance OI HTTP poll to HL native data.
2026-05-11: switched HL call to meta_cache singleton (shared with 3 other
consumers). Adds randomized startup jitter, adaptive poll interval, and
honours 429 backoff floor set by meta_cache. Eliminates the
"CloudFront 429 on every 5min OI cycle" pattern.
"""
import os, threading, time, random
from collections import defaultdict, deque

_OI = defaultdict(lambda: deque(maxlen=288))  # 24h at 5min intervals
_LOCK = threading.Lock()
_RUN = False
_LAST_POLL_OK = 0
_LAST_POLL_ERR = ''
_CURRENT_INTERVAL = float(os.environ.get('OI_POLL_INTERVAL_SEC', '300'))
_BASE_INTERVAL = _CURRENT_INTERVAL
_MAX_INTERVAL = float(os.environ.get('OI_POLL_MAX_INTERVAL_SEC', '900'))
_STARTUP_JITTER_MAX = float(os.environ.get('OI_STARTUP_JITTER_SEC', '30'))

# Legacy COINS list kept for backward compat. _poll grabs all HL coins.
COINS = ['BTC','ETH','SOL','XRP','ADA','AVAX','LINK','BNB','DOT','ATOM','SUI','DOGE',
         'WIF','ORDI','TIA','APT','FIL','LTC','OP','ARB','INJ','LDO','AAVE']

def _poll():
    """Single shared meta_and_asset_ctxs call updates OI for ALL coins."""
    global _LAST_POLL_OK, _LAST_POLL_ERR, _CURRENT_INTERVAL
    try:
        import meta_cache
        # OI is happy with up to 5min-old data — we're tracking 15min deltas
        meta_ctxs = meta_cache.get_meta_ctxs(max_age_sec=300)
        if not meta_ctxs:
            _LAST_POLL_ERR = 'meta_cache returned None (likely 429 backoff or info not ready)'
            # Slow down our polling to give the cache time to recover
            _CURRENT_INTERVAL = min(_MAX_INTERVAL, _CURRENT_INTERVAL * 1.5)
            return
        meta, ctxs = meta_ctxs
        coins_list = [u.get('name', '') for u in meta.get('universe', [])]
        now_ts = time.time()
        updated = 0
        with _LOCK:
            for i, coin in enumerate(coins_list):
                if i >= len(ctxs):
                    break
                if not coin:
                    continue
                try:
                    oi = float(ctxs[i].get('openInterest', 0) or 0)
                    if oi > 0:
                        _OI[coin].append((now_ts, oi))
                        updated += 1
                except (TypeError, ValueError):
                    continue
        _LAST_POLL_OK = int(now_ts)
        _LAST_POLL_ERR = ''
        # Successful poll → relax back toward base interval
        _CURRENT_INTERVAL = max(_BASE_INTERVAL, _CURRENT_INTERVAL * 0.8)
    except Exception as e:
        _LAST_POLL_ERR = f'{type(e).__name__}: {e}'
        _CURRENT_INTERVAL = min(_MAX_INTERVAL, _CURRENT_INTERVAL * 1.5)

def _fetch(coin):
    """Legacy helper — kept for backward compat. Not used by _poll anymore."""
    return None

def get_delta(coin, window_sec=900):
    """Returns OI % change over window."""
    with _LOCK:
        data = list(_OI.get(coin, []))
    if len(data) < 2: return 0
    cutoff = time.time() - window_sec
    past = [x for x in data if x[0] <= cutoff]
    if not past: return 0
    old = past[-1][1]; now = data[-1][1]
    if old == 0: return 0
    return (now - old) / old

def oi_bias(coin, price_dir):
    """Combine OI delta with price direction."""
    delta = get_delta(coin)
    _oi_thresh = float(os.environ.get('OI_DELTA_MIN_PCT', '0.007'))
    if abs(delta) < _oi_thresh: return 0
    if delta > 0:
        return 1 if price_dir > 0 else -1
    return 0

def status():
    with _LOCK:
        tracked = len([c for c,d in _OI.items() if d])
    return {
        'tracked': tracked,
        'coins': list(_OI.keys())[:10],
        'source': 'HL meta_and_asset_ctxs (via meta_cache)',
        'last_poll_ok': _LAST_POLL_OK,
        'last_poll_age_sec': int(time.time() - _LAST_POLL_OK) if _LAST_POLL_OK else -1,
        'last_poll_err': _LAST_POLL_ERR,
        'current_interval_sec': round(_CURRENT_INTERVAL, 1),
        'base_interval_sec': _BASE_INTERVAL,
    }

def _runner():
    # Randomized startup jitter — desynchronizes OI poll from other 5min cycles
    # (snapshot rebuild, funding, etc) so we don't all hit /info at the same tick.
    initial_delay = random.uniform(0, _STARTUP_JITTER_MAX)
    print(f"[oi] startup delay {initial_delay:.1f}s for desync", flush=True)
    time.sleep(initial_delay)
    while _RUN:
        try: _poll()
        except Exception as e: print(f"[oi] {e}", flush=True)
        # Adaptive interval with small per-cycle jitter
        sleep_for = _CURRENT_INTERVAL + random.uniform(0, 10)
        time.sleep(sleep_for)

def start():
    global _RUN
    if _RUN: return
    _RUN = True
    threading.Thread(target=_runner, daemon=True, name='oi').start()
    print(f"[oi] started (meta_cache singleton, base interval {_BASE_INTERVAL}s)", flush=True)
