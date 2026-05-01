"""SMC PRIMITIVE — Market Structure Shift (MSS) / Break of Structure (BOS).

MSS = the LAST swing in the prior trend gets broken in the OPPOSITE
direction. It signals trend change from up→down or down→up.

Examples:
  - Trend was DOWN (LH/LL sequence). Price breaks the most recent LH.
    → MSS up (bullish reversal trigger).
  - Trend was UP (HH/HL). Price breaks the most recent HL.
    → MSS down (bearish reversal trigger).

Inputs: dataframe + bar index to evaluate "did MSS just happen at i?"
Output: MSS direction or None.

Tunables:
  pivot_lookback     same as structure (default 5)
  require_close      if True, MSS only counts on candle CLOSE through level
                     (default True — wick-only is too weak)
"""
from dataclasses import dataclass
from typing import Optional
import pandas as pd

from .structure import detect_pivots, structure_at


@dataclass
class MSS:
    idx: int
    ts: pd.Timestamp
    direction: str          # 'up' (bullish reversal) | 'down' (bearish reversal)
    broken_level: float
    candle_close: float


def mss_at(df: pd.DataFrame, idx: int,
           pivot_lookback: int = 5,
           require_close: bool = True) -> Optional[MSS]:
    """Did an MSS occur at bar idx?

    Logic:
      - Determine prior trend from structure as of idx-1.
      - If trend was DOWN: did bar i CLOSE above the most recent LH? → MSS up
      - If trend was UP:   did bar i CLOSE below the most recent HL? → MSS down
    """
    if idx < pivot_lookback * 2 + 5:
        return None
    state = structure_at(df, idx - 1, lookback=pivot_lookback)
    bar = df.iloc[idx]
    px = bar['Close'] if require_close else bar['High']

    if state.trend == 'down' and state.last_lh is not None:
        if (bar['Close'] > state.last_lh.price if require_close
                else bar['High'] > state.last_lh.price):
            return MSS(idx=idx, ts=df.index[idx], direction='up',
                       broken_level=state.last_lh.price,
                       candle_close=float(bar['Close']))
    elif state.trend == 'up' and state.last_hl is not None:
        if (bar['Close'] < state.last_hl.price if require_close
                else bar['Low'] < state.last_hl.price):
            return MSS(idx=idx, ts=df.index[idx], direction='down',
                       broken_level=state.last_hl.price,
                       candle_close=float(bar['Close']))
    return None
