"""BTC Dominance proxy — captures alt-vs-BTC divergence.

REGIME-ADAPTIVE (2026-04-30): Lookback window and threshold are now
conditioned on the current market regime. Verified from /btcd_backtest
on 14-day, 491-trade window (PR #51-#55):

  bear-calm: 60min lookback / 0.1% threshold → Wlo 62.2%, n=30, sum +$0.76
  chop:      6h   lookback / 0.5% threshold → Wlo 50.6%, n=23, sum +$0.81
  bull-calm: insufficient data — borrows bear-calm config
  storm:     no observations in window — conservative 30min/0.5%
  unknown:   safe default 4h/0.5%

Sub-hour intervals (5m/15m/30m candles) tested and rejected — they don't
unlock new edge over 1h candles. Signal lives on regime's micro-rotation
timescale, not finer.

True BTC dominance requires market cap data we don't have on-chain. We
proxy via BTC/ETH ratio:

    btcd_proxy = BTC / ETH

BTC/ETH rising  = BTC outperforming ETH = alts weak (BTCD rising) → favor SHORTS
BTC/ETH falling = ETH outperforming BTC = alts strong (BTCD falling) → favor LONGS

ETH is the alt benchmark. Only BTC is excluded from the gate.

API:
  status() -> dict        # for /btcd_status
  block_alt_side(coin, side) -> (blocked: bool, reason: str)
  refresh()               # explicit refresh (auto every 5min)

Env overrides (all optional):
  BTCD_GATE_ENABLED         master toggle (default '1')
  BTCD_REFRESH_S            cache TTL (default 300s)

  Per-regime overrides — fall back to defaults if unset:
    BTCD_LOOKBACK_BEAR_CALM   minutes (default 60)
    BTCD_THRESHOLD_BEAR_CALM  fraction (default 0.001 = 0.1%)
    BTCD_LOOKBACK_BULL_CALM   default 60
    BTCD_THRESHOLD_BULL_CALM  default 0.001
    BTCD_LOOKBACK_BEAR_STORM  default 30
    BTCD_THRESHOLD_BEAR_STORM default 0.005
    BTCD_LOOKBACK_BULL_STORM  default 30
    BTCD_THRESHOLD_BULL_STORM default 0.005
    BTCD_LOOKBACK_CHOP        default 360 (6h)
    BTCD_THRESHOLD_CHOP       default 0.005
    BTCD_LOOKBACK_DEFAULT     default 240 (4h)
    BTCD_THRESHOLD_DEFAULT    default 0.005
"""
import os
import json
import time
import threading
import urllib.request


# ─── CONFIG ────────────────────────────────────────────────────────────
ENABLED              = os.environ.get('BTCD_GATE_ENABLED', '1') == '1'
REFRESH_INTERVAL_S   = int(os.environ.get('BTCD_REFRESH_S', '300'))
HL_INFO_URL          = 'https://api.hyperliquid.xyz/info'


def _env_minutes(key, default):
    try: return max(1, int(os.environ.get(key, str(default))))
    except Exception: return default


def _env_threshold(key, default):
    try: return max(0.0, float(os.environ.get(key, str(default))))
    except Exception: return default


# Verified-from-data per-regime configs (lookback in MINUTES, threshold as fraction)
REGIME_CONFIG = {
    'bear-calm':  {'lookback_min': _env_minutes('BTCD_LOOKBACK_BEAR_CALM',  60),
                   'threshold':    _env_threshold('BTCD_THRESHOLD_BEAR_CALM', 0.001)},
    'bull-calm':  {'lookback_min': _env_minutes('BTCD_LOOKBACK_BULL_CALM',  60),
                   'threshold':    _env_threshold('BTCD_THRESHOLD_BULL_CALM', 0.001)},
    'bear-storm': {'lookback_min': _env_minutes('BTCD_LOOKBACK_BEAR_STORM', 30),
                   'threshold':    _env_threshold('BTCD_THRESHOLD_BEAR_STORM', 0.005)},
    'bull-storm': {'lookback_min': _env_minutes('BTCD_LOOKBACK_BULL_STORM', 30),
                   'threshold':    _env_threshold('BTCD_THRESHOLD_BULL_STORM', 0.005)},
    'chop':       {'lookback_min': _env_minutes('BTCD_LOOKBACK_CHOP',      360),
                   'threshold':    _env_threshold('BTCD_THRESHOLD_CHOP',    0.005)},
}
DEFAULT_CONFIG = {'lookback_min': _env_minutes('BTCD_LOOKBACK_DEFAULT',     240),
                  'threshold':    _env_threshold('BTCD_THRESHOLD_DEFAULT',   0.005)}


_CACHE = {
    'ts': 0,
    'regime': None,
    'lookback_min_used': 0,
    'threshold_used': 0.0,
    'btc_price': 0.0,
    'eth_price': 0.0,
    'btcd_proxy_now': 0.0,
    'btcd_proxy_pre': 0.0,
    'btcd_change_pct': 0.0,
    'state': 'unknown',
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


def _get_regime():
    """Pull current regime from regime_detector. Returns string or None."""
    try:
        import regime_detector as _rd
        return _rd.get_regime()
    except Exception:
        return None


def _fetch_close_now_and_pre(coin, lookback_min):
    """Fetch latest 1m close + close lookback_min ago.

    Pads fetch by 10 minutes to ensure both bars are available.
    """
    now_ms = int(time.time() * 1000)
    span_min = lookback_min + 10
    start_ms = now_ms - span_min * 60 * 1000
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
    if not isinstance(data, list) or len(data) < lookback_min:
        return None, None
    closes = [float(b.get('c', 0) or 0) for b in data if b.get('c')]
    if len(closes) < lookback_min:
        return None, None
    # latest close, close lookback_min bars back from end
    return closes[-1], closes[-lookback_min]


def _refresh():
    """Fetch BTC + ETH prices using current regime's lookback. Update cache."""
    try:
        regime = _get_regime() or 'unknown'
        cfg = REGIME_CONFIG.get(regime, DEFAULT_CONFIG)
        lookback_min = cfg['lookback_min']
        threshold = cfg['threshold']

        btc_now, btc_pre = _fetch_close_now_and_pre('BTC', lookback_min)
        eth_now, eth_pre = _fetch_close_now_and_pre('ETH', lookback_min)
        if not all([btc_now, eth_now, btc_pre, eth_pre]):
            with _LOCK:
                _CACHE['errors'] += 1
                _CACHE['last_err'] = 'incomplete fetch'
            return None

        btcd_now = btc_now / eth_now
        btcd_pre = btc_pre / eth_pre
        change_pct = (btcd_now - btcd_pre) / btcd_pre

        if change_pct > threshold:
            state = 'rising'
        elif change_pct < -threshold:
            state = 'falling'
        else:
            state = 'flat'

        with _LOCK:
            _CACHE['ts'] = time.time()
            _CACHE['regime'] = regime
            _CACHE['lookback_min_used'] = lookback_min
            _CACHE['threshold_used'] = threshold
            _CACHE['btc_price'] = btc_now
            _CACHE['eth_price'] = eth_now
            _CACHE['btcd_proxy_now'] = btcd_now
            _CACHE['btcd_proxy_pre'] = btcd_pre
            _CACHE['btcd_change_pct'] = change_pct
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
        return
    _refresh()


def block_alt_side(coin, side):
    """Decide whether to block this alt trade based on BTCD trend.

    BTCD rising  → alts weak  → block LONG alts
    BTCD falling → alts strong → block SHORT alts
    BTCD flat    → no bias → allow either side
    state=unknown → fail-soft, allow

    BTC excluded from gate (BTCD computed from BTC).
    """
    if not ENABLED:
        return False, 'disabled'
    if coin == 'BTC':
        return False, 'btc-not-gated'
    refresh()
    with _LOCK:
        state = _CACHE.get('state', 'unknown')
    side_upper = (side or '').upper()
    if state == 'unknown':
        return False, 'no-data'
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
        'regime_at_last_refresh': c.get('regime'),
        'lookback_min_used': c.get('lookback_min_used', 0),
        'threshold_pct_used': round(c.get('threshold_used', 0) * 100, 4),
        'btcd_change_pct': round(c.get('btcd_change_pct', 0) * 100, 4),
        'btc_price': c.get('btc_price', 0),
        'eth_price': c.get('eth_price', 0),
        'btcd_proxy_now': round(c.get('btcd_proxy_now', 0), 6),
        'btcd_proxy_pre': round(c.get('btcd_proxy_pre', 0), 6),
        'regime_configs': {
            reg: {'lookback_min': cfg['lookback_min'],
                  'threshold_pct': round(cfg['threshold'] * 100, 4)}
            for reg, cfg in REGIME_CONFIG.items()
        },
        'default_config': {
            'lookback_min': DEFAULT_CONFIG['lookback_min'],
            'threshold_pct': round(DEFAULT_CONFIG['threshold'] * 100, 4),
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
