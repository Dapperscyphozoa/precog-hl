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
    """Fetch one Stooq symbol and write to tv_cache. Returns tuple (status, detail)."""
    try:
        url = f'https://stooq.com/q/l/?s={stooq_sym}&f=sd2t2ohlcvn&h&e=csv'
        body = _http(url, timeout=TIMEOUT)
        parsed = _parse_stooq_csv(body)
        if not parsed:
            # Return first 100 chars of body for diagnostic
            snippet = body[:120].decode('utf-8', errors='replace').replace('\n','|')
            return ('parse_failed', snippet)
        price, prev_close = parsed
        written = tv_cache.write(
            symbol=canonical,
            price=price,
            prev_close=prev_close,
            timeframe='1d',
            raw={'source': 'stooq', 'stooq_sym': stooq_sym},
        )
        return ('ok', written or 'write_failed')
    except urllib.error.HTTPError as e:
        return (f'http_{e.code}', str(e)[:100])
    except Exception as e:
        return (f'err_{type(e).__name__}', str(e)[:100])


def _fetch_massive_one(ticker, canonical):
    """Fetch latest daily bar from Massive.io for forex/metals.

    Note: this account tier returns DELAYED/empty on /range/1/hour/.
    Daily bars work fine. For intraday, upgrade Massive.io subscription.
    Returns tuple (status, detail) for diagnostics.
    """
    api_key = os.environ.get('MASSIVE_API_KEY') or os.environ.get('MASSIVE_IO_API_KEY')
    if not api_key:
        return ('no_key', 'MASSIVE_API_KEY not set')
    try:
        # 7-day window to handle weekend gaps (FX closed Sat/Sun)
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - 7 * 24 * 3600 * 1000
        url = (f'https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/day/'
               f'{from_ms}/{now_ms}?adjusted=true&sort=desc&limit=3&apiKey={api_key}')
        body = _http(url, timeout=TIMEOUT)
        d = json.loads(body.decode())
        if d.get('status') == 'ERROR':
            return ('api_error', d.get('error', 'unknown')[:100])
        results = d.get('results') or []
        if not results:
            return ('empty_results', f'status={d.get("status","?")} count={d.get("resultsCount",0)}')
        latest = results[0]
        prev = results[1] if len(results) > 1 else latest
        price = latest.get('c')
        prev_close = prev.get('c') if prev is not latest else latest.get('o')
        if price is None:
            return ('no_close', 'results missing close price')
        written = tv_cache.write(
            symbol=canonical,
            price=float(price),
            prev_close=float(prev_close) if prev_close is not None else None,
            timeframe='1d',
            raw={'source': 'massive', 'ticker': ticker, 'bar_ts': latest.get('t'),
                 'api_status': d.get('status')},
        )
        return ('ok', written or 'write_failed')
    except urllib.error.HTTPError as e:
        return (f'http_{e.code}', str(e)[:100])
    except Exception as e:
        return (f'err_{type(e).__name__}', str(e)[:100])


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
                result = fut.result(timeout=TIMEOUT + 4)
                # _fetch_stooq_one now returns tuple (status, detail)
                if isinstance(result, tuple):
                    summary['stooq'][canon] = {'status': result[0], 'detail': result[1]}
                else:
                    summary['stooq'][canon] = result or 'failed'
            except Exception as e:
                summary['stooq'][canon] = f'err:{type(e).__name__}'

        # Massive.io parallel (only if API key set)
        if os.environ.get('MASSIVE_API_KEY') or os.environ.get('MASSIVE_IO_API_KEY'):
            massive_futs = {ex.submit(_fetch_massive_one, t, c): c for t, c in MASSIVE_SYMBOLS.items()}
            for fut, canon in massive_futs.items():
                try:
                    result = fut.result(timeout=TIMEOUT + 4)
                    if isinstance(result, tuple):
                        summary['massive'][canon] = {'status': result[0], 'detail': result[1]}
                    else:
                        summary['massive'][canon] = result or 'failed'
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
