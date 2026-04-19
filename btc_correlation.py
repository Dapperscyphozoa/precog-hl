"""BTC correlation guard. Multi-timeframe.
15min: >0.5% = directional conviction.
1h: >1% = slow drift = strict.
"""
import time, threading, urllib.request, json

_CACHE = {'ts': 0, 'btc_dir': 0, 'btc_move': 0, 'btc_1h_move': 0, 'btc_1h_dir': 0}
_LOCK = threading.Lock()

def _refresh():
    try:
        now = int(time.time()*1000)
        req = urllib.request.Request('https://api.hyperliquid.xyz/info',
            data=json.dumps({'type':'candleSnapshot','req':{
                'coin':'BTC','interval':'5m',
                'startTime': now - 90*60*1000, 'endTime': now}}).encode(),
            headers={'Content-Type':'application/json'})
        r = json.loads(urllib.request.urlopen(req, timeout=8).read())
        if not r or len(r) < 15: return
        latest = float(r[-1]['c'])
        # 15min
        old_15 = float(r[-3]['c'])
        move_15 = (latest - old_15) / old_15 if old_15 > 0 else 0
        # 1h
        old_1h = float(r[-12]['c']) if len(r) >= 12 else float(r[0]['c'])
        move_1h = (latest - old_1h) / old_1h if old_1h > 0 else 0
        dir_15 = 1 if move_15 > 0.005 else (-1 if move_15 < -0.005 else 0)
        dir_1h = 1 if move_1h > 0.01 else (-1 if move_1h < -0.01 else 0)
        with _LOCK:
            _CACHE['ts'] = time.time()
            _CACHE['btc_dir'] = dir_15
            _CACHE['btc_move'] = move_15
            _CACHE['btc_1h_dir'] = dir_1h
            _CACHE['btc_1h_move'] = move_1h
    except Exception as e:
        print(f"[btc_corr] err: {e}", flush=True)

def allow_alt_trade(coin, side):
    if coin in ('BTC', 'ETH'): return True
    now = time.time()
    with _LOCK:
        stale = now - _CACHE['ts'] > 60
    if stale: _refresh()
    with _LOCK:
        d15 = _CACHE['btc_dir']
        d1h = _CACHE['btc_1h_dir']
    want = 1 if side == 'BUY' else -1
    # Block if EITHER timeframe shows opposite direction
    if d15 != 0 and want != d15: return False
    if d1h != 0 and want != d1h: return False
    return True

def get_state():
    with _LOCK: return dict(_CACHE)
