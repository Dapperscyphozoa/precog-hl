"""SMC PRIMITIVE — Fair Value Gap (FVG) / Imbalance.

FVG = 3-candle pattern where there's a price range UNTRADED.
  Bullish FVG: candle1.high < candle3.low → gap from c1.high to c3.low
               (price moved up so fast that c2 didn't fill the gap)
  Bearish FVG: candle1.low > candle3.high → gap from c3.high to c1.low

The FVG marks an "imbalance" — the market often returns to fill it
before continuing. Used as an entry zone after MSS confirmation.

Inputs: dataframe.
Output: list of FVGs with status (open/filled).

Tunables:
  min_gap_atr_mult    gap must be > N×ATR to count (default 0 = any gap)
"""
from dataclasses import dataclass
from typing import List, Optional
import numpy as np
import pandas as pd

from .structure import _atr


@dataclass
class FVG:
    idx: int                # index of candle 3 (last in pattern)
    ts: pd.Timestamp
    side: str               # 'bullish' | 'bearish'
    high: float             # top of gap
    low: float              # bottom of gap
    filled_at: Optional[int] = None

    @property
    def open(self) -> bool:
        return self.filled_at is None

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0


def detect_fvgs(df: pd.DataFrame, min_gap_atr_mult: float = 0.0) -> List[FVG]:
    n = len(df)
    if n < 25:
        return []
    h = df['High'].values
    l = df['Low'].values
    atr = _atr(df).values

    fvgs: List[FVG] = []
    for i in range(2, n):
        gap_size = 0.0
        side = None
        gap_high = gap_low = 0.0
        # Bullish: c1.high < c3.low
        if h[i - 2] < l[i]:
            gap_high = float(l[i])
            gap_low = float(h[i - 2])
            gap_size = gap_high - gap_low
            side = 'bullish'
        # Bearish: c1.low > c3.high
        elif l[i - 2] > h[i]:
            gap_high = float(l[i - 2])
            gap_low = float(h[i])
            gap_size = gap_high - gap_low
            side = 'bearish'
        if side is None:
            continue
        if min_gap_atr_mult > 0 and not np.isnan(atr[i]):
            if gap_size < atr[i] * min_gap_atr_mult:
                continue
        fvgs.append(FVG(idx=i, ts=df.index[i], side=side,
                        high=gap_high, low=gap_low))

    # Annotate fill — bullish filled when low touches gap_low; bearish when high touches gap_high
    for f in fvgs:
        for j in range(f.idx + 1, n):
            if f.side == 'bullish' and l[j] <= f.high:
                f.filled_at = j; break
            if f.side == 'bearish' and h[j] >= f.low:
                f.filled_at = j; break
    return fvgs


def open_fvgs_at(df: pd.DataFrame, as_of_idx: int,
                 side: Optional[str] = None,
                 **detect_kwargs) -> List[FVG]:
    """Snapshot of UNFILLED FVGs observable at as_of_idx (no lookahead)."""
    sub = df.iloc[:as_of_idx + 1]
    all_fvgs = detect_fvgs(sub, **detect_kwargs)
    open_ = [f for f in all_fvgs if f.open and (side is None or f.side == side)]
    return open_
