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
            bids = {float(b[0]):float(b[1]) for b in snap.get('bids',[])[:50]}
            asks = {float(a[0]):float(a[1]) for a in snap.get('asks',[])[:50]}
            with _LOCK:
                d = _DEPTH.setdefault(hl, {'bids':{}, 'asks':{}, 'mid':0, 'ts':0})
                for p,s in bids.items(): d['bids'][p] = d['bids'].get(p,0) + s
                for p,s in asks.items(): d['asks'][p] = d['asks'].get(p,0) + s
                d['ts'] = time.time()
                if bids and asks: d['mid'] = (max(bids.keys()) + min(asks.keys())) / 2
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
        if m.get('type') != 'snapshot' and m.get('type') != 'l2update': return
        prod = m.get('product_id','')
        hl = None
        for h,c in HL_TO_COINBASE.items():
            if c == prod: hl = h; break
        if not hl: return
        with _LOCK:
            d = _DEPTH.setdefault(hl, {'bids':{}, 'asks':{}, 'mid':0, 'ts':0})
            if m.get('type') == 'snapshot':
                for b in m.get('bids',[])[:50]: d['bids'][float(b[0])] = d['bids'].get(float(b[0]),0) + float(b[1])
                for a in m.get('asks',[])[:50]: d['asks'][float(a[0])] = d['asks'].get(float(a[0]),0) + float(a[1])
            else:
                for side,p,s in m.get('changes',[]):
                    book = d['bids'] if side=='buy' else d['asks']
                    sz = float(s)
                    if sz == 0: book.pop(float(p), None)
                    else: book[float(p)] = book.get(float(p),0) + sz
            d['ts'] = time.time()
            if d['bids'] and d['asks']: d['mid'] = (max(d['bids'].keys()) + min(d['asks'].keys())) / 2
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
    threading.Thread(target=_wall_scanner, daemon=True, name='ob_scan').start()
    print("[ob_ws] started Bybit+Binance depth + wall scanner", flush=True)
