"""Auto-pull macro from free sources — NO Yahoo, no user config required.

Primary source: Stooq (https://stooq.com)
  - Public CSV endpoint, no auth, used by quant shops for 20+ years
  - Daily EOD data for equities/indices; recent-enough for macro overlay
  - Rate-limit-friendly: we cache aggressively (default 30min)
  - Format: CSV with Symbol,Date,Time,Open,High,Low,Close,Volume,Name

Secondary source: Massive.io (user-owned key)
  - Hourly forex + gold data
  - Used when intraday granularity matters (e.g. mid-NY-session)
  - Key from env var MASSIVE_API_KEY

Both write into the same tv_macro_cache table so macro.py picks them up
via the existing tv_cache.read() path. TV webhook remains available as
a third option for users who want to push their own feeds.
"""
import os
import time
import json
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import tv_cache

_LOCK = threading.Lock()
_LAST_PULL = 0.0
TTL = int(os.environ.get('POSTMORTEM_AUTOMACRO_TTL', '1800'))   # 30min default
TIMEOUT = 8
UA = 'Mozilla/5.0 (precog-postmortem)'

# Stooq symbol → canonical cache symbol
STOOQ_SYMBOLS = {
    '^spx':   'SPX',
    '^vix':   'VIX',
    'dx.f':   'DXY',      # Dollar index futures
    'gc.f':   'GOLD',     # Gold futures
    'cl.f':   'OIL',      # Crude oil futures
    '^tnx':   'US10Y',    # 10-year Treasury yield
    '^dji':   'DJI',      # Dow Jones
    '^ndx':   'NDX',      # Nasdaq 100
    '^ftm':   'FTSE',     # FTSE 100
    '^dax':   'DAX',      # DAX
    '^n225':  'N225',     # Nikkei
}

# Massive.io symbols (prefix C: for forex/metals per their API)
# Disabled by default — requires MASSIVE_API_KEY env var
MASSIVE_SYMBOLS = {
    'C:XAUUSD': 'GOLD_INTRADAY',   # gold spot, hourly
    'C:GBPUSD': 'GBPUSD',
    'C:USDJPY': 'USDJPY',
    'C:USDCAD': 'USDCAD',
    'C:EURUSD': 'EURUSD',
}


def _http(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse_stooq_csv(body_bytes):
    """Parse Stooq single-quote CSV. Returns (price, prev_close) or None."""
    try:
        lines = body_bytes.decode('utf-8', errors='ignore').strip().split('\n')
        if len(lines) < 2: return None
        # Header: Symbol,Date,Time,Open,High,Low,Close,Volume,Name
        row = lines[1].split(',')
        if len(row) < 7: return None
        # N/D = no data
        if 'N/D' in row: return None
        open_p = float(row[3])
        close_p = float(row[6])
        return close_p, open_p
    except Exception:
        return None


def _fetch_stooq_one(stooq_sym, canonical):
    """Fetch one Stooq symbol and write to tv_cache."""
    try:
        url = f'https://stooq.com/q/l/?s={stooq_sym}&f=sd2t2ohlcvn&h&e=csv'
        body = _http(url, timeout=TIMEOUT)
        parsed = _parse_stooq_csv(body)
        if not parsed: return None
        price, prev_close = parsed
        written = tv_cache.write(
            symbol=canonical,
            price=price,
            prev_close=prev_close,
            timeframe='1d',
            raw={'source': 'stooq', 'stooq_sym': stooq_sym},
        )
        return written
    except Exception as e:
        return None


def _fetch_massive_one(ticker, canonical):
    """Fetch latest hourly bar from Massive.io for forex/metals."""
    api_key = os.environ.get('MASSIVE_API_KEY') or os.environ.get('MASSIVE_IO_API_KEY')
    if not api_key: return None
    try:
        # Last 4h window to catch the most recent hourly bar
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - 4 * 3600 * 1000
        url = (f'https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/hour/'
               f'{from_ms}/{now_ms}?adjusted=true&sort=desc&limit=2&apiKey={api_key}')
        body = _http(url, timeout=TIMEOUT)
        d = json.loads(body.decode())
        results = d.get('results') or []
        if not results: return None
        latest = results[0]
        prev = results[1] if len(results) > 1 else latest
        price = latest.get('c')
        prev_close = prev.get('c') if prev is not latest else latest.get('o')
        if price is None: return None
        written = tv_cache.write(
            symbol=canonical,
            price=float(price),
            prev_close=float(prev_close) if prev_close is not None else None,
            timeframe='1h',
            raw={'source': 'massive', 'ticker': ticker, 'bar_ts': latest.get('t')},
        )
        return written
    except Exception:
        return None


def pull_all(force=False):
    """Pull all auto-sources into tv_cache. Returns summary dict.

    Safe to call frequently — internally gated by TTL. Call from a
    background timer or from macro.fetch_all() when cache is stale.
    """
    global _LAST_PULL
    now = time.time()
    with _LOCK:
        if not force and (now - _LAST_PULL) < TTL:
            return {'ok': True, 'skipped': 'cached', 'age_sec': int(now - _LAST_PULL)}
        _LAST_PULL = now

    summary = {'ok': True, 'ts': now, 'stooq': {}, 'massive': {}}
    with ThreadPoolExecutor(max_workers=6) as ex:
        # Stooq parallel
        stooq_futs = {ex.submit(_fetch_stooq_one, s, c): c for s, c in STOOQ_SYMBOLS.items()}
        for fut, canon in stooq_futs.items():
            try:
                summary['stooq'][canon] = fut.result(timeout=TIMEOUT + 4) or 'failed'
            except Exception as e:
                summary['stooq'][canon] = f'err:{type(e).__name__}'

        # Massive.io parallel (only if API key set)
        if os.environ.get('MASSIVE_API_KEY') or os.environ.get('MASSIVE_IO_API_KEY'):
            massive_futs = {ex.submit(_fetch_massive_one, t, c): c for t, c in MASSIVE_SYMBOLS.items()}
            for fut, canon in massive_futs.items():
                try:
                    summary['massive'][canon] = fut.result(timeout=TIMEOUT + 4) or 'failed'
                except Exception as e:
                    summary['massive'][canon] = f'err:{type(e).__name__}'
        else:
            summary['massive'] = 'disabled (MASSIVE_API_KEY not set)'

    return summary


# Background thread: refresh every TTL seconds automatically
_DAEMON = None


def start_daemon():
    """Start a background thread that refreshes auto-pulled macro every TTL seconds."""
    global _DAEMON
    if _DAEMON and _DAEMON.is_alive():
        return False
    def _loop():
        # Initial pull after 30s so server boot isn't blocked
        time.sleep(30)
        while True:
            try:
                pull_all(force=True)
            except Exception as e:
                print(f'[postmortem.auto_macro] pull err: {e}', flush=True)
            time.sleep(TTL)
    _DAEMON = threading.Thread(target=_loop, name='auto_macro_daemon', daemon=True)
    _DAEMON.start()
    return True


def daemon_alive():
    return _DAEMON is not None and _DAEMON.is_alive()
