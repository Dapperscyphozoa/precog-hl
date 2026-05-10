"""SMC PRIMITIVE — Liquidity Sweep.

Sweep = a candle whose wick exceeds a prior swing high (sell-side
liquidity grab) or swing low (buy-side liquidity grab) AND closes
back inside the prior range. The "wick out, close in" is the
signature of stops being raided then price reversing.

Inputs: dataframe + reference swing levels (from structure.py).
Output: bool per bar + sweep direction.

Tunables:
  min_wick_ratio   wick must be > N × body (default 1.0 — 1:1 ratio)
  lookback_window  how many bars back to consider for swing reference (default 20)
"""
from dataclasses import dataclass
from typing import List, Optional
import numpy as np
import pandas as pd

from .structure import detect_pivots, Pivot


@dataclass
class Sweep:
    idx: int
    ts: pd.Timestamp
    side: str         # 'buy_side' (swept low; bullish reversal candidate)
                      # 'sell_side' (swept high; bearish reversal candidate)
    swept_level: float
    candle_close: float


def detect_sweeps(df: pd.DataFrame,
                  pivot_lookback: int = 5,
                  min_wick_ratio: float = 1.0,
                  swing_lookback: int = 20) -> List[Sweep]:
    """Iterate bars, flag those that sweep prior swing highs/lows and close back."""
    n = len(df)
    if n < pivot_lookback * 2 + 5:
        return []

    o = df['Open'].values
    h = df['High'].values
    l = df['Low'].values
    c = df['Close'].values

    # Pre-compute swing highs/lows for reference
    pivots = detect_pivots(df, lookback=pivot_lookback)
    pivot_highs = [(p.idx, p.price) for p in pivots if p.kind in ('HH', 'LH')]
    pivot_lows = [(p.idx, p.price) for p in pivots if p.kind in ('HL', 'LL')]

    sweeps: List[Sweep] = []
    for i in range(pivot_lookback + 1, n):
        body = abs(c[i] - o[i])
        upper_wick = h[i] - max(o[i], c[i])
        lower_wick = min(o[i], c[i]) - l[i]
        if body <= 0:
            body = max(body, 1e-12)

        # Check sell-side sweep (sweep a swing high; close back below)
        recent_highs = [(idx, px) for idx, px in pivot_highs
                        if i - swing_lookback <= idx < i]
        for hidx, hpx in recent_highs:
            if h[i] > hpx and c[i] < hpx and upper_wick >= min_wick_ratio * body:
                sweeps.append(Sweep(
                    idx=i, ts=df.index[i], side='sell_side',
                    swept_level=hpx, candle_close=float(c[i])))
                break  # only one sweep label per candle

        # Check buy-side sweep (sweep a swing low; close back above)
        recent_lows = [(idx, px) for idx, px in pivot_lows
                       if i - swing_lookback <= idx < i]
        for lidx, lpx in recent_lows:
            if l[i] < lpx and c[i] > lpx and lower_wick >= min_wick_ratio * body:
                sweeps.append(Sweep(
                    idx=i, ts=df.index[i], side='buy_side',
                    swept_level=lpx, candle_close=float(c[i])))
                break
    return sweeps


def sweep_at(df: pd.DataFrame, idx: int, **kwargs) -> Optional[Sweep]:
    """Did bar at idx sweep liquidity? Return Sweep or None."""
    sweeps = detect_sweeps(df.iloc[:idx + 1], **kwargs)
    return next((s for s in sweeps if s.idx == idx), None)
