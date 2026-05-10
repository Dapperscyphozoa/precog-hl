"""SMC PRIMITIVE — Order Blocks + Supply/Demand Zones.

Order block = the LAST opposing candle before a strong impulsive move.
  Bullish OB = last bearish (red) candle before an impulse up.
  Bearish OB = last bullish (green) candle before an impulse down.

Supply/demand zone = price range covered by the order block candle's
body (or wick if explicit). Zone is "fresh" until price returns
("mitigates" it) by trading back through the zone.

Inputs: HTF dataframe (typically 4H or Daily).
Output: list of zones with status (fresh/mitigated/broken).

Tunables:
  impulse_atr_mult     impulse must be > N×ATR to qualify (default 1.5)
  impulse_lookforward  bars to look forward after candidate OB (default 3)
  zone_use_wicks       if True, zone uses wick range; else body (default False)
"""
from dataclasses import dataclass, field
from typing import List, Optional
import numpy as np
import pandas as pd

from .structure import _atr


@dataclass
class Zone:
    idx: int                  # candle index of the order block
    ts: pd.Timestamp
    side: str                 # 'bullish' (demand) | 'bearish' (supply)
    high: float
    low: float
    mitigated_at: Optional[int] = None
    broken_at: Optional[int] = None

    @property
    def fresh(self) -> bool:
        return self.mitigated_at is None and self.broken_at is None

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0


def detect_order_blocks(df: pd.DataFrame,
                        impulse_atr_mult: float = 1.5,
                        impulse_lookforward: int = 3,
                        zone_use_wicks: bool = False) -> List[Zone]:
    """Find order blocks: last opposing candle before an impulsive move.

    For each candle i, check next `impulse_lookforward` candles. If the
    range traversed exceeds `impulse_atr_mult` × ATR(i), and the candle i
    is the OPPOSITE color to the impulse direction, candle i is an OB.
    """
    n = len(df)
    if n < 30:
        return []

    o = df['Open'].values
    h = df['High'].values
    l = df['Low'].values
    c = df['Close'].values
    atr = _atr(df).values
    ts = df.index

    zones: List[Zone] = []
    for i in range(20, n - impulse_lookforward):
        if np.isnan(atr[i]) or atr[i] <= 0:
            continue
        impulse_thresh = atr[i] * impulse_atr_mult

        # Look at next K bars to measure impulse
        end = min(i + impulse_lookforward + 1, n)
        future_high = h[i + 1:end].max()
        future_low = l[i + 1:end].min()
        impulse_up = future_high - c[i]
        impulse_dn = c[i] - future_low

        is_red = c[i] < o[i]
        is_green = c[i] > o[i]

        if impulse_up >= impulse_thresh and is_red:
            # Bullish OB (last red before impulse up)
            zones.append(Zone(
                idx=i, ts=ts[i], side='bullish',
                high=h[i] if zone_use_wicks else max(o[i], c[i]),
                low=l[i] if zone_use_wicks else min(o[i], c[i]),
            ))
        elif impulse_dn >= impulse_thresh and is_green:
            # Bearish OB
            zones.append(Zone(
                idx=i, ts=ts[i], side='bearish',
                high=h[i] if zone_use_wicks else max(o[i], c[i]),
                low=l[i] if zone_use_wicks else min(o[i], c[i]),
            ))

    # Annotate mitigation/break by walking forward
    for z in zones:
        for j in range(z.idx + impulse_lookforward + 1, n):
            if z.side == 'bullish':
                if l[j] <= z.high and l[j] >= z.low:
                    z.mitigated_at = j; break
                if c[j] < z.low:
                    z.broken_at = j; break
            else:
                if h[j] >= z.low and h[j] <= z.high:
                    z.mitigated_at = j; break
                if c[j] > z.high:
                    z.broken_at = j; break
    return zones


def fresh_zones_at(df: pd.DataFrame, as_of_idx: int,
                   side: Optional[str] = None,
                   **detect_kwargs) -> List[Zone]:
    """Snapshot of UNMITIGATED zones observable at as_of_idx (no lookahead)."""
    sub = df.iloc[:as_of_idx + 1]
    all_zones = detect_order_blocks(sub, **detect_kwargs)
    fresh = [z for z in all_zones if z.fresh and (side is None or z.side == side)]
    return fresh
