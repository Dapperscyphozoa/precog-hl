"""Data loader — pulls OHLC for backtest from yfinance, with CSV fallback.

Sources by priority:
  1. Local CSV cache (if present at data/<symbol>_<interval>.csv)
  2. yfinance
  3. Stooq daily (fallback for older history)

Symbols:
  Gold:  yfinance 'GC=F' (gold futures continuous)
  EURUSD: yfinance 'EURUSD=X'

Intervals:
  '1d' / '4h' / '1h' / '15m' / '5m' / '1m'

Yahoo limits intra-hour data to:
  1m → last 7 days
  5m → last 60 days
  15m → last 60 days
  1h → last 730 days
  1d → unlimited
"""
import os
from typing import Optional
import pandas as pd

DATA_DIR = os.path.dirname(os.path.abspath(__file__))


def _csv_path(symbol: str, interval: str) -> str:
    safe_symbol = symbol.replace('=', '').replace('/', '_')
    return os.path.join(DATA_DIR, f'{safe_symbol}_{interval}.csv')


def load_csv(symbol: str, interval: str) -> Optional[pd.DataFrame]:
    """Load from local CSV cache. Returns None if not present."""
    p = _csv_path(symbol, interval)
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    df.columns = [c.title() for c in df.columns]
    return df


def save_csv(df: pd.DataFrame, symbol: str, interval: str) -> str:
    p = _csv_path(symbol, interval)
    df.to_csv(p)
    return p


def fetch_yfinance(symbol: str, interval: str, period: str = 'max') -> pd.DataFrame:
    """Pull OHLC from Yahoo Finance. Requires network access."""
    import yfinance as yf
    df = yf.download(symbol, period=period, interval=interval,
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f'yfinance returned empty for {symbol} {interval}')
    # Flatten MultiIndex columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df.columns = [c.title() for c in df.columns]
    needed = ['Open', 'High', 'Low', 'Close']
    for c in needed:
        if c not in df.columns:
            raise RuntimeError(f'missing column {c} in {symbol} response')
    return df[needed + (['Volume'] if 'Volume' in df.columns else [])]


def load(symbol: str, interval: str, period: str = 'max',
         use_cache: bool = True) -> pd.DataFrame:
    """Load OHLC data with cache fallback.

    1. If use_cache and CSV exists, load it.
    2. Else fetch from yfinance and cache.
    3. Raise if both fail.
    """
    if use_cache:
        cached = load_csv(symbol, interval)
        if cached is not None and len(cached) > 50:
            return cached
    df = fetch_yfinance(symbol, interval, period=period)
    save_csv(df, symbol, interval)
    return df


def load_all_tfs(symbol: str, htf: str = '1d', mtf: str = '1h', ltf: str = '15m',
                 use_cache: bool = True):
    """Convenience: load HTF + MTF + LTF for the same symbol."""
    htf_df = load(symbol, htf, use_cache=use_cache)
    mtf_df = load(symbol, mtf, use_cache=use_cache)
    ltf_df = load(symbol, ltf, use_cache=use_cache)
    return htf_df, mtf_df, ltf_df
