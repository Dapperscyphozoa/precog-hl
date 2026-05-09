"""
btcd_regime.py — BTC dominance proxy regime tracker.

True BTCD = BTC market cap / total crypto market cap. We don't have a market-cap
feed, so we proxy with: BTC's 4h log-return MINUS the equal-weighted alt 4h
log-return. Positive proxy slope = BTC outperforming (BTCD rising).
Negative = alts outperforming (BTCD falling).

Refresh cadence: every 5 minutes. Uses Hyperliquid candleSnapshot for BTC + a
sampled basket of 8-12 liquid alts. Result cached, stale if > 15 min old.

Backtest result on 302 wick fades / 120d:
  baseline (no filter)        WR 50.7%  +$9.50 / 302
  slope < -0.3 (filter ON)    WR 53.7%  +$11.20 / 244   ← chosen threshold
  slope < -1.0 (extreme)      WR 42.9%  worse — euphoria

Live default: skip long wick fades when btcd_slope_4h >= -0.3.
Reverse logic for shorts (skip when slope <= +0.3) — when SMC/wick fade engine
goes bidirectional later this becomes meaningful.
"""
import logging
import math
import threading
import time
import urllib.request
import json as _json

log = logging.getLogger(__name__)

HL_INFO = 'https://api.hyperliquid.xyz/info'
ALT_BASKET = ['SOL', 'AVAX', 'ARB', 'OP', 'SUI', 'APT', 'INJ', 'TIA',
              'ENA', 'PENDLE', 'HYPE', 'TAO']
REFRESH_SEC = 300        # 5 min
STALE_AFTER_SEC = 900    # 15 min

_state = {
    'slope_4h': 0.0,
    'last_refresh_ms': 0,
    'last_btc_4h_ret': 0.0,
    'last_alt_4h_ret': 0.0,
    'sample_size': 0,
}
_lock = threading.Lock()
_thread = None
_stop = False


def _hl_post(payload):
    body = _json.dumps(payload).encode()
    req = urllib.request.Request(HL_INFO, data=body,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=8) as r:
        return _json.loads(r.read())


def _4h_log_ret(coin, end_ms):
    """Fetch last 17 bars of 15m and compute log-return over 16 bars (4h)."""
    try:
        # 17 bars * 15min = 255min ≈ 4h15m, gives a buffer
        start_ms = end_ms - 17 * 15 * 60 * 1000
        bars = _hl_post({'type': 'candleSnapshot',
                         'req': {'coin': coin, 'interval': '15m',
                                 'startTime': start_ms, 'endTime': end_ms}})
        if not bars or len(bars) < 17:
            return None
        c0 = float(bars[-17]['c'])
        c1 = float(bars[-1]['c'])
        if c0 <= 0 or c1 <= 0:
            return None
        return math.log(c1 / c0) * 100
    except Exception as e:
        log.warning(f"btcd_regime: fetch {coin} failed: {e}")
        return None


def refresh():
    """Compute and cache BTCD proxy slope. Idempotent, thread-safe."""
    end_ms = int(time.time() * 1000)
    btc_ret = _4h_log_ret('BTC', end_ms)
    if btc_ret is None:
        log.warning("btcd_regime: BTC fetch failed, skipping refresh")
        return
    alt_rets = []
    for alt in ALT_BASKET:
        r = _4h_log_ret(alt, end_ms)
        if r is not None:
            alt_rets.append(r)
        time.sleep(0.1)  # gentle pacing
    if not alt_rets:
        log.warning("btcd_regime: no alt rets fetched")
        return
    mean_alt = sum(alt_rets) / len(alt_rets)
    slope = btc_ret - mean_alt
    with _lock:
        _state['slope_4h'] = slope
        _state['last_btc_4h_ret'] = btc_ret
        _state['last_alt_4h_ret'] = mean_alt
        _state['sample_size'] = len(alt_rets)
        _state['last_refresh_ms'] = end_ms
    log.info(f"btcd_regime: slope_4h={slope:+.3f} (BTC {btc_ret:+.2f}%, alts {mean_alt:+.2f}% n={len(alt_rets)})")


def get_slope():
    """Return current BTCD proxy slope, or None if stale."""
    with _lock:
        age_ms = int(time.time() * 1000) - _state['last_refresh_ms']
        if age_ms > STALE_AFTER_SEC * 1000:
            return None
        return _state['slope_4h']


def get_status():
    with _lock:
        age_ms = int(time.time() * 1000) - _state['last_refresh_ms']
        return {**_state, 'age_sec': age_ms // 1000}


def _loop():
    while not _stop:
        try:
            refresh()
        except Exception as e:
            log.exception(f"btcd_regime loop error: {e}")
        for _ in range(REFRESH_SEC):
            if _stop:
                return
            time.sleep(1)


def start():
    """Start refresher thread. Idempotent."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    refresh()  # first sync refresh
    _thread = threading.Thread(target=_loop, daemon=True, name='btcd_regime')
    _thread.start()
    log.info("btcd_regime: refresher started")


def stop():
    global _stop
    _stop = True
