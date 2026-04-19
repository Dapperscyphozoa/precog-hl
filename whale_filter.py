"""Whale trade filter. Binance @aggTrade stream, flags prints >$100k.
Net imbalance over 60s = directional confirmation. Used as confluence boost, not standalone signal.
"""
import json, threading, time
from collections import defaultdict, deque
try:
    import websocket
except ImportError:
    websocket = None

WHALE_USD = 100_000
WINDOW_SEC = 60

BIN_TO_HL = {
    'btcusdt':'BTC','ethusdt':'ETH','solusdt':'SOL','xrpusdt':'XRP','adausdt':'ADA',
    'avaxusdt':'AVAX','linkusdt':'LINK','bnbusdt':'BNB','dotusdt':'DOT','atomusdt':'ATOM',
    'suiusdt':'SUI','dogeusdt':'DOGE','arbusdt':'ARB','opusdt':'OP','ltcusdt':'LTC',
    'tiausdt':'TIA','aptusdt':'APT','filusdt':'FIL','nearusdt':'NEAR','enausdt':'ENA',
    'wifusdt':'WIF','jupusdt':'JUP','ordiusdt':'ORDI','tonusdt':'TON','aaveusdt':'AAVE',
    'injusdt':'INJ','ldousdt':'LDO',
}
_WHALES = defaultdict(lambda: deque(maxlen=300))  # coin -> deque[(ts, side, usd)]
_LOCK = threading.Lock()
_RUN = False

def _on_msg(ws, msg):
    try:
        m = json.loads(msg)
        data = m.get('data', m)
        sym = data.get('s','').lower()
        hl = BIN_TO_HL.get(sym)
        if not hl: return
        qty = float(data.get('q', 0))
        px = float(data.get('p', 0))
        usd = qty * px
        if usd < WHALE_USD: return
        # Binance: m=True means buyer is maker → aggressor was SELLER
        # m=False → buyer was aggressor (taker buy)
        aggressor = 'SELL' if data.get('m') else 'BUY'
        ts = time.time()
        with _LOCK:
            _WHALES[hl].append((ts, aggressor, usd))
    except Exception:
        pass

def get_imbalance(coin, window_sec=WINDOW_SEC):
    """Returns (buy_usd, sell_usd, net_bias). net_bias: +1 strong buy, -1 strong sell, 0 balanced."""
    now = time.time(); cutoff = now - window_sec
    with _LOCK:
        recent = [x for x in _WHALES.get(coin, []) if x[0] > cutoff]
    buy = sum(u for _, s, u in recent if s == 'BUY')
    sell = sum(u for _, s, u in recent if s == 'SELL')
    total = buy + sell
    if total < WHALE_USD: return (buy, sell, 0)
    bias = (buy - sell) / total  # -1 to +1
    return (buy, sell, bias)

def confluence_boost(coin, side, window_sec=WINDOW_SEC):
    """Returns multiplier 0.5-1.5 based on whale alignment with trade side."""
    _, _, bias = get_imbalance(coin, window_sec)
    side_view = 1 if side == 'BUY' else -1
    alignment = side_view * bias  # +1 fully aligned, -1 fully opposed
    if alignment > 0.5: return 1.5
    if alignment > 0.2: return 1.2
    if alignment > -0.2: return 1.0
    if alignment > -0.5: return 0.8
    return 0.5

def status():
    with _LOCK:
        active = sum(1 for c,d in _WHALES.items() if d and time.time()-d[-1][0] < 300)
        total = sum(len(v) for v in _WHALES.values())
    return {'tracked_coins': len(_WHALES), 'active_coins': active, 'total_whales': total}

def _open(ws):
    streams = [f"{s}@aggTrade" for s in BIN_TO_HL.keys()]
    ws.send(json.dumps({'method':'SUBSCRIBE','params':streams,'id':1}))

def _runner():
    while _RUN:
        try:
            ws = websocket.WebSocketApp('wss://fstream.binance.com/ws',
                on_message=_on_msg, on_open=_open,
                on_error=lambda ws,e:None, on_close=lambda ws,c,m:None)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[whale] {e}", flush=True)
        if _RUN: time.sleep(5)

def start():
    global _RUN
    if _RUN or websocket is None: return
    _RUN = True
    threading.Thread(target=_runner, daemon=True, name='whale_ws').start()
    print("[whale] started Binance aggTrade", flush=True)
