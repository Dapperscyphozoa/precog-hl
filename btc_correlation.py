"""BTC correlation guard. When BTC moves >0.5% in 15min, confirm alt trades align with BTC direction.
Prevents correlated alt-blowup clusters (10 alts dumping same hour while BTC pumps).
"""
import time, threading, urllib.request, json

WINDOW_SEC = 900
MOVE_THRESHOLD = 0.005
_CACHE = {'ts': 0, 'btc_dir': 0, 'btc_move': 0}
_LOCK = threading.Lock()

def _refresh():
    try:
        # HL direct fetch — reliable, no WS dependency
        now = int(time.time()*1000)
        req = urllib.request.Request('https://api.hyperliquid.xyz/info',
            data=json.dumps({'type':'candleSnapshot','req':{
                'coin':'BTC','interval':'5m',
                'startTime': now - 20*60*1000, 'endTime': now}}).encode(),
            headers={'Content-Type':'application/json'})
        r = json.loads(urllib.request.urlopen(req, timeout=8).read())
        if not r or len(r) < 3: return
        latest = float(r[-1]['c']); old = float(r[-3]['c'])
        move = (latest - old) / old if old > 0 else 0
        direction = 1 if move > MOVE_THRESHOLD else (-1 if move < -MOVE_THRESHOLD else 0)
        with _LOCK:
            _CACHE['ts'] = time.time()
            _CACHE['btc_dir'] = direction
            _CACHE['btc_move'] = move
    except Exception as e:
        print(f"[btc_corr] refresh err: {e}", flush=True)

def allow_alt_trade(coin, side):
    if coin in ('BTC', 'ETH'): return True
    now = time.time()
    with _LOCK:
        stale = now - _CACHE['ts'] > 60
    if stale: _refresh()
    with _LOCK:
        direction = _CACHE['btc_dir']
    if direction == 0: return True
    want_dir = 1 if side == 'BUY' else -1
    return want_dir == direction

def get_state():
    with _LOCK: return dict(_CACHE)
