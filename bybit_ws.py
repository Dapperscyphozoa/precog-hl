"""Bybit public WebSocket — live price + 5m candles for PreCog signal generation.
Subscribes to publicTrade.* (entry trigger) AND kline.5.* (signal candles).
"""
import json, threading, time, traceback
from collections import deque
try:
    import websocket
except ImportError:
    websocket = None

HL_TO_BYBIT = {
    'BTC':'BTCUSDT','ETH':'ETHUSDT','SOL':'SOLUSDT','XRP':'XRPUSDT','ADA':'ADAUSDT',
    'AVAX':'AVAXUSDT','LINK':'LINKUSDT','BNB':'BNBUSDT','AAVE':'AAVEUSDT','INJ':'INJUSDT',
    'DOGE':'DOGEUSDT','ARB':'ARBUSDT','OP':'OPUSDT','HYPE':'HYPEUSDT',
    'kBONK':'1000BONKUSDT','kPEPE':'1000PEPEUSDT','kSHIB':'1000SHIBUSDT',
    'TRB':'TRBUSDT','POLYX':'POLYXUSDT','BLUR':'BLURUSDT','LIT':'LITUSDT','COMP':'COMPUSDT',
    'PENDLE':'PENDLEUSDT','AIXBT':'AIXBTUSDT','DOT':'DOTUSDT','WLD':'WLDUSDT','AR':'ARUSDT',
    'MORPHO':'MORPHOUSDT','APE':'APEUSDT','MOODENG':'MOODENGUSDT','LDO':'LDOUSDT',
    'TON':'TONUSDT','UMA':'UMAUSDT','ALGO':'ALGOUSDT','APT':'APTUSDT','TAO':'TAOUSDT',
    'JUP':'JUPUSDT','SAND':'SANDUSDT','SPX':'SPXUSDT','POL':'POLUSDT','ENS':'ENSUSDT',
    'SUSHI':'SUSHIUSDT','TIA':'TIAUSDT','ATOM':'ATOMUSDT','SUI':'SUIUSDT','LTC':'LTCUSDT',
    'UNI':'UNIUSDT','PUMP':'PUMPUSDT','PENGU':'PENGUUSDT','WIF':'WIFUSDT',
    'AERO':'AEROUSDT','GALA':'GALAUSDT','VIRTUAL':'VIRTUALUSDT','VVV':'VVVUSDT',
    'FARTCOIN':'FARTCOINUSDT',
    'MAVIA':'MAVIAUSDT',
    'HMSTR':'HMSTRUSDT',
    'ZEREBRO':'ZEREBROUSDT',
    'BLAST':'BLASTUSDT',
    'BOME':'BOMEUSDT',
    'MANTA':'MANTAUSDT',
    'CHILLGUY':'CHILLGUYUSDT',
    'RSR':'RSRUSDT',
    'MELANIA':'MELANIAUSDT',
    'SCR':'SCRUSDT',
    'BIO':'BIOUSDT',
    'TNSR':'TNSRUSDT',
    'MINA':'MINAUSDT',
    'NOT':'NOTUSDT',
    'MEW':'MEWUSDT',
    'BRETT':'BRETTUSDT',
    'ME':'MEUSDT',
    'IOTA':'IOTAUSDT',
    'DYM':'DYMUSDT',
    'ORDI':'ORDIUSDT',
    'POPCAT':'POPCATUSDT',
    'SAGA':'SAGAUSDT',
    'FIL':'FILUSDT',
    'REZ':'REZUSDT',
    'BANANA':'BANANAUSDT',
    'kNEIRO':'1000NEIROUSDT',
    'GMT':'GMTUSDT',
    'XAI':'XAIUSDT',
    'NEO':'NEOUSDT',
    'MAV':'MAVUSDT',
    'RESOLV':'RESOLVUSDT',
    'HEMI':'HEMIUSDT',
    'STABLE':'STABLEUSDT',
    'BABY':'BABYUSDT',
    'TST':'TSTUSDT',
    'YZY':'YZYUSDT',
    'PROMPT':'PROMPTUSDT',
    'DOOD':'DOODUSDT',
    'FOGO':'FOGOUSDT',
    'NXPC':'NXPCUSDT',
    'INIT':'INITUSDT',
    'APEX':'APEXUSDT',
    'WLFI':'WLFIUSDT',
    'VINE':'VINEUSDT',
    'XAI':'XAIUSDT',
    'SUPER':'SUPERUSDT',
    'YGG':'YGGUSDT',
    'MERL':'MERLUSDT',
    'SKR':'SKRUSDT',
    'KAITO':'KAITOUSDT',
    'BSV':'BSVUSDT',
    'LAYER':'LAYERUSDT',
    'USUAL':'USUALUSDT',
    'TURBO':'TURBOUSDT',
    'GMX':'GMXUSDT',
    'ACE':'ACEUSDT',
    'AVNT':'AVNTUSDT',
    'W':'WUSDT',
    'ARK':'ARKUSDT',
    'HYPER':'HYPERUSDT',
    'IO':'IOUSDT',
    'NIL':'NILUSDT',
    'kFLOKI':'1000FLOKIUSDT',
    'ETC':'ETCUSDT',
    'SOPH':'SOPHUSDT',
    'PNUT':'PNUTUSDT',
    'RUNE':'RUNEUSDT',
}
# Inverse map
BYBIT_TO_HL = {v:k for k,v in HL_TO_BYBIT.items()}
# HL-only coins (no Bybit listing) — fall back to HL REST
HL_ONLY = set()

_PRICES = {}          # HL_coin -> (price, ts_ms)
_CANDLES = {}         # HL_coin -> deque of (ts, o, h, l, c, v), most recent last
CANDLE_LIMIT = 500    # keep last 500 5m bars (~42h)
_LOCK = threading.Lock()
_WS_TRADE = None
_WS_KLINE = None
_RUN = False

def get_price(coin):
    with _LOCK:
        v = _PRICES.get(coin)
    if not v: return None, None
    return v[0], int(time.time()*1000) - v[1]

def get_candles(coin, limit=500):
    with _LOCK:
        dq = _CANDLES.get(coin)
        if not dq: return []
        # Return as list of (ts, o, h, l, c, v) tuples matching HL format
        return list(dq)[-limit:]

def has_coin(coin):
    return coin in HL_TO_BYBIT and coin not in HL_ONLY

def _on_msg_trade(ws, msg):
    try:
        m = json.loads(msg)
        topic = m.get('topic','')
        if topic.startswith('publicTrade.'):
            sym = topic.split('.',1)[1]
            hl = BYBIT_TO_HL.get(sym)
            if not hl: return
            data = m.get('data', [])
            if data:
                last = data[-1]
                with _LOCK:
                    _PRICES[hl] = (float(last['p']), int(last['T']))
    except Exception: pass

def _on_msg_kline(ws, msg):
    try:
        m = json.loads(msg)
        topic = m.get('topic','')
        if topic.startswith('kline.'):
            parts = topic.split('.')
            sym = parts[2] if len(parts)>=3 else None
            hl = BYBIT_TO_HL.get(sym)
            if not hl: return
            data = m.get('data', [])
            for k in data:
                # Bybit kline fields: start, end, open, close, high, low, volume, confirm
                ts = int(k['start'])
                o = float(k['open']); c = float(k['close'])
                h = float(k['high']); l = float(k['low']); v = float(k.get('volume', 0))
                confirmed = bool(k.get('confirm', False))
                with _LOCK:
                    dq = _CANDLES.setdefault(hl, deque(maxlen=CANDLE_LIMIT))
                    # Replace last if same timestamp, else append
                    if dq and dq[-1][0] == ts:
                        dq[-1] = (ts, o, h, l, c, v)
                    else:
                        dq.append((ts, o, h, l, c, v))
    except Exception: pass

def _open_trade(ws):
    syms = list(HL_TO_BYBIT.values())
    topics = [f'publicTrade.{s}' for s in syms]
    for i in range(0, len(topics), 10):
        ws.send(json.dumps({'op':'subscribe','args':topics[i:i+10]}))

def _open_kline(ws):
    syms = list(HL_TO_BYBIT.values())
    topics = [f'kline.5.{s}' for s in syms]  # 5m candles
    for i in range(0, len(topics), 10):
        ws.send(json.dumps({'op':'subscribe','args':topics[i:i+10]}))

def _runner_trade():
    global _WS_TRADE
    while _RUN:
        try:
            _WS_TRADE = websocket.WebSocketApp(
                'wss://stream.bybit.com/v5/public/linear',
                on_message=_on_msg_trade, on_open=_open_trade,
                on_error=lambda ws,e: print(f"[bybit_ws trade] {e}", flush=True),
                on_close=lambda ws,c,m: print(f"[bybit_ws trade] closed {c}", flush=True))
            _WS_TRADE.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[bybit_ws trade] runner: {e}", flush=True)
        if _RUN: time.sleep(5)

def _runner_kline():
    global _WS_KLINE
    while _RUN:
        try:
            _WS_KLINE = websocket.WebSocketApp(
                'wss://stream.bybit.com/v5/public/linear',
                on_message=_on_msg_kline, on_open=_open_kline,
                on_error=lambda ws,e: print(f"[bybit_ws kline] {e}", flush=True),
                on_close=lambda ws,c,m: print(f"[bybit_ws kline] closed {c}", flush=True))
            _WS_KLINE.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[bybit_ws kline] runner: {e}", flush=True)
        if _RUN: time.sleep(5)

def _prefetch_history():
    """Seed candle buffers with REST history on boot (Bybit REST, not HL)."""
    import urllib.request
    seeded = 0
    for hl, sym in HL_TO_BYBIT.items():
        try:
            url = f'https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval=5&limit=500'
            r = json.loads(urllib.request.urlopen(url, timeout=10).read())
            if r.get('retCode') != 0: continue
            bars = r.get('result',{}).get('list',[])
            # Bybit returns newest first
            bars = list(reversed(bars))
            dq = deque(maxlen=CANDLE_LIMIT)
            for b in bars:
                # [start, open, high, low, close, volume, turnover]
                dq.append((int(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])))
            with _LOCK:
                _CANDLES[hl] = dq
            seeded += 1
            time.sleep(0.1)  # Bybit REST rate limit
        except Exception as e:
            pass
    print(f"[bybit_ws] prefetched {seeded}/{len(HL_TO_BYBIT)} coin histories", flush=True)

def start():
    global _RUN
    if _RUN: return
    _RUN = True
    if websocket is None:
        print("[bybit_ws] websocket-client not installed", flush=True); return
    # Seed history first (blocking, ~30s for 50 coins at 0.1s each)
    threading.Thread(target=_prefetch_history, daemon=True, name='bybit_prefetch').start()
    threading.Thread(target=_runner_trade, daemon=True, name='bybit_trade').start()
    threading.Thread(target=_runner_kline, daemon=True, name='bybit_kline').start()
    print("[bybit_ws] started trade+kline+prefetch threads", flush=True)

def status():
    with _LOCK:
        return {
            'prices_live': len(_PRICES),
            'candles_live': len(_CANDLES),
            'candle_counts': {k:len(v) for k,v in list(_CANDLES.items())[:10]},
        }
