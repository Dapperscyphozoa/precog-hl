"""whale_display — populates the dashboard's whale-prints feed from
Hyperliquid's public trades WebSocket.

Read-only display feed. Does NOT touch whale_filter, does NOT affect
confluence_boost scoring, does NOT modify trade sizing or any engine
behaviour. The /whales HTTP route reads from get_recent() and the result
is for visual display only.

Why a separate module:
  whale_filter.py drives confluence_boost() which scales position size
  on every trade. Changing its data source would change live trading
  behaviour. This module is parallel: it reads the same kind of data
  (large prints) from a different source (HL public WS instead of
  Binance aggTrade) and only ever serves it to the dashboard.

Source: wss://api.hyperliquid.xyz/ws — public, no auth, free.
Subscribes to `trades` channel per coin. HL's universe-of-40 trades
flow through here in real time. Filter prints over WHALE_USD threshold.

Capacity: 200 prints per coin in a deque. Older prints fall off as new
ones arrive. /whales reads only the most recent N across all coins.
"""
import json
import threading
import time
from collections import defaultdict, deque

try:
    import websocket
except ImportError:
    websocket = None

WHALE_USD = 25_000
WS_URL = 'wss://api.hyperliquid.xyz/ws'

# Coins to monitor. Subset of HL universe that consistently has whale-sized
# prints. Matches the scoring path's coverage so dashboard and confluence
# math stay roughly aligned in coverage even though sources differ.
COINS = [
    'BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'AVAX', 'LINK', 'BNB', 'DOT',
    'ATOM', 'SUI', 'DOGE', 'ARB', 'OP', 'LTC', 'TIA', 'APT', 'FIL',
    'NEAR', 'ENA', 'WIF', 'JUP', 'ORDI', 'TON', 'AAVE', 'INJ', 'LDO',
    'PYTH', 'SAND', 'UNI', 'PENDLE', 'HYPE',
]

_PRINTS = defaultdict(lambda: deque(maxlen=200))   # coin -> deque[(ts, side, usd, px, sz)]
_LOCK = threading.Lock()
_RUN = False
_STATE = {'connected': False, 'last_msg_ts': 0, 'errors': 0, 'last_err': None, 'msgs_total': 0, 'msgs_trades': 0, 'channels_seen': {}}


def _on_msg(ws, msg):
    """Receive trade messages, filter to whale prints, store."""
    try:
        _STATE['msgs_total'] += 1
        _STATE['last_msg_ts'] = time.time()
        m = json.loads(msg)
        ch = m.get('channel', 'unknown')
        _STATE['channels_seen'][ch] = _STATE['channels_seen'].get(ch, 0) + 1
        if ch != 'trades':
            return
        _STATE['msgs_trades'] += 1
        data = m.get('data', [])
        if not isinstance(data, list):
            return
        for t in data:
            try:
                coin = t.get('coin', '')
                px = float(t.get('px', 0))
                sz = float(t.get('sz', 0))
                usd = px * sz
                if usd < WHALE_USD:
                    continue
                # HL: side='B' = buy aggressor, 'A' = sell aggressor
                side_raw = t.get('side', '')
                side = 'BUY' if side_raw == 'B' else 'SELL'
                ts_ms = int(t.get('time', time.time() * 1000))
                ts = ts_ms / 1000.0
                with _LOCK:
                    _PRINTS[coin].append((ts, side, usd, px, sz))
            except Exception:
                continue
    except Exception as e:
        _STATE['errors'] += 1
        _STATE['last_err'] = f'msg parse: {e}'


def _on_open(ws):
    """Subscribe to trades channel for each monitored coin.

    HL's WS accepts multiple subscriptions on a single connection. We send
    them with a small delay between each to stay polite — sending 32 in
    a tight loop on connect can occasionally trigger rate-limiting.
    """
    _STATE['connected'] = True
    def _subscribe_all():
        for coin in COINS:
            try:
                ws.send(json.dumps({
                    'method': 'subscribe',
                    'subscription': {'type': 'trades', 'coin': coin},
                }))
                time.sleep(0.05)  # 50ms between subscribes
            except Exception as e:
                _STATE['errors'] += 1
                _STATE['last_err'] = f'subscribe {coin}: {e}'
    threading.Thread(target=_subscribe_all, daemon=True).start()


def _on_err(ws, e):
    _STATE['errors'] += 1
    _STATE['last_err'] = str(e)[:200]


def _on_close(ws, code, reason):
    _STATE['connected'] = False


def _runner():
    """Reconnect loop. WS app closed → wait → reconnect."""
    while _RUN:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=_on_open,
                on_message=_on_msg,
                on_error=_on_err,
                on_close=_on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            _STATE['errors'] += 1
            _STATE['last_err'] = f'runner: {e}'
        if _RUN:
            time.sleep(5)  # backoff before reconnect


def start():
    """Idempotent. Spins up a daemon thread to maintain the WS subscription."""
    global _RUN
    if _RUN:
        return
    if websocket is None:
        _STATE['last_err'] = 'websocket-client not installed'
        return
    _RUN = True
    threading.Thread(target=_runner, daemon=True, name='whale_display_ws').start()


def get_recent(limit=20, max_age_sec=900):
    """Return the most recent whale prints across all coins.
    Oldest cap = 15 min by default so the feed always shows fresh activity."""
    now = time.time()
    cutoff = now - max_age_sec
    items = []
    with _LOCK:
        for coin, dq in _PRINTS.items():
            for ts, side, usd, px, sz in dq:
                if ts < cutoff:
                    continue
                items.append({
                    'coin': coin, 'side': side, 'usd': usd,
                    'px': px, 'sz': sz, 'ts': ts,
                })
    items.sort(key=lambda x: x['ts'], reverse=True)
    return items[:limit]


def status():
    """Diagnostic. Tells us whether the WS is alive + how much data is buffered."""
    with _LOCK:
        total = sum(len(v) for v in _PRINTS.values())
        active = sum(1 for c, d in _PRINTS.items() if d and time.time() - d[-1][0] < 300)
    return {
        'source': 'hyperliquid_ws',
        'connected': _STATE['connected'],
        'last_msg_age_sec': round(time.time() - _STATE['last_msg_ts'], 1) if _STATE['last_msg_ts'] else None,
        'errors': _STATE['errors'],
        'last_err': _STATE['last_err'],
        'tracked_coins': len(_PRINTS),
        'active_coins': active,
        'total_prints': total,
        'msgs_total': _STATE['msgs_total'],
        'msgs_trades': _STATE['msgs_trades'],
        'channels_seen': dict(_STATE['channels_seen']),
        'threshold_usd': WHALE_USD,
        'subscribed_coins': len(COINS),
    }
