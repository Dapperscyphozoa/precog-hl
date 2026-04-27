"""Open Interest tracker. Polls HL meta_and_asset_ctxs every 5min.
Rising OI + rising price = new longs = trend continuation.
Rising OI + falling price = new shorts = trend continuation down.
Falling OI + price move = short/long covering = exhaustion.

2026-04-27: switched from Binance OI HTTP poll to HL native data.
Binance feed was returning 0 tracked coins (likely geo-block from Render
IP region). HL meta_and_asset_ctxs already exposes openInterest per coin
in the asset_contexts array — same data, no geo-restrictions, single
HTTP call covers ALL coins instead of one-per-symbol.
"""
import threading, time
from collections import defaultdict, deque

_OI = defaultdict(lambda: deque(maxlen=288))  # 24h at 5min intervals
_LOCK = threading.Lock()
_RUN = False
_LAST_POLL_OK = 0
_LAST_POLL_ERR = ''

# Legacy COINS list kept for backward compat / fallback. No longer used in
# _poll (which now grabs all HL coins automatically).
COINS = ['BTC','ETH','SOL','XRP','ADA','AVAX','LINK','BNB','DOT','ATOM','SUI','DOGE',
         'WIF','ORDI','TIA','APT','FIL','LTC','OP','ARB','INJ','LDO','AAVE']

def _poll():
    """Single HL meta_and_asset_ctxs call updates OI for ALL coins."""
    global _LAST_POLL_OK, _LAST_POLL_ERR
    try:
        # Lazy import to avoid circular dep at module load time
        import precog as _precog
        if not hasattr(_precog, 'info') or _precog.info is None:
            _LAST_POLL_ERR = 'precog.info not ready'
            return
        meta_ctxs = _precog.info.meta_and_asset_ctxs()
        if not meta_ctxs or len(meta_ctxs) < 2:
            _LAST_POLL_ERR = 'meta_and_asset_ctxs returned empty'
            return
        meta = meta_ctxs[0]
        ctxs = meta_ctxs[1]
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
    except Exception as e:
        _LAST_POLL_ERR = f'{type(e).__name__}: {e}'

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
    """Combine OI delta with price direction.
    Rising OI + up = bullish continuation (+1)
    Rising OI + down = bearish continuation (-1)
    Falling OI = covering = reversal risk (0)
    """
    delta = get_delta(coin)
    import os as _os_oi
    _oi_thresh = float(_os_oi.environ.get('OI_DELTA_MIN_PCT', '0.007'))
    if abs(delta) < _oi_thresh: return 0  # below threshold = no signal
    if delta > 0:
        return 1 if price_dir > 0 else -1
    return 0  # covering, don't signal

def status():
    with _LOCK:
        tracked = len([c for c,d in _OI.items() if d])
    return {
        'tracked': tracked,
        'coins': list(_OI.keys())[:10],
        'source': 'HL meta_and_asset_ctxs',
        'last_poll_ok': _LAST_POLL_OK,
        'last_poll_age_sec': int(time.time() - _LAST_POLL_OK) if _LAST_POLL_OK else -1,
        'last_poll_err': _LAST_POLL_ERR,
    }

def _runner():
    while _RUN:
        try: _poll()
        except Exception as e: print(f"[oi] {e}", flush=True)
        time.sleep(300)

def start():
    global _RUN
    if _RUN: return
    _RUN = True
    threading.Thread(target=_runner, daemon=True, name='oi').start()
    print("[oi] started (HL native source)", flush=True)
