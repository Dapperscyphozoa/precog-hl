"""SMC CONFLUENCE — combines all primitives into a single signal generator.

The Carroll/SMC top-down framework:

  1. KEY ZONE on HTF (4H/Daily)  — unmitigated supply/demand
  2. STRUCTURE on MTF (1H/15m)   — HH/HL for longs, LH/LL for shorts
  3. TRIGGER on LTF (5m/1m)      — sweep → MSS → entry on FVG/OB retest
  4. SL beyond sweep wick. TP at HTF opposing liquidity.

This module evaluates a single instant `as_of_idx` on the LTF bar
and returns a Signal if all conditions align, or None otherwise.

Inputs:
  htf_df:  Higher TF candles (4H or 1D) — for zone detection
  mtf_df:  Mid TF candles (1H or 15m)   — for structure
  ltf_df:  Lower TF candles (5m or 1m)  — for sweep + MSS + entry
  as_of_idx_ltf: index in ltf_df at which to evaluate

Output:
  Signal(symbol, direction, entry, sl, tp, htf_zone, mtf_trend,
         ltf_sweep, ltf_mss, ltf_fvg) or None.

Tunables:
  proximity_bp     LTF entry must be within N bp of a fresh HTF zone
                   midpoint to count (default 50bp)
  rr_target        TP placed at SL_distance * rr_target (default 3.0)
  use_fvg_entry    if True, entry waits for FVG retest after MSS
                   (default True). False = enter at MSS close.
"""
from dataclasses import dataclass
from typing import Optional
import pandas as pd

from .structure import structure_at
from .zones import fresh_zones_at, Zone
from .sweep import sweep_at, Sweep
from .mss import mss_at, MSS
from .fvg import open_fvgs_at, FVG


@dataclass
class Signal:
    ts: pd.Timestamp
    symbol: str
    direction: str          # 'long' | 'short'
    entry: float
    sl: float
    tp: float
    htf_zone: Optional[Zone]
    mtf_trend: str
    ltf_sweep: Optional[Sweep]
    ltf_mss: Optional[MSS]
    ltf_fvg: Optional[FVG]
    rr: float

    def to_dict(self) -> dict:
        return {
            'ts': self.ts.isoformat(),
            'symbol': self.symbol,
            'direction': self.direction,
            'entry': self.entry,
            'sl': self.sl,
            'tp': self.tp,
            'rr': self.rr,
            'mtf_trend': self.mtf_trend,
            'has_zone': self.htf_zone is not None,
            'zone_side': self.htf_zone.side if self.htf_zone else None,
            'has_sweep': self.ltf_sweep is not None,
            'has_mss': self.ltf_mss is not None,
            'has_fvg': self.ltf_fvg is not None,
        }


def _within_zone(price: float, zone: Zone, proximity_pct: float) -> bool:
    """Is price within proximity% of the zone (or inside it)?"""
    midpoint = zone.midpoint
    band = max(zone.high, midpoint * (1 + proximity_pct))
    band_lo = min(zone.low, midpoint * (1 - proximity_pct))
    return band_lo <= price <= band


def generate_signal(symbol: str,
                    htf_df: pd.DataFrame, mtf_df: pd.DataFrame, ltf_df: pd.DataFrame,
                    as_of_idx_ltf: int,
                    proximity_bp: float = 50.0,
                    rr_target: float = 3.0,
                    use_fvg_entry: bool = True,
                    pivot_lookback_mtf: int = 5,
                    pivot_lookback_ltf: int = 3,
                    impulse_atr_mult_htf: float = 1.5,
                    sweep_min_wick_ratio: float = 1.0,
                    ) -> Optional[Signal]:
    """Generate a single signal at as_of_idx_ltf (no lookahead).

    Steps:
      1. Find HTF zones unmitigated as of the timestamp.
      2. Determine MTF trend.
      3. On the LTF bar at as_of_idx_ltf, check for sweep.
      4. If sweep, check for MSS in the opposite direction (at this bar
         or the very next).
      5. If MSS, find an open FVG between sweep extreme and MSS close.
      6. If all align AND price is at/near a fresh HTF zone in the
         right direction, emit Signal.
    """
    if as_of_idx_ltf < 30:
        return None
    ltf_bar = ltf_df.iloc[as_of_idx_ltf]
    ts = ltf_df.index[as_of_idx_ltf]
    proximity_pct = proximity_bp / 10000.0

    # Map timestamp to HTF and MTF index
    try:
        htf_idx = htf_df.index.get_indexer([ts], method='ffill')[0]
        mtf_idx = mtf_df.index.get_indexer([ts], method='ffill')[0]
    except Exception:
        return None
    if htf_idx < 30 or mtf_idx < 30:
        return None

    # 1. HTF zones unmitigated
    htf_zones = fresh_zones_at(htf_df, htf_idx, impulse_atr_mult=impulse_atr_mult_htf)
    if not htf_zones:
        return None

    # 3. LTF sweep at this bar
    sweep = sweep_at(ltf_df, as_of_idx_ltf,
                     pivot_lookback=pivot_lookback_ltf,
                     min_wick_ratio=sweep_min_wick_ratio)
    if sweep is None:
        return None

    # Sweep determines candidate direction
    candidate_dir = 'long' if sweep.side == 'buy_side' else 'short'
    needed_zone_side = 'bullish' if candidate_dir == 'long' else 'bearish'

    # 4. Find HTF zone of correct side that price is near
    eligible = [z for z in htf_zones if z.side == needed_zone_side
                and _within_zone(ltf_bar['Close'], z, proximity_pct)]
    if not eligible:
        return None
    htf_zone = max(eligible, key=lambda z: z.idx)  # most recent

    # 2. MTF structure should be neutral or aligned with the candidate.
    # We allow countertrend setups since SMC is reversal-oriented at zones.
    mtf_state = structure_at(mtf_df, mtf_idx, lookback=pivot_lookback_mtf)
    mtf_trend = mtf_state.trend

    # 5. MSS at this bar OR the next ltf bar
    mss = mss_at(ltf_df, as_of_idx_ltf, pivot_lookback=pivot_lookback_ltf)
    if mss is None or mss.direction != ('up' if candidate_dir == 'long' else 'down'):
        return None

    # 6. Optional FVG entry filter
    fvg_used: Optional[FVG] = None
    if use_fvg_entry:
        fvgs = open_fvgs_at(ltf_df, as_of_idx_ltf,
                            side=('bullish' if candidate_dir == 'long' else 'bearish'))
        # Pick the FVG nearest to entry price that hasn't been filled
        if not fvgs:
            return None
        fvg_used = min(fvgs, key=lambda f: abs(f.midpoint - ltf_bar['Close']))

    # SL beyond sweep wick + small buffer (10% of sweep distance)
    if candidate_dir == 'long':
        sweep_low = ltf_df.iloc[sweep.idx]['Low']
        sl = float(sweep_low) - abs(sweep.swept_level - sweep_low) * 0.10
        entry = float(fvg_used.midpoint) if fvg_used else float(ltf_bar['Close'])
        sl_distance = entry - sl
        if sl_distance <= 0:
            return None
        tp = entry + sl_distance * rr_target
    else:
        sweep_high = ltf_df.iloc[sweep.idx]['High']
        sl = float(sweep_high) + abs(sweep_high - sweep.swept_level) * 0.10
        entry = float(fvg_used.midpoint) if fvg_used else float(ltf_bar['Close'])
        sl_distance = sl - entry
        if sl_distance <= 0:
            return None
        tp = entry - sl_distance * rr_target

    return Signal(
        ts=ts, symbol=symbol, direction=candidate_dir,
        entry=entry, sl=sl, tp=tp,
        htf_zone=htf_zone, mtf_trend=mtf_trend,
        ltf_sweep=sweep, ltf_mss=mss, ltf_fvg=fvg_used,
        rr=rr_target,
    )
