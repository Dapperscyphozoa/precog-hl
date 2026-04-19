"""Liquidation cascade detector. Binance !forceOrder@arr WS.
Tracks liquidations per coin over 60s windows. Cascade = >$2M in <60s one direction.
Signal: fade the cascade (opposite direction entry) — liquidations are exhaustion.
"""
import json, threading, time
from collections import defaultdict, deque
try:
    import websocket
except ImportError:
    websocket = None

CASCADE_USD_THRESHOLD = 2_000_000   # $2M in window = cascade
CASCADE_WINDOW_SEC = 60
WHALE_LIQ_USD = 100_000             # single liq >$100k = whale

BIN_TO_HL = {
    'BTCUSDT':'BTC','ETHUSDT':'ETH','SOLUSDT':'SOL','XRPUSDT':'XRP','ADAUSDT':'ADA',
    'AVAXUSDT':'AVAX','LINKUSDT':'LINK','BNBUSDT':'BNB','AAVEUSDT':'AAVE','INJUSDT':'INJ',
    'DOGEUSDT':'DOGE','ARBUSDT':'ARB','OPUSDT':'OP','DOTUSDT':'DOT','ATOMUSDT':'ATOM',
    'SUIUSDT':'SUI','LTCUSDT':'LTC','TIAUSDT':'TIA','APTUSDT':'APT','FILUSDT':'FIL',
    'NEARUSDT':'NEAR','ENAUSDT':'ENA','WIFUSDT':'WIF','JUPUSDT':'JUP','PYTHUSDT':'PYTH',
    'POPCATUSDT':'POPCAT','BRETTUSDT':'BRETT','BOMEUSDT':'BOME','MANTAUSDT':'MANTA',
    'ORDIUSDT':'ORDI','TONUSDT':'TON','SANDUSDT':'SAND','PENDLEUSDT':'PENDLE',
    '1000PEPEUSDT':'kPEPE','1000BONKUSDT':'kBONK','1000SHIBUSDT':'kSHIB',
}

_LIQS = defaultdict(lambda: deque(maxlen=500))   # coin -> deque[(ts, side, usd)]
_LAST_CASCADE = {}                               # coin -> {ts, side, total_usd}
_LOCK = threading.Lock()
_RUN = False

def _on_msg(ws, msg):
    try:
        m = json.loads(msg)
        data = m.get('data', m)
        o = data.get('o', {})
        sym = o.get('s','')
        hl = BIN_TO_HL.get(sym)
        if not hl: return
        side = o.get('S')  # 'BUY' or 'SELL' — side of the liquidation order placed
        # NOTE: a 'BUY' liquidation = someone's SHORT got liquidated (forced buyback)
        liq_side = 'short_liq' if side == 'BUY' else 'long_liq'
        qty = float(o.get('q', 0))
        px = float(o.get('p', 0))
        usd = qty * px
        if usd < 1000: return  # dust
        ts = time.time()
        with _LOCK:
            _LIQS[hl].append((ts, liq_side, usd))
            _detect_cascade(hl)
    except Exception as e:
        pass

def _detect_cascade(coin):
    """Evaluate if recent liqs form a cascade. Called inside _LOCK."""
    now = time.time()
    cutoff = now - CASCADE_WINDOW_SEC
    recent = [x for x in _LIQS[coin] if x[0] > cutoff]
    long_liq_usd = sum(u for _, s, u in recent if s == 'long_liq')
    short_liq_usd = sum(u for _, s, u in recent if s == 'short_liq')
    if long_liq_usd > CASCADE_USD_THRESHOLD:
        _LAST_CASCADE[coin] = {'ts': now, 'side': 'long_liq', 'total_usd': long_liq_usd,
                               'fade_direction': 'BUY'}
    elif short_liq_usd > CASCADE_USD_THRESHOLD:
        _LAST_CASCADE[coin] = {'ts': now, 'side': 'short_liq', 'total_usd': short_liq_usd,
                               'fade_direction': 'SELL'}

def get_cascade(coin, max_age_sec=300):
    """Returns cascade event if recent (<5min), else None."""
    with _LOCK:
        c = _LAST_CASCADE.get(coin)
    if not c: return None
    if time.time() - c['ts'] > max_age_sec: return None
    return c

def get_recent_liq_pressure(coin, window_sec=60):
    """Returns (long_liq_usd, short_liq_usd) within window."""
    now = time.time()
    cutoff = now - window_sec
    with _LOCK:
        recent = [x for x in _LIQS.get(coin, []) if x[0] > cutoff]
    long_liq = sum(u for _, s, u in recent if s == 'long_liq')
    short_liq = sum(u for _, s, u in recent if s == 'short_liq')
    return long_liq, short_liq

def status():
    with _LOCK:
        active = len([c for c, d in _LAST_CASCADE.items() if time.time() - d['ts'] < 300])
        total_liqs = sum(len(v) for v in _LIQS.values())
    return {'tracked_coins': len(_LIQS), 'recent_cascades': active, 'total_liqs_cached': total_liqs}

def _open(ws):
    ws.send(json.dumps({'method':'SUBSCRIBE','params':['!forceOrder@arr'],'id':1}))

def _runner():
    while _RUN:
        try:
            ws = websocket.WebSocketApp('wss://fstream.binance.com/ws',
                on_message=_on_msg, on_open=_open,
                on_error=lambda ws,e: None, on_close=lambda ws,c,m: None)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[liq_ws] {e}", flush=True)
        if _RUN: time.sleep(5)

def start():
    global _RUN
    if _RUN: return
    _RUN = True
    if websocket is None:
        print("[liq_ws] websocket-client missing", flush=True); return
    threading.Thread(target=_runner, daemon=True, name='liq_ws').start()
    print("[liq_ws] started Binance !forceOrder@arr", flush=True)
