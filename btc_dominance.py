"""BTC Dominance proxy — captures alt-vs-BTC divergence.

Better than raw BTC direction because alts and BTC don't always move
together. When BTC dominance is rising, alts are weak relative to BTC
even if BTC is dropping (alts drop more). Conversely, falling BTCD
means alts are outperforming BTC even in a down move (long edge on alts).

True BTC dominance requires market cap data we don't have on-chain. We
proxy via BTC/ETH ratio — the industry standard for alt rotation:

    btcd_proxy = BTC / ETH

BTC/ETH rising = BTC outperforming ETH = alts weak (BTCD rising).
BTC/ETH falling = ETH outperforming BTC = alts strong (BTCD falling).

ETH is the alt benchmark. Only BTC is excluded from the gate — ETH and
all other alts are gated.

States:
- 'rising'  → BTCD up, alts weak relative to BTC → favor SHORTS
- 'falling' → BTCD down, alts strong vs BTC → favor LONGS
- 'flat'   → no clear bias

API:
  status() -> dict        # for /btcd_status
  block_alt_side(coin, side) -> (blocked: bool, reason: str)
  refresh()               # explicit refresh (auto every 5min)

Used as a complement (not replacement) for btc_correlation.
"""
import os
import json
import math
import time
import threading
import urllib.request

# ─── CONFIG ────────────────────────────────────────────────────────────
ENABLED              = os.environ.get('BTCD_GATE_ENABLED', '1') == '1'
RISING_THRESHOLD     = float(os.environ.get('BTCD_RISING_THRESHOLD', '0.003'))   # 0.3% change in 1h
FALLING_THRESHOLD    = float(os.environ.get('BTCD_FALLING_THRESHOLD', '-0.003'))  # -0.3%
REFRESH_INTERVAL_S   = int(os.environ.get('BTCD_REFRESH_S', '300'))  # 5 min
NEUTRAL_BUFFER       = float(os.environ.get('BTCD_NEUTRAL_BUFFER', '0.001'))  # 0.1% dead zone
HL_INFO_URL          = 'https://api.hyperliquid.xyz/info'

_CACHE = {
    'ts': 0,
    'btc_price': 0.0,
    'eth_price': 0.0,
    'btcd_proxy_now': 0.0,
    'btcd_proxy_1h_ago': 0.0,
    'btcd_change_1h_pct': 0.0,
    'state': 'unknown',  # 'rising' | 'falling' | 'flat' | 'unknown'
    'last_err': None,
    'fetch_count': 0,
    'errors': 0,
}
_LOCK = threading.Lock()


def _log(msg):
    print(f'[btc_dominance] {msg}', flush=True)


def _hl_throttle():
    try:
        import precog as _p
        if hasattr(_p, '_hl_throttle'):
            _p._hl_throttle()
    except Exception:
        pass


def _fetch_close_now_and_1h_ago(coin):
    """Fetch BTC/ETH/SOL closes: latest 1m close + close 1h ago."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 70 * 60 * 1000  # 70min back to ensure 1h-ago bar exists
    body = json.dumps({
        'type': 'candleSnapshot',
        'req': {'coin': coin, 'interval': '1m',
                'startTime': start_ms, 'endTime': now_ms}
    }).encode()
    _hl_throttle()
    req = urllib.request.Request(
        HL_INFO_URL, data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    if not isinstance(data, list) or len(data) < 60:
        return None, None
    closes = [float(b.get('c', 0) or 0) for b in data if b.get('c')]
    if len(closes) < 60:
        return None, None
    # current = latest close, 1h_ago = ~60 bars back from end
    return closes[-1], closes[-60]


def _refresh():
    """Fetch BTC and ETH prices, compute BTCD proxy = BTC/ETH ratio."""
    try:
        btc_now, btc_1h = _fetch_close_now_and_1h_ago('BTC')
        eth_now, eth_1h = _fetch_close_now_and_1h_ago('ETH')
        if not all([btc_now, eth_now, btc_1h, eth_1h]):
            with _LOCK:
                _CACHE['errors'] += 1
                _CACHE['last_err'] = 'incomplete fetch'
            return None
        btcd_now = btc_now / eth_now
        btcd_1h  = btc_1h  / eth_1h
        change_1h_pct = (btcd_now - btcd_1h) / btcd_1h
        # Classify with neutral buffer
        if change_1h_pct > RISING_THRESHOLD:
            state = 'rising'
        elif change_1h_pct < FALLING_THRESHOLD:
            state = 'falling'
        else:
            state = 'flat'
        with _LOCK:
            _CACHE['ts'] = time.time()
            _CACHE['btc_price'] = btc_now
            _CACHE['eth_price'] = eth_now
            _CACHE['btcd_proxy_now'] = btcd_now
            _CACHE['btcd_proxy_1h_ago'] = btcd_1h
            _CACHE['btcd_change_1h_pct'] = change_1h_pct
            _CACHE['state'] = state
            _CACHE['fetch_count'] += 1
            _CACHE['last_err'] = None
        return state
    except Exception as e:
        with _LOCK:
            _CACHE['errors'] += 1
            _CACHE['last_err'] = f'{type(e).__name__}: {e}'
        _log(f'refresh err: {type(e).__name__}: {e}')
        return None


def refresh():
    """Public refresh trigger. Idempotent — respects refresh interval."""
    if not ENABLED:
        return
    now = time.time()
    if now - _CACHE.get('ts', 0) < REFRESH_INTERVAL_S:
        return  # too recent
    _refresh()


def block_alt_side(coin, side):
    """Decide whether to block this alt trade based on BTCD trend.

    Args:
      coin: coin symbol (BTC/ETH not subject to gate)
      side: 'BUY' or 'SELL'

    Returns:
      (blocked: bool, reason: str)
        - Block LONG alts when BTCD rising (alts weak)
        - Block SHORT alts when BTCD falling (alts strong)
        - Pass otherwise (flat or unknown)
    """
    if not ENABLED:
        return False, 'disabled'
    # Only BTC excluded — ETH and all other alts are gated.
    if coin == 'BTC':
        return False, 'btc-not-gated'
    # Auto-refresh if cache stale
    refresh()
    with _LOCK:
        state = _CACHE.get('state', 'unknown')
    side_upper = (side or '').upper()
    if state == 'unknown':
        return False, 'no-data'  # fail-soft
    if state == 'rising' and side_upper in ('BUY', 'B', 'L', 'LONG'):
        return True, 'btcd_rising_blocks_long'
    if state == 'falling' and side_upper in ('SELL', 'S', 'SHORT'):
        return True, 'btcd_falling_blocks_short'
    return False, 'allowed'


def status():
    """Snapshot for /btcd_status endpoint."""
    with _LOCK:
        c = dict(_CACHE)
    return {
        'enabled': ENABLED,
        'state': c.get('state', 'unknown'),
        'btcd_change_1h_pct': round(c.get('btcd_change_1h_pct', 0) * 100, 4),
        'btc_price': c.get('btc_price', 0),
        'eth_price': c.get('eth_price', 0),
        'btcd_proxy_now': round(c.get('btcd_proxy_now', 0), 6),
        'btcd_proxy_1h_ago': round(c.get('btcd_proxy_1h_ago', 0), 6),
        'thresholds': {
            'rising_pct': RISING_THRESHOLD * 100,
            'falling_pct': FALLING_THRESHOLD * 100,
            'neutral_buf_pct': NEUTRAL_BUFFER * 100,
        },
        'last_refresh_ts': c.get('ts', 0),
        'last_refresh_age_sec': int(time.time() - c.get('ts', 0)) if c.get('ts') else None,
        'refresh_interval_s': REFRESH_INTERVAL_S,
        'fetch_count': c.get('fetch_count', 0),
        'errors': c.get('errors', 0),
        'last_err': c.get('last_err'),
        'interpretation': {
            'rising':  'alts weak vs BTC — favor SHORTS, block LONGS',
            'falling': 'alts strong vs BTC — favor LONGS, block SHORTS',
            'flat':    'no clear bias — gate is no-op',
            'unknown': 'no data yet',
        }.get(c.get('state', 'unknown'), 'unknown'),
    }
