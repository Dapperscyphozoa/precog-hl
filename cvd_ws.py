"""Cross-venue CVD (Cumulative Volume Delta).

2026-04-27: switched from Binance aggTrade to HL native trades WS.
Binance feed was returning 0 tracked coins (geo-block from Render).
HL exposes per-coin trades stream — same data, no restrictions, full
universe coverage instead of hardcoded 25 majors.

CVD rising = buyer aggression. CVD divergence from price = reversal signal.
"""
import json, threading, time
from collections import defaultdict, deque
try: import websocket
except ImportError: websocket = None

_CVD = defaultdict(lambda: deque(maxlen=600))  # coin -> deque[(ts, delta_usd)]
_LOCK = threading.Lock()
_RUN = False
_SUBSCRIBED_COINS = []
_LAST_MSG_TS = 0
_MSG_COUNT = 0
_LAST_ERR = ''


def _on_msg(ws, msg):
    """HL trades message format:
      {"channel":"trades","data":[{"coin":"BTC","side":"B"|"A","px":"...","sz":"...","time":...}]}
    'B' = buyer aggressor (taker bought) = positive delta (buy pressure)
    'A' = seller aggressor (taker sold) = negative delta (sell pressure)
    """
    global _LAST_MSG_TS, _MSG_COUNT
    try:
        m = json.loads(msg)
        if m.get('channel') != 'trades':
            return
        now = time.time()
        for trade in m.get('data', []):
            coin = trade.get('coin', '')
            if not coin:
                continue
            try:
                sz = float(trade.get('sz', 0))
                px = float(trade.get('px', 0))
            except (TypeError, ValueError):
                continue
            usd = sz * px
            side = trade.get('side', '')
            delta = usd if side == 'B' else -usd
            with _LOCK:
                _CVD[coin].append((now, delta))
            _MSG_COUNT += 1
            _LAST_MSG_TS = now
    except Exception:
        pass


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
        active = len([c for c, d in _CVD.items() if d and time.time() - d[-1][0] < 300])
    return {
        'tracked_coins': len(_CVD),
        'active': active,
        'source': 'HL native trades WS',
        'subscribed': len(_SUBSCRIBED_COINS),
        'msg_count': _MSG_COUNT,
        'last_msg_age_sec': int(time.time() - _LAST_MSG_TS) if _LAST_MSG_TS else -1,
        'last_err': _LAST_ERR,
    }


def _open(ws):
    """Subscribe to trades for each coin in our universe."""
    for coin in _SUBSCRIBED_COINS:
        try:
            ws.send(json.dumps({
                'method': 'subscribe',
                'subscription': {'type': 'trades', 'coin': coin}
            }))
        except Exception:
            pass


def _runner():
    global _LAST_ERR
    while _RUN:
        try:
            ws = websocket.WebSocketApp(
                'wss://api.hyperliquid.xyz/ws',
                on_message=_on_msg, on_open=_open,
                on_error=lambda ws, e: None, on_close=lambda ws, c, m: None
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            _LAST_ERR = f'{type(e).__name__}: {e}'
            print(f"[cvd] {e}", flush=True)
        if _RUN: time.sleep(5)


def start():
    global _RUN, _SUBSCRIBED_COINS
    if _RUN or websocket is None:
        if websocket is None:
            print("[cvd] websocket-client missing", flush=True)
        return
    # Pull universe from percoin_configs (same source as confluence)
    try:
        import percoin_configs as _pc
        _SUBSCRIBED_COINS = sorted(set(
            list(_pc.PURE_14.keys()) +
            list(_pc.NINETY_99.keys()) +
            list(_pc.EIGHTY_89.keys()) +
            list(_pc.SEVENTY_79.keys())
        ))
    except Exception:
        _SUBSCRIBED_COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'BNB']  # safe fallback
    _RUN = True
    threading.Thread(target=_runner, daemon=True, name='cvd_ws').start()
    print(f"[cvd] started (HL native, {len(_SUBSCRIBED_COINS)} coins)", flush=True)
