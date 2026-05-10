"""SMC PRIMITIVE — Market Structure (Higher Highs / Lower Lows).

Detects swing pivots and labels current trend as up / down / range based
on the sequence of HH/HL/LH/LL.

Inputs: OHLC dataframe with index as datetime.
Output: per-bar trend label + most recent swing points.

Tunables:
  pivot_lookback     N bars on each side to confirm a pivot (default 5)
  atr_filter_mult    pivot must extend > N×ATR from prior pivot to count
                     (default 0 = off; set 0.5 to filter micro-pivots)
"""
from dataclasses import dataclass
from typing import Optional, List, Tuple
import numpy as np
import pandas as pd


@dataclass
class Pivot:
    idx: int
    ts: pd.Timestamp
    price: float
    kind: str   # 'HH' | 'HL' | 'LH' | 'LL'


@dataclass
class StructureState:
    trend: str          # 'up' | 'down' | 'range'
    last_hh: Optional[Pivot]
    last_hl: Optional[Pivot]
    last_lh: Optional[Pivot]
    last_ll: Optional[Pivot]
    pivots: List[Pivot]


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df['High'], df['Low'], df['Close']
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def detect_pivots(df: pd.DataFrame, lookback: int = 5,
                  atr_filter_mult: float = 0.0) -> List[Pivot]:
    """Detect swing highs and lows using N-bar fractal definition.

    A pivot high at index i = high[i] is the max of high[i-N..i+N].
    A pivot low at index i = low[i] is the min of low[i-N..i+N].

    If atr_filter_mult > 0, the pivot must extend > mult*ATR from the
    previous opposing pivot to be recorded (filters noise pivots).
    """
    n = len(df)
    if n < 2 * lookback + 1:
        return []
    highs = df['High'].values
    lows = df['Low'].values
    atr = _atr(df).values if atr_filter_mult > 0 else None

    raw_pivots: List[Tuple[int, float, str]] = []
    for i in range(lookback, n - lookback):
        window_hi = highs[i - lookback:i + lookback + 1]
        window_lo = lows[i - lookback:i + lookback + 1]
        if highs[i] == window_hi.max() and (window_hi == highs[i]).sum() == 1:
            raw_pivots.append((i, float(highs[i]), 'high'))
        elif lows[i] == window_lo.min() and (window_lo == lows[i]).sum() == 1:
            raw_pivots.append((i, float(lows[i]), 'low'))

    # Apply ATR filter and label HH/HL/LH/LL
    pivots: List[Pivot] = []
    last_high_price: Optional[float] = None
    last_low_price: Optional[float] = None
    for idx, price, kind in raw_pivots:
        if atr is not None and not np.isnan(atr[idx]):
            min_extension = atr[idx] * atr_filter_mult
        else:
            min_extension = 0.0

        if kind == 'high':
            if last_high_price is None:
                label = 'HH'  # first high — assume HH (trend bias upward by default)
            elif price > last_high_price + min_extension:
                label = 'HH'
            elif price < last_high_price - min_extension:
                label = 'LH'
            else:
                continue  # noise — skip
            last_high_price = price
        else:
            if last_low_price is None:
                label = 'HL'
            elif price > last_low_price + min_extension:
                label = 'HL'
            elif price < last_low_price - min_extension:
                label = 'LL'
            else:
                continue
            last_low_price = price

        pivots.append(Pivot(idx=idx, ts=df.index[idx], price=price, kind=label))

    return pivots


def structure_at(df: pd.DataFrame, as_of_idx: int,
                 lookback: int = 5, atr_filter_mult: float = 0.0) -> StructureState:
    """Snapshot of structure state as observable at as_of_idx (avoiding lookahead).

    Only uses pivots that would have been confirmed by bar as_of_idx.
    A pivot at index p is only confirmed by index p+lookback.
    """
    sub = df.iloc[:as_of_idx + 1]
    all_pivots = detect_pivots(sub, lookback=lookback, atr_filter_mult=atr_filter_mult)
    # Only use pivots whose confirmation bar (p.idx + lookback) <= as_of_idx
    confirmed = [p for p in all_pivots if p.idx + lookback <= as_of_idx]

    last_hh = next((p for p in reversed(confirmed) if p.kind == 'HH'), None)
    last_hl = next((p for p in reversed(confirmed) if p.kind == 'HL'), None)
    last_lh = next((p for p in reversed(confirmed) if p.kind == 'LH'), None)
    last_ll = next((p for p in reversed(confirmed) if p.kind == 'LL'), None)

    # Trend logic: most recent two pivots determine direction
    recent = confirmed[-4:] if len(confirmed) >= 4 else confirmed
    kinds = [p.kind for p in recent]
    if 'HH' in kinds and 'HL' in kinds and not ('LH' in kinds[-2:] or 'LL' in kinds[-2:]):
        trend = 'up'
    elif 'LH' in kinds and 'LL' in kinds and not ('HH' in kinds[-2:] or 'HL' in kinds[-2:]):
        trend = 'down'
    else:
        trend = 'range'

    return StructureState(trend=trend, last_hh=last_hh, last_hl=last_hl,
                          last_lh=last_lh, last_ll=last_ll, pivots=confirmed)
