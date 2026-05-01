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
                    proximity_bp: float = 200.0,
                    rr_target: float = 3.0,
                    use_fvg_entry: bool = False,
                    pivot_lookback_mtf: int = 5,
                    pivot_lookback_ltf: int = 3,
                    impulse_atr_mult_htf: float = 1.0,
                    sweep_min_wick_ratio: float = 0.6,
                    sweep_lookback_bars: int = 30,
                    mss_window_bars: int = 20,
                    require_htf_zone: bool = True,
                    debug: Optional[dict] = None,
                    ) -> Optional[Signal]:
    """Permissive SMC confluence — fire on MSS confirmation after sweep.

    Defaults relaxed from Carroll-strict to actually produce signals on
    real data:
      proximity_bp 50 → 200      (2% band around HTF zone, vs 0.5%)
      impulse_atr_mult_htf 1.5 → 1.0  (more zones qualify)
      sweep_min_wick_ratio 1.0 → 0.6  (smaller wicks count as sweeps)
      sweep_lookback_bars 10 → 30
      mss_window_bars 10 → 20
      use_fvg_entry False (default) — fire at MSS close, not FVG retest
      require_htf_zone True — set False to skip HTF zone gating entirely

    Pass debug={} dict; gets counter bumps for each rejection reason
    so you can see why signals are being filtered.
    """
    if as_of_idx_ltf < 30:
        if debug is not None: debug['too_early'] = debug.get('too_early', 0) + 1
        return None
    ltf_bar = ltf_df.iloc[as_of_idx_ltf]
    ts = ltf_df.index[as_of_idx_ltf]
    proximity_pct = proximity_bp / 10000.0

    try:
        htf_idx = htf_df.index.get_indexer([ts], method='ffill')[0]
        mtf_idx = mtf_df.index.get_indexer([ts], method='ffill')[0]
    except Exception:
        if debug is not None: debug['idx_map_err'] = debug.get('idx_map_err', 0) + 1
        return None
    if htf_idx < 30 or mtf_idx < 30:
        if debug is not None: debug['htf_mtf_too_early'] = debug.get('htf_mtf_too_early', 0) + 1
        return None

    # 2. Look back for a sweep in last N bars
    lookback_start = max(0, as_of_idx_ltf - sweep_lookback_bars)
    sweep_window = ltf_df.iloc[:as_of_idx_ltf + 1]
    all_sweeps = detect_sweeps(sweep_window,
                               pivot_lookback=pivot_lookback_ltf,
                               min_wick_ratio=sweep_min_wick_ratio)
    recent_sweeps = [s for s in all_sweeps if s.idx >= lookback_start]
    if not recent_sweeps:
        if debug is not None: debug['no_sweep'] = debug.get('no_sweep', 0) + 1
        return None

    sweep = recent_sweeps[-1]
    candidate_dir = 'long' if sweep.side == 'buy_side' else 'short'
    needed_zone_side = 'bullish' if candidate_dir == 'long' else 'bearish'

    # 3. (Optional) HTF zone gating
    htf_zone: Optional[Zone] = None
    if require_htf_zone:
        htf_zones = fresh_zones_at(htf_df, htf_idx, impulse_atr_mult=impulse_atr_mult_htf)
        if not htf_zones:
            if debug is not None: debug['no_htf_zones'] = debug.get('no_htf_zones', 0) + 1
            return None
        eligible = [z for z in htf_zones if z.side == needed_zone_side
                    and _within_zone(ltf_bar['Close'], z, proximity_pct)]
        if not eligible:
            if debug is not None: debug['no_eligible_zone'] = debug.get('no_eligible_zone', 0) + 1
            return None
        htf_zone = max(eligible, key=lambda z: z.idx)

    # MTF structure (informational, not gating)
    mtf_state = structure_at(mtf_df, mtf_idx, lookback=pivot_lookback_mtf)
    mtf_trend = mtf_state.trend

    # 4. MSS in correct direction within `mss_window_bars` AFTER sweep,
    #    occurring AT or NEAR the current bar.
    needed_mss_dir = 'up' if candidate_dir == 'long' else 'down'
    mss = None
    mss_search_end = min(as_of_idx_ltf + 1, sweep.idx + mss_window_bars + 1)
    for j in range(sweep.idx + 1, mss_search_end):
        candidate_mss = mss_at(ltf_df, j, pivot_lookback=pivot_lookback_ltf)
        if candidate_mss is not None and candidate_mss.direction == needed_mss_dir:
            mss = candidate_mss
            break
    if mss is None:
        if debug is not None: debug['no_mss'] = debug.get('no_mss', 0) + 1
        return None

    # Only fire on the BAR the MSS occurs (or 1 bar after) so we don't
    # repeatedly fire across a multi-bar trending move.
    if as_of_idx_ltf - mss.idx > 1:
        if debug is not None: debug['mss_too_old'] = debug.get('mss_too_old', 0) + 1
        return None

    # 5. (Optional) FVG entry filter — wait for FVG retest instead of
    #    firing at MSS close.
    fvg_used: Optional[FVG] = None
    if use_fvg_entry:
        all_fvgs = open_fvgs_at(
            ltf_df, as_of_idx_ltf,
            side=('bullish' if candidate_dir == 'long' else 'bearish'),
        )
        post_sweep_fvgs = [f for f in all_fvgs if f.idx >= sweep.idx]
        if not post_sweep_fvgs:
            if debug is not None: debug['no_fvg'] = debug.get('no_fvg', 0) + 1
            return None
        retesting = []
        for f in post_sweep_fvgs:
            if f.side == 'bullish' and ltf_bar['Low'] <= f.high and ltf_bar['Low'] >= f.low * 0.999:
                retesting.append(f)
            elif f.side == 'bearish' and ltf_bar['High'] >= f.low and ltf_bar['High'] <= f.high * 1.001:
                retesting.append(f)
        if not retesting:
            if debug is not None: debug['no_fvg_retest'] = debug.get('no_fvg_retest', 0) + 1
            return None
        fvg_used = max(retesting, key=lambda f: f.idx)

    # SL beyond sweep wick + small buffer
    if candidate_dir == 'long':
        sweep_low = ltf_df.iloc[sweep.idx]['Low']
        sl = float(sweep_low) - abs(sweep.swept_level - sweep_low) * 0.10
        entry = float(fvg_used.midpoint) if fvg_used else float(ltf_bar['Close'])
        sl_distance = entry - sl
        if sl_distance <= 0:
            if debug is not None: debug['bad_sl'] = debug.get('bad_sl', 0) + 1
            return None
        tp = entry + sl_distance * rr_target
    else:
        sweep_high = ltf_df.iloc[sweep.idx]['High']
        sl = float(sweep_high) + abs(sweep_high - sweep.swept_level) * 0.10
        entry = float(fvg_used.midpoint) if fvg_used else float(ltf_bar['Close'])
        sl_distance = sl - entry
        if sl_distance <= 0:
            if debug is not None: debug['bad_sl'] = debug.get('bad_sl', 0) + 1
            return None
        tp = entry - sl_distance * rr_target

    if debug is not None: debug['signals'] = debug.get('signals', 0) + 1
    return Signal(
        ts=ts, symbol=symbol, direction=candidate_dir,
        entry=entry, sl=sl, tp=tp,
        htf_zone=htf_zone, mtf_trend=mtf_trend,
        ltf_sweep=sweep, ltf_mss=mss, ltf_fvg=fvg_used,
        rr=rr_target,
    )
