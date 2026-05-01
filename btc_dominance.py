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

# Engine allowlist: if non-empty, ONLY these engines get the BTCD filter.
# Empty/unset = filter applies globally (legacy behavior). Comma-separated.
# Use case: filter only currently-blocked engines (HL, CONFLUENCE_BTC_WALL+*)
# without disturbing already-working engines (BB_REJ, PIVOT, DAY+NEWS) whose
# verified +EV was developed without the filter.
_ENGINE_FILTER_RAW = os.environ.get('BTCD_FILTER_ENGINES', '').strip()
ENGINE_FILTER = {e.strip() for e in _ENGINE_FILTER_RAW.split(',') if e.strip()}


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
    """Fetch latest close + close lookback_min ago.

    Uses 1h candles for low rate-limit footprint (1m candles get HL 429s).
    Rounds lookback up to whole hours: 60min→1h ago, 360min→6h ago, etc.
    Min lookback = 1h (60min). Sub-hour lookback uses 1h granularity.
    """
    lookback_h = max(1, (lookback_min + 30) // 60)  # round to nearest hour, min 1h
    now_ms = int(time.time() * 1000)
    span_h = lookback_h + 2  # +2h padding
    start_ms = now_ms - span_h * 3600 * 1000
    body = json.dumps({
        'type': 'candleSnapshot',
        'req': {'coin': coin, 'interval': '1h',
                'startTime': start_ms, 'endTime': now_ms}
    }).encode()
    _hl_throttle()
    req = urllib.request.Request(
        HL_INFO_URL, data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    if not isinstance(data, list) or len(data) < lookback_h + 1:
        return None, None
    closes = [float(b.get('c', 0) or 0) for b in data if b.get('c')]
    if len(closes) < lookback_h + 1:
        return None, None
    # latest close, close lookback_h bars back from end
    return closes[-1], closes[-(lookback_h + 1)]


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


def block_alt_side(coin, side, engine=None):
    """Decide whether to block this alt trade based on BTCD trend.

    BTCD rising  → alts weak  → block LONG alts
    BTCD falling → alts strong → block SHORT alts
    BTCD flat    → no bias → allow either side
    state=unknown → fail-soft, allow

    BTC excluded from gate (BTCD computed from BTC).

    If ENGINE_FILTER is set and engine is provided, only filter
    when engine ∈ ENGINE_FILTER. Otherwise (no engine arg or
    empty filter) filter applies to all callers.
    """
    if not ENABLED:
        return False, 'disabled'
    if coin == 'BTC':
        return False, 'btc-not-gated'
    # Engine-scoped filter (when ENGINE_FILTER is set):
    #   - engine in filter   → apply BTCD filter (continue below)
    #   - engine not in filter → skip filter (return allowed)
    #   - engine is None     → caller didn't identify engine → skip filter
    # This lets us scope BTCD filter to specific engines (e.g. verified losers)
    # without disturbing engines whose +EV was developed sans filter.
    if ENGINE_FILTER:
        if engine is None or engine not in ENGINE_FILTER:
            return False, 'engine-not-in-filter'
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
        'engine_filter': sorted(ENGINE_FILTER) if ENGINE_FILTER else [],
        'engine_filter_active': bool(ENGINE_FILTER),
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


def _refresh_daemon():
    """Background daemon — refresh BTCD state every REFRESH_INTERVAL_S.
    Ensures gate is ready before first signal arrives."""
    import time as _time
    while True:
        try:
            if ENABLED:
                _refresh()
        except Exception as e:
            _log(f'daemon err: {type(e).__name__}: {e}')
        _time.sleep(REFRESH_INTERVAL_S)


def _start_daemon():
    """Start refresh daemon if not already running. Idempotent."""
    if _CACHE.get('_daemon_started'):
        return
    _CACHE['_daemon_started'] = True
    t = threading.Thread(target=_refresh_daemon, daemon=True, name='btcd_refresh')
    t.start()
    _log('refresh daemon started')


# ─── 2026-05-01: FAST-LOOKBACK MODE FOR TRANSITION DETECTION ──────
# Independent of regime-tuned cache. Used by run_regime_flip_position_review()
# in precog.py to confirm BTCD direction at moment of regime flip.
# Cached separately (60s TTL) so regime-flip path doesn't pollute entry-gate cache.
_FAST_CACHE = {
    'ts': 0,
    'state': 'unknown',  # rising | falling | flat | unknown
    'change_pct': 0.0,
    'lookback_min': None,
    'threshold': None,
    'btcd_now': None,
    'btcd_pre': None,
    'last_err': None,
}
_FAST_LOCK = threading.Lock()
_FAST_CACHE_TTL_S = 60.0


def fast_state(lookback_min=15, threshold_pct=0.001):
    """Fast BTCD state for transition detection (separate from entry-gate cache).
    
    Returns dict: {state, change_pct, btcd_now, btcd_pre, lookback_min, threshold}
    
    state: 'rising' | 'falling' | 'flat' | 'unknown'
    
    Notes:
    - Lookback must be >= 1h granularity due to HL candle endpoint constraints.
      Sub-hour values get rounded up to 1h. 15min default = 1h actual.
    - Threshold is relative change (0.001 = 0.1%).
    - 60s cache TTL — repeated calls within window return cached.
    - Fail-soft: on fetch error returns state='unknown', last_err populated.
    """
    if not ENABLED:
        return {'state': 'disabled', 'change_pct': 0.0,
                'btcd_now': None, 'btcd_pre': None,
                'lookback_min': lookback_min, 'threshold': threshold_pct}
    now = time.time()
    with _FAST_LOCK:
        cached = dict(_FAST_CACHE)
    if (now - cached.get('ts', 0)) < _FAST_CACHE_TTL_S and cached.get('state') != 'unknown':
        return {
            'state': cached['state'],
            'change_pct': cached['change_pct'],
            'btcd_now': cached.get('btcd_now'),
            'btcd_pre': cached.get('btcd_pre'),
            'lookback_min': cached.get('lookback_min'),
            'threshold': cached.get('threshold'),
            'cached': True,
        }
    try:
        btc_now, btc_pre = _fetch_close_now_and_pre('BTC', lookback_min)
        eth_now, eth_pre = _fetch_close_now_and_pre('ETH', lookback_min)
        if not all([btc_now, eth_now, btc_pre, eth_pre]):
            with _FAST_LOCK:
                _FAST_CACHE['last_err'] = 'incomplete fetch'
                _FAST_CACHE['ts'] = now
                _FAST_CACHE['state'] = 'unknown'
            return {'state': 'unknown', 'change_pct': 0.0,
                    'btcd_now': None, 'btcd_pre': None,
                    'lookback_min': lookback_min, 'threshold': threshold_pct,
                    'last_err': 'incomplete fetch'}
        btcd_now = btc_now / eth_now
        btcd_pre = btc_pre / eth_pre
        change_pct = (btcd_now - btcd_pre) / btcd_pre
        if change_pct > threshold_pct:
            state = 'rising'
        elif change_pct < -threshold_pct:
            state = 'falling'
        else:
            state = 'flat'
        with _FAST_LOCK:
            _FAST_CACHE['ts'] = now
            _FAST_CACHE['state'] = state
            _FAST_CACHE['change_pct'] = change_pct
            _FAST_CACHE['btcd_now'] = btcd_now
            _FAST_CACHE['btcd_pre'] = btcd_pre
            _FAST_CACHE['lookback_min'] = lookback_min
            _FAST_CACHE['threshold'] = threshold_pct
            _FAST_CACHE['last_err'] = None
        return {
            'state': state,
            'change_pct': change_pct,
            'btcd_now': btcd_now,
            'btcd_pre': btcd_pre,
            'lookback_min': lookback_min,
            'threshold': threshold_pct,
            'cached': False,
        }
    except Exception as e:
        with _FAST_LOCK:
            _FAST_CACHE['last_err'] = str(e)
            _FAST_CACHE['ts'] = now
            _FAST_CACHE['state'] = 'unknown'
        return {'state': 'unknown', 'change_pct': 0.0,
                'btcd_now': None, 'btcd_pre': None,
                'lookback_min': lookback_min, 'threshold': threshold_pct,
                'last_err': str(e)}


def fast_state_status():
    """For diagnostic endpoints."""
    with _FAST_LOCK:
        c = dict(_FAST_CACHE)
    return {
        'last_check_age_sec': int(time.time() - c.get('ts', 0)) if c.get('ts') else None,
        'state': c.get('state', 'unknown'),
        'change_pct': round(c.get('change_pct', 0) * 100, 4),
        'lookback_min_used': c.get('lookback_min'),
        'threshold_pct_used': round((c.get('threshold') or 0) * 100, 4),
        'btcd_now': c.get('btcd_now'),
        'btcd_pre': c.get('btcd_pre'),
        'last_err': c.get('last_err'),
    }


# Auto-start daemon on module import
_start_daemon()
