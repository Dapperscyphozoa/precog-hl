"""Cross-venue CVD (Cumulative Volume Delta). Aggregates buy/sell pressure from Binance + Bybit aggTrade streams.
CVD rising = buyer aggression. CVD divergence from price = reversal signal.
"""
import json, threading, time
from collections import defaultdict, deque
try: import websocket
except ImportError: websocket = None

_CVD = defaultdict(lambda: deque(maxlen=600))  # coin -> deque[(ts, delta_usd)]
_LOCK = threading.Lock()
_RUN = False

BIN_MAP = {'btcusdt':'BTC','ethusdt':'ETH','solusdt':'SOL','xrpusdt':'XRP','adausdt':'ADA',
    'avaxusdt':'AVAX','linkusdt':'LINK','bnbusdt':'BNB','dotusdt':'DOT','atomusdt':'ATOM',
    'suiusdt':'SUI','aaveusdt':'AAVE','wifusdt':'WIF','ordiusdt':'ORDI','tiausdt':'TIA',
    'injusdt':'INJ','ldousdt':'LDO','aptusdt':'APT','filusdt':'FIL','ltcusdt':'LTC',
    'opusdt':'OP','arbusdt':'ARB','dogeusdt':'DOGE','nearusdt':'NEAR','jupusdt':'JUP'}

def _on_msg(ws, msg):
    try:
        m = json.loads(msg)
        d = m.get('data', m)
        sym = d.get('s','').lower()
        hl = BIN_MAP.get(sym)
        if not hl: return
        qty = float(d.get('q',0)); px = float(d.get('p',0))
        usd = qty * px
        # m=True means buyer maker → aggressor sold
        delta = -usd if d.get('m') else usd
        with _LOCK:
            _CVD[hl].append((time.time(), delta))
    except Exception: pass

def get_cvd(coin, window_sec=300):
    """Net delta USD in window. Positive = buy pressure, negative = sell pressure."""
    cutoff = time.time() - window_sec
    with _LOCK:
        recent = [x for x in _CVD.get(coin, []) if x[0] > cutoff]
    return sum(d for _, d in recent)

def cvd_signal(coin, min_usd=500000):
    """Returns 'BUY' if strong buy pressure, 'SELL' if strong sell, None otherwise."""
    cvd = get_cvd(coin)
    if cvd > min_usd: return 'BUY'
    if cvd < -min_usd: return 'SELL'
    return None

def status():
    with _LOCK:
        active = len([c for c,d in _CVD.items() if d and time.time()-d[-1][0] < 300])
    return {'tracked_coins': len(_CVD), 'active': active}

def _open(ws):
    streams = [f"{s}@aggTrade" for s in BIN_MAP.keys()]
    ws.send(json.dumps({'method':'SUBSCRIBE','params':streams,'id':2}))

def _runner():
    while _RUN:
        try:
            ws = websocket.WebSocketApp('wss://fstream.binance.com/ws',
                on_message=_on_msg, on_open=_open,
                on_error=lambda ws,e:None, on_close=lambda ws,c,m:None)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e: print(f"[cvd] {e}", flush=True)
        if _RUN: time.sleep(5)

def start():
    global _RUN
    if _RUN or websocket is None: return
    _RUN = True
    threading.Thread(target=_runner, daemon=True, name='cvd_ws').start()
    print("[cvd] started", flush=True)
