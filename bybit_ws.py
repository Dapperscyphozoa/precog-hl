"""Bybit public WebSocket price feed for PreCog entry trigger.
Subscribes to publicTrade.* for all symbols. Maintains last price + timestamp in memory.
"""
import json, threading, time, traceback
try:
    import websocket  # websocket-client
except ImportError:
    websocket = None

# HL → Bybit symbol map (Bybit uses USDT perps)
HL_TO_BYBIT = {
    'BTC':'BTCUSDT','ETH':'ETHUSDT','SOL':'SOLUSDT','XRP':'XRPUSDT','ADA':'ADAUSDT',
    'AVAX':'AVAXUSDT','LINK':'LINKUSDT','BNB':'BNBUSDT','AAVE':'AAVEUSDT','INJ':'INJUSDT',
    'DOGE':'DOGEUSDT','ARB':'ARBUSDT','OP':'OPUSDT','HYPE':'HYPEUSDT','FARTCOIN':'FARTCOINUSDT',
    'kBONK':'1000BONKUSDT','kPEPE':'1000PEPEUSDT','kSHIB':'1000SHIBUSDT',
    'TRB':'TRBUSDT','POLYX':'POLYXUSDT','BLUR':'BLURUSDT','LIT':'LITUSDT','COMP':'COMPUSDT',
    'PENDLE':'PENDLEUSDT','AIXBT':'AIXBTUSDT','DOT':'DOTUSDT','WLD':'WLDUSDT','AR':'ARUSDT',
    'MORPHO':'MORPHOUSDT','APE':'APEUSDT','MOODENG':'MOODENGUSDT','LDO':'LDOUSDT',
    'TON':'TONUSDT','UMA':'UMAUSDT','ALGO':'ALGOUSDT','APT':'APTUSDT','TAO':'TAOUSDT',
    'JUP':'JUPUSDT','SAND':'SANDUSDT','SPX':'SPXUSDT','POL':'POLUSDT','ENS':'ENSUSDT',
    'SUSHI':'SUSHIUSDT','TIA':'TIAUSDT','ATOM':'ATOMUSDT','SUI':'SUIUSDT','LTC':'LTCUSDT',
    'UNI':'UNIUSDT','PUMP':'PUMPUSDT','PENGU':'PENGUUSDT','WIF':'WIFUSDT',
    'AERO':'AEROUSDT','MON':'MONUSDT','GALA':'GALAUSDT','VIRTUAL':'VIRTUALUSDT','VVV':'VVVUSDT',
}

_PRICES = {}  # HL_coin -> (price, ts_ms)
_LOCK = threading.Lock()
_WS = None
_RUN = False

def get(coin):
    """Latest Bybit price for an HL coin. Returns (price, age_ms) or (None, None)."""
    with _LOCK:
        v = _PRICES.get(coin)
    if not v: return None, None
    price, ts = v
    return price, int(time.time()*1000) - ts

def _on_message(ws, msg):
    try:
        m = json.loads(msg)
        if m.get('topic','').startswith('publicTrade.'):
            sym = m['topic'].split('.',1)[1]
            # Find HL coin
            hl_coin = None
            for hl,by in HL_TO_BYBIT.items():
                if by == sym: hl_coin = hl; break
            if not hl_coin: return
            data = m.get('data', [])
            if not data: return
            # Use last trade
            last = data[-1]
            price = float(last['p'])
            ts = int(last['T'])
            with _LOCK:
                _PRICES[hl_coin] = (price, ts)
    except Exception:
        pass

def _on_open(ws):
    # Subscribe to all publicTrade feeds. Bybit allows up to 10 args per sub msg.
    syms = list(HL_TO_BYBIT.values())
    topics = [f'publicTrade.{s}' for s in syms]
    # batch 10 per msg
    for i in range(0, len(topics), 10):
        ws.send(json.dumps({'op':'subscribe','args':topics[i:i+10]}))

def _on_error(ws, err):
    print(f"[bybit_ws] error: {err}", flush=True)

def _on_close(ws, code, msg):
    print(f"[bybit_ws] closed: {code} {msg}", flush=True)

def _runner():
    global _WS, _RUN
    if websocket is None:
        print("[bybit_ws] websocket-client not installed", flush=True); return
    while _RUN:
        try:
            _WS = websocket.WebSocketApp(
                'wss://stream.bybit.com/v5/public/linear',
                on_message=_on_message, on_open=_on_open,
                on_error=_on_error, on_close=_on_close)
            _WS.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[bybit_ws] runner exc: {e}", flush=True)
        if _RUN:
            time.sleep(5)  # reconnect

def start():
    global _RUN
    if _RUN: return
    _RUN = True
    t = threading.Thread(target=_runner, daemon=True, name='bybit_ws')
    t.start()
    print("[bybit_ws] started", flush=True)

def stop():
    global _RUN, _WS
    _RUN = False
    if _WS:
        try: _WS.close()
        except: pass

def status():
    with _LOCK:
        return {'symbols_live': len(_PRICES), 'latest': {k:v[0] for k,v in list(_PRICES.items())[:5]}}
