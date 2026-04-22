"""TradingView webhook cache for macro symbols.

User configures TradingView alerts on DXY / SPX / VIX / GOLD / OIL / etc
to POST to /postmortem/tv/macro with a simple payload:

    {
      "symbol": "DXY",
      "price": 106.42,
      "prev_close": 106.10,       # optional — enables % change calc
      "timeframe": "1h"           # optional — for display only
    }

TradingView alert message template (paste this into any TV alert's Message
field, then configure webhook URL → https://<your-render>/postmortem/tv/macro
with header X-Webhook-Secret: <secret>):

    {"symbol":"{{ticker}}","price":{{close}},"prev_close":{{open}},"timeframe":"{{interval}}"}

The cache stores the most-recent value per symbol in SQLite. Reads are
used by macro.py to inject DXY/SPX/VIX/etc into the entry-gate prompt
without any Yahoo dependency.

Stale entries (>2h old) are ignored by the formatter.
"""
import os
import time
import threading

from . import db as _db  # reuses same /var/data/postmortem.db

_LOCK = threading.Lock()
_TABLE_READY = False

# Allowed symbols (case-insensitive on write, stored uppercase)
ALLOWED = {
    # Equities / indices
    'DXY', 'SPX', 'SPY', 'VIX', 'GOLD', 'GOLD_INTRADAY', 'XAUUSD',
    'OIL', 'USOIL', 'WTI', 'US10Y', 'TNX', 'TLT', 'DJI', 'NDX', 'QQQ', 'ES', 'NQ',
    'FTSE', 'DAX', 'N225',
    # Crypto dominance / mcap
    'BTC_D', 'BTC.D', 'ETH_D', 'ETH.D', 'TOTAL', 'TOTAL2', 'TOTAL3',
    # FX majors
    'EURUSD', 'GBPUSD', 'USDJPY', 'USDCAD', 'USDCHF', 'AUDUSD', 'NZDUSD',
    'EURJPY', 'GBPJPY', 'EURGBP',
}

# Normalize incoming symbol → canonical key we store under
def _normalize(sym):
    if not sym: return None
    s = str(sym).strip().upper().replace('.', '_')
    # Strip any exchange prefix first: "TVC:DXY" → "DXY", "CBOE:VIX" → "VIX", etc.
    if ':' in s:
        s = s.split(':', 1)[1]
    # Strip TradingView continuous-contract suffix: "GC1!" → "GC1", leave as is if still meaningful
    remap = {
        'DXY': 'DXY',
        'SPX': 'SPX', 'SP500': 'SPX', 'SPY': 'SPY',
        'VIX': 'VIX',
        'GC1!': 'GOLD', 'GC1': 'GOLD', 'XAUUSD': 'GOLD', 'GOLD': 'GOLD',
        'CL1!': 'OIL', 'CL1': 'OIL', 'USOIL': 'OIL', 'WTI': 'OIL', 'OIL': 'OIL',
        'US10Y': 'US10Y', 'TNX': 'US10Y', '^TNX': 'US10Y',
        'BTC_D': 'BTC_D',
        'ETH_D': 'ETH_D',
        'TOTAL': 'TOTAL', 'TOTAL2': 'TOTAL2', 'TOTAL3': 'TOTAL3',
        'DJI': 'DJI', 'NDX': 'NDX', 'QQQ': 'QQQ', 'ES1!': 'ES', 'NQ1!': 'NQ',
        'TLT': 'TLT',
    }
    if s in remap: return remap[s]
    if s in ALLOWED: return s
    return None


def _ensure_table():
    global _TABLE_READY
    if _TABLE_READY: return
    try:
        _db.init_db()
        with _LOCK, _db._conn() as c:
            c.execute('''
                CREATE TABLE IF NOT EXISTS tv_macro_cache (
                    symbol       TEXT PRIMARY KEY,
                    price        REAL,
                    prev_close   REAL,
                    chg_pct_session REAL,
                    ts           REAL NOT NULL,
                    timeframe    TEXT,
                    raw_json     TEXT
                )
            ''')
            c.commit()
        _TABLE_READY = True
    except Exception as e:
        print(f'[postmortem.tv_cache] ensure_table err: {e}', flush=True)


def write(symbol, price, prev_close=None, timeframe=None, raw=None):
    """Write a single TV push. Returns canonical symbol or None."""
    _ensure_table()
    sym = _normalize(symbol)
    if not sym:
        return None
    try:
        price = float(price)
    except Exception:
        return None
    pc = None
    chg = None
    try:
        if prev_close is not None:
            pc = float(prev_close)
            if pc > 0:
                chg = round((price - pc) / pc * 100.0, 3)
    except Exception:
        pass
    import json as _json
    try:
        with _LOCK, _db._conn() as c:
            c.execute('''
                INSERT INTO tv_macro_cache(symbol, price, prev_close, chg_pct_session, ts, timeframe, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    price=excluded.price,
                    prev_close=excluded.prev_close,
                    chg_pct_session=excluded.chg_pct_session,
                    ts=excluded.ts,
                    timeframe=excluded.timeframe,
                    raw_json=excluded.raw_json
            ''', (sym, price, pc, chg, time.time(), timeframe,
                  _json.dumps(raw) if raw is not None else None))
            c.commit()
        return sym
    except Exception as e:
        print(f'[postmortem.tv_cache] write err: {e}', flush=True)
        return None


def read(symbol):
    _ensure_table()
    sym = _normalize(symbol)
    if not sym: return None
    try:
        with _db._conn() as c:
            row = c.execute(
                'SELECT symbol, price, prev_close, chg_pct_session, ts, timeframe FROM tv_macro_cache WHERE symbol=?',
                (sym,)
            ).fetchone()
        if not row: return None
        d = dict(row)
        d['age_sec'] = int(time.time() - d['ts']) if d.get('ts') else None
        return d
    except Exception:
        return None


def list_all():
    _ensure_table()
    try:
        with _db._conn() as c:
            rows = c.execute(
                'SELECT * FROM tv_macro_cache ORDER BY ts DESC'
            ).fetchall()
        now = time.time()
        out = []
        for r in rows:
            d = dict(r)
            d['age_sec'] = int(now - d['ts']) if d.get('ts') else None
            out.append(d)
        return out
    except Exception:
        return []


def purge_stale(max_age_sec=86400):
    _ensure_table()
    try:
        with _LOCK, _db._conn() as c:
            c.execute('DELETE FROM tv_macro_cache WHERE ts < ?', (time.time() - max_age_sec,))
            c.commit()
        return True
    except Exception:
        return False
