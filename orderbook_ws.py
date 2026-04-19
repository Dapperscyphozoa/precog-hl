"""Aggregated orderbook heatmap: Bybit + Binance WS depth.
Buckets depth into 0.1% price bins. Detects verified walls (>$500k aggregate, >5min persistence).
"""
import json, threading, time
from collections import defaultdict, deque
try:
    import websocket
except ImportError:
    websocket = None

# HL coin -> (bybit_sym, binance_sym)
HL_VENUES = {
    'BTC':('BTCUSDT','BTCUSDT'),'ETH':('ETHUSDT','ETHUSDT'),'SOL':('SOLUSDT','SOLUSDT'),
    'XRP':('XRPUSDT','XRPUSDT'),'ADA':('ADAUSDT','ADAUSDT'),'AVAX':('AVAXUSDT','AVAXUSDT'),
    'LINK':('LINKUSDT','LINKUSDT'),'BNB':('BNBUSDT','BNBUSDT'),'AAVE':('AAVEUSDT','AAVEUSDT'),
    'INJ':('INJUSDT','INJUSDT'),'DOGE':('DOGEUSDT','DOGEUSDT'),'ARB':('ARBUSDT','ARBUSDT'),
    'OP':('OPUSDT','OPUSDT'),'HYPE':('HYPEUSDT',None),
    'kBONK':('1000BONKUSDT','1000BONKUSDT'),'kPEPE':('1000PEPEUSDT','1000PEPEUSDT'),
    'kSHIB':('1000SHIBUSDT','1000SHIBUSDT'),'TRB':('TRBUSDT','TRBUSDT'),
    'DOT':('DOTUSDT','DOTUSDT'),'ATOM':('ATOMUSDT','ATOMUSDT'),'SUI':('SUIUSDT','SUIUSDT'),
    'LDO':('LDOUSDT','LDOUSDT'),'UMA':('UMAUSDT','UMAUSDT'),'ALGO':('ALGOUSDT','ALGOUSDT'),
    'APE':('APEUSDT','APEUSDT'),'LTC':('LTCUSDT','LTCUSDT'),'TIA':('TIAUSDT','TIAUSDT'),
    'ORDI':('ORDIUSDT','ORDIUSDT'),'TON':('TONUSDT','TONUSDT'),'PENDLE':('PENDLEUSDT','PENDLEUSDT'),
    'JUP':('JUPUSDT','JUPUSDT'),'WIF':('WIFUSDT','WIFUSDT'),'APT':('APTUSDT','APTUSDT'),
    'FIL':('FILUSDT','FILUSDT'),'BOME':('BOMEUSDT','BOMEUSDT'),'MANTA':('MANTAUSDT','MANTAUSDT'),
    'POPCAT':('POPCATUSDT','POPCATUSDT'),'BRETT':('BRETTUSDT','BRETTUSDT'),
    'SAND':('SANDUSDT','SANDUSDT'),'AVAX':('AVAXUSDT','AVAXUSDT'),
    'NOT':('NOTUSDT','NOTUSDT'),'MEW':('MEWUSDT','MEWUSDT'),'ME':('MEUSDT','MEUSDT'),
    'PYTH':('PYTHUSDT','PYTHUSDT'),'ENA':('ENAUSDT','ENAUSDT'),'NEAR':('NEARUSDT','NEARUSDT'),
}

_DEPTH = defaultdict(lambda: {'bids':{}, 'asks':{}, 'ts':0, 'mid':0})  # HL coin -> levels
_WALLS_HISTORY = defaultdict(lambda: deque(maxlen=60))  # (coin, side, price_bucket) -> deque of (ts, usd)
_VERIFIED_WALLS = {}  # HL coin -> [{'side','price','usd','detected_at'}]
_LOCK = threading.Lock()
_RUN = False

BIN_TO_HL = {v[1]:k for k,v in HL_VENUES.items() if v[1]}
BY_TO_HL  = {v[0]:k for k,v in HL_VENUES.items() if v[0]}

def _update_levels(coin, bids, asks, venue):
    with _LOCK:
        d = _DEPTH[coin]
        d['ts'] = time.time()
        # Aggregate (bybit + binance additive)
        for px_s, sz_s in bids:
            px=float(px_s); sz=float(sz_s)
            if sz==0: d['bids'].pop(f"{venue}_{px}", None)
            else: d['bids'][f"{venue}_{px}"] = (px, sz)
        for px_s, sz_s in asks:
            px=float(px_s); sz=float(sz_s)
            if sz==0: d['asks'].pop(f"{venue}_{px}", None)
            else: d['asks'][f"{venue}_{px}"] = (px, sz)
        # Compute mid from best aggregated bid/ask
        if d['bids'] and d['asks']:
            best_bid = max(v[0] for v in d['bids'].values())
            best_ask = min(v[0] for v in d['asks'].values())
            d['mid'] = (best_bid + best_ask) / 2

def _bybit_msg(ws, msg):
    try:
        m = json.loads(msg)
        topic = m.get('topic','')
        if not topic.startswith('orderbook.'): return
        parts = topic.split('.')
        sym = parts[2] if len(parts) >= 3 else None
        coin = BY_TO_HL.get(sym)
        if not coin: return
        data = m.get('data', {})
        _update_levels(coin, data.get('b', []), data.get('a', []), 'by')
    except Exception: pass

def _binance_msg(ws, msg):
    try:
        m = json.loads(msg)
        # Binance combined stream format: {"stream":"btcusdt@depth20","data":{...}}
        stream = m.get('stream','')
        if '@depth' not in stream: return
        sym = stream.split('@')[0].upper()
        coin = BIN_TO_HL.get(sym)
        if not coin: return
        data = m.get('data', {})
        _update_levels(coin, data.get('bids', []), data.get('asks', []), 'bn')
    except Exception: pass

def _bybit_open(ws):
    topics = [f'orderbook.50.{v[0]}' for v in HL_VENUES.values()]
    for i in range(0, len(topics), 10):
        ws.send(json.dumps({'op':'subscribe','args':topics[i:i+10]}))

def _runner_bybit():
    while _RUN:
        try:
            ws = websocket.WebSocketApp('wss://stream.bybit.com/v5/public/linear',
                on_message=_bybit_msg, on_open=_bybit_open,
                on_error=lambda ws,e: None, on_close=lambda ws,c,m: None)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[ob_ws bybit] {e}", flush=True)
        if _RUN: time.sleep(5)

def _runner_binance():
    # Combined stream URL
    syms = [v[1].lower() for v in HL_VENUES.values() if v[1]]
    streams = "/".join(f"{s}@depth20@100ms" for s in syms)
    url = f"wss://fstream.binance.com/stream?streams={streams}"
    while _RUN:
        try:
            ws = websocket.WebSocketApp(url, on_message=_binance_msg,
                on_error=lambda ws,e: None, on_close=lambda ws,c,m: None)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[ob_ws binance] {e}", flush=True)
        if _RUN: time.sleep(5)

# Verified wall detector — runs every 30s
def _wall_scanner():
    while _RUN:
        time.sleep(30)
        try:
            with _LOCK:
                coins = list(_DEPTH.keys())
            for coin in coins:
                _detect_walls(coin)
        except Exception as e:
            print(f"[ob_ws scanner] {e}", flush=True)

def _detect_walls(coin):
    with _LOCK:
        d = _DEPTH.get(coin)
        if not d or not d.get('mid'): return
        mid = d['mid']
        # Aggregate depth into 0.1% buckets
        buckets_b = defaultdict(float)  # bid side: bucket_pct -> total USD
        buckets_a = defaultdict(float)
        for px, sz in d['bids'].values():
            pct = round((mid - px) / mid * 1000) / 10  # 0.1% bucket
            if 0 <= pct <= 5: buckets_b[pct] += px * sz
        for px, sz in d['asks'].values():
            pct = round((px - mid) / mid * 1000) / 10
            if 0 <= pct <= 5: buckets_a[pct] += px * sz
        ts = time.time()
        # Track history for persistence check
        for pct, usd in buckets_b.items():
            if usd >= 500000:
                _WALLS_HISTORY[(coin,'bid',pct)].append((ts, usd))
        for pct, usd in buckets_a.items():
            if usd >= 500000:
                _WALLS_HISTORY[(coin,'ask',pct)].append((ts, usd))
        # Verify: wall seen in ≥5 of last ~10 windows (5min at 30s cadence)
        verified = []
        for key, hist in list(_WALLS_HISTORY.items()):
            c, side, pct = key
            if c != coin: continue
            # Prune old
            while hist and ts - hist[0][0] > 600: hist.popleft()
            if len(hist) >= 5:
                avg_usd = sum(x[1] for x in hist) / len(hist)
                price = mid * (1 - pct/100) if side == 'bid' else mid * (1 + pct/100)
                verified.append({'side':side,'price':price,'usd':avg_usd,'distance_pct':pct,
                                 'persistence_windows':len(hist),'detected_at':ts})
        _VERIFIED_WALLS[coin] = verified

def get_nearest_wall(coin, side):
    """side: 'bid' (support) or 'ask' (resistance). Returns nearest verified wall or None."""
    with _LOCK:
        walls = _VERIFIED_WALLS.get(coin, [])
    relevant = [w for w in walls if w['side'] == side]
    if not relevant: return None
    return min(relevant, key=lambda w: w['distance_pct'])

def get_walls(coin):
    with _LOCK:
        return list(_VERIFIED_WALLS.get(coin, []))

def status():
    with _LOCK:
        return {
            'depth_feeds': len(_DEPTH),
            'tracked_walls': sum(len(v) for v in _VERIFIED_WALLS.values()),
            'verified_coins': len([c for c, w in _VERIFIED_WALLS.items() if w]),
        }


# OKX symbol map (HL coin -> OKX instId)
HL_TO_OKX = {
    'BTC':'BTC-USDT-SWAP','ETH':'ETH-USDT-SWAP','SOL':'SOL-USDT-SWAP','XRP':'XRP-USDT-SWAP',
    'ADA':'ADA-USDT-SWAP','AVAX':'AVAX-USDT-SWAP','LINK':'LINK-USDT-SWAP','DOT':'DOT-USDT-SWAP',
    'ATOM':'ATOM-USDT-SWAP','SUI':'SUI-USDT-SWAP','DOGE':'DOGE-USDT-SWAP','LTC':'LTC-USDT-SWAP',
    'BNB':'BNB-USDT-SWAP','APT':'APT-USDT-SWAP','NEAR':'NEAR-USDT-SWAP','TIA':'TIA-USDT-SWAP',
    'INJ':'INJ-USDT-SWAP','FIL':'FIL-USDT-SWAP','ARB':'ARB-USDT-SWAP','OP':'OP-USDT-SWAP',
    'AAVE':'AAVE-USDT-SWAP','LDO':'LDO-USDT-SWAP','WIF':'WIF-USDT-SWAP','ORDI':'ORDI-USDT-SWAP',
    'TON':'TON-USDT-SWAP','JUP':'JUP-USDT-SWAP','PYTH':'PYTH-USDT-SWAP',
}

HL_TO_COINBASE = {
    'BTC':'BTC-USD','ETH':'ETH-USD','SOL':'SOL-USD','XRP':'XRP-USD','ADA':'ADA-USD',
    'AVAX':'AVAX-USD','LINK':'LINK-USD','DOT':'DOT-USD','ATOM':'ATOM-USD','SUI':'SUI-USD',
    'DOGE':'DOGE-USD','LTC':'LTC-USD','APT':'APT-USD','NEAR':'NEAR-USD','AAVE':'AAVE-USD',
    'FIL':'FIL-USD','ARB':'ARB-USD','OP':'OP-USD','INJ':'INJ-USD','LDO':'LDO-USD',
}

def _okx_msg(ws, msg):
    try:
        m = json.loads(msg)
        if 'data' not in m: return
        arg = m.get('arg', {}); inst = arg.get('instId','')
        hl = None
        for h,o in HL_TO_OKX.items():
            if o == inst: hl = h; break
        if not hl: return
        for snap in m['data']:
            bids = snap.get('bids',[])[:50]
            asks = snap.get('asks',[])[:50]
            _update_levels(hl, bids, asks, 'okx')
    except Exception: pass

def _okx_open(ws):
    args = [{'channel':'books','instId':v} for v in HL_TO_OKX.values()]
    ws.send(json.dumps({'op':'subscribe','args':args}))

def _runner_okx():
    url = 'wss://ws.okx.com:8443/ws/v5/public'
    while _RUN:
        try:
            ws = websocket.WebSocketApp(url, on_message=_okx_msg, on_open=_okx_open,
                on_error=lambda ws,e:None, on_close=lambda ws,c,m:None)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e: print(f"[ob okx] {e}", flush=True)
        if _RUN: time.sleep(5)

def _coinbase_msg(ws, msg):
    try:
        m = json.loads(msg)
        t = m.get('type')
        if t not in ('snapshot', 'l2update'): return
        prod = m.get('product_id','')
        hl = None
        for h,v in HL_TO_COINBASE.items():
            if v == prod: hl = h; break
        if not hl: return
        if t == 'snapshot':
            bids = m.get('bids',[])[:50]
            asks = m.get('asks',[])[:50]
            _update_levels(hl, bids, asks, 'cb')
        else:
            # Convert delta to bid/ask lists
            bids = []; asks = []
            for side, p, s in m.get('changes', []):
                (bids if side == 'buy' else asks).append([p, s])
            _update_levels(hl, bids, asks, 'cb')
    except Exception: pass

def _coinbase_open(ws):
    ws.send(json.dumps({'type':'subscribe','product_ids':list(HL_TO_COINBASE.values()),'channels':['level2']}))

def _runner_coinbase():
    url = 'wss://ws-feed.exchange.coinbase.com'
    while _RUN:
        try:
            ws = websocket.WebSocketApp(url, on_message=_coinbase_msg, on_open=_coinbase_open,
                on_error=lambda ws,e:None, on_close=lambda ws,c,m:None)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e: print(f"[ob coinbase] {e}", flush=True)
        if _RUN: time.sleep(5)



HL_TO_BITGET = {
    'BTC':'BTCUSDT','ETH':'ETHUSDT','SOL':'SOLUSDT','XRP':'XRPUSDT','ADA':'ADAUSDT',
    'AVAX':'AVAXUSDT','LINK':'LINKUSDT','DOT':'DOTUSDT','ATOM':'ATOMUSDT','SUI':'SUIUSDT',
    'DOGE':'DOGEUSDT','LTC':'LTCUSDT','BNB':'BNBUSDT','APT':'APTUSDT','NEAR':'NEARUSDT',
    'TIA':'TIAUSDT','INJ':'INJUSDT','FIL':'FILUSDT','ARB':'ARBUSDT','OP':'OPUSDT',
    'AAVE':'AAVEUSDT','LDO':'LDOUSDT','WIF':'WIFUSDT','ORDI':'ORDIUSDT','TON':'TONUSDT',
}

HL_TO_KRAKEN = {
    'BTC':'PF_XBTUSD','ETH':'PF_ETHUSD','SOL':'PF_SOLUSD','XRP':'PF_XRPUSD','ADA':'PF_ADAUSD',
    'AVAX':'PF_AVAXUSD','LINK':'PF_LINKUSD','DOT':'PF_DOTUSD','ATOM':'PF_ATOMUSD','SUI':'PF_SUIUSD',
    'DOGE':'PF_DOGEUSD','LTC':'PF_LTCUSD','AAVE':'PF_AAVEUSD','FIL':'PF_FILUSD','ARB':'PF_ARBUSD',
    'OP':'PF_OPUSD','INJ':'PF_INJUSD','LDO':'PF_LDOUSD','APT':'PF_APTUSD','NEAR':'PF_NEARUSD',
}

def _bitget_msg(ws, msg):
    try:
        m = json.loads(msg)
        if m.get('action') not in ('snapshot','update'): return
        arg = m.get('arg', {}); inst = arg.get('instId','')
        hl = None
        for h,b in HL_TO_BITGET.items():
            if b == inst: hl = h; break
        if not hl: return
        for d in m.get('data', []):
            _update_levels(hl, d.get('bids', [])[:50], d.get('asks', [])[:50], 'bg')
    except Exception: pass

def _bitget_open(ws):
    args = [{'instType':'USDT-FUTURES','channel':'books','instId':v} for v in HL_TO_BITGET.values()]
    ws.send(json.dumps({'op':'subscribe','args':args}))

def _runner_bitget():
    while _RUN:
        try:
            ws = websocket.WebSocketApp('wss://ws.bitget.com/v2/ws/public',
                on_message=_bitget_msg, on_open=_bitget_open,
                on_error=lambda ws,e:None, on_close=lambda ws,c,m:None)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e: print(f"[ob bitget] {e}", flush=True)
        if _RUN: time.sleep(5)

def _kraken_msg(ws, msg):
    try:
        m = json.loads(msg)
        if m.get('feed') not in ('book_snapshot','book'): return
        prod = m.get('product_id','')
        hl = None
        for h,k in HL_TO_KRAKEN.items():
            if k == prod: hl = h; break
        if not hl: return
        # Kraken provides bids/asks as list of {price, qty}
        bids = [[b['price'], b['qty']] for b in m.get('bids', [])[:50]] if m.get('bids') else []
        asks = [[a['price'], a['qty']] for a in m.get('asks', [])[:50]] if m.get('asks') else []
        if bids or asks:
            _update_levels(hl, bids, asks, 'kr')
    except Exception: pass

def _kraken_open(ws):
    ws.send(json.dumps({'event':'subscribe','feed':'book','product_ids':list(HL_TO_KRAKEN.values())}))

def _runner_kraken():
    while _RUN:
        try:
            ws = websocket.WebSocketApp('wss://futures.kraken.com/ws/v1',
                on_message=_kraken_msg, on_open=_kraken_open,
                on_error=lambda ws,e:None, on_close=lambda ws,c,m:None)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e: print(f"[ob kraken] {e}", flush=True)
        if _RUN: time.sleep(5)

def start():
    global _RUN
    if _RUN: return
    _RUN = True
    if websocket is None:
        print("[ob_ws] websocket-client missing", flush=True); return
    threading.Thread(target=_runner_bybit, daemon=True, name='ob_bybit').start()
    threading.Thread(target=_runner_binance, daemon=True, name='ob_binance').start()
    threading.Thread(target=_runner_okx, daemon=True, name='ob_okx').start()
    threading.Thread(target=_runner_coinbase, daemon=True, name='ob_coinbase').start()
    threading.Thread(target=_runner_bitget, daemon=True, name='ob_bitget').start()
    threading.Thread(target=_runner_kraken, daemon=True, name='ob_kraken').start()
    threading.Thread(target=_wall_scanner, daemon=True, name='ob_scan').start()
    print("[ob_ws] started Bybit+Binance depth + wall scanner", flush=True)
