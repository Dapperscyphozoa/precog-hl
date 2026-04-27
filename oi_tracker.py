"""Open Interest tracker. Polls Binance OI API every 5min. Major shifts = positioning signal.
Rising OI + rising price = new longs = trend continuation.
Rising OI + falling price = new shorts = trend continuation down.
Falling OI + price move = short/long covering = exhaustion.
"""
import threading, time, urllib.request, json
from collections import defaultdict, deque

_OI = defaultdict(lambda: deque(maxlen=288))  # 24h at 5min intervals
_LOCK = threading.Lock()
_RUN = False

COINS = ['BTC','ETH','SOL','XRP','ADA','AVAX','LINK','BNB','DOT','ATOM','SUI','DOGE',
         'WIF','ORDI','TIA','APT','FIL','LTC','OP','ARB','INJ','LDO','AAVE']

def _fetch(coin):
    sym = f"{coin}USDT"
    try:
        url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}"
        r = json.loads(urllib.request.urlopen(url, timeout=10).read())
        return float(r.get('openInterest', 0))
    except Exception: return None

def _poll():
    for coin in COINS:
        oi = _fetch(coin)
        if oi:
            with _LOCK:
                _OI[coin].append((time.time(), oi))

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
    # 2026-04-27: 1% → 0.7% threshold via env override.
    # /confluence engine_stats showed zero oi_contributed — quiet regime.
    # 0.7% still requires meaningful 15min OI move.
    import os as _os_oi
    _oi_thresh = float(_os_oi.environ.get('OI_DELTA_MIN_PCT', '0.007'))
    if abs(delta) < _oi_thresh: return 0  # below threshold = no signal
    if delta > 0:
        return 1 if price_dir > 0 else -1
    return 0  # covering, don't signal

def status():
    with _LOCK:
        tracked = len([c for c,d in _OI.items() if d])
    return {'tracked': tracked, 'coins': list(_OI.keys())[:10]}

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
    print("[oi] started", flush=True)
