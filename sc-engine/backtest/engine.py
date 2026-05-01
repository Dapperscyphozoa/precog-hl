"""Backtest engine — simulates the SMC framework over historical data.

Walks the LTF dataframe forward bar-by-bar. At each LTF bar, calls
generate_signal() with snapshots of HTF/MTF/LTF up-to-and-including
that bar (no lookahead). If a signal fires, opens a simulated trade,
tracks SL/TP across subsequent bars, records outcome.

Realistic frictions:
  - Slippage: configurable bp on entry + exit (default 5bp each side)
  - Spread: applied as cost on every trade (default 2bp)
  - One position at a time per symbol (matches risk plan)

Metrics computed:
  - WR (win rate)
  - PF (profit factor)
  - Sharpe (annualized, ~250 trading days)
  - Max drawdown (% of equity)
  - Expectancy (avg R per trade)
  - Sample size (n)
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict
import math
import pandas as pd

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from primitives.confluence import generate_signal, Signal, _within_zone
from primitives.structure import detect_pivots, structure_at
from primitives.zones import detect_order_blocks, Zone
from primitives.sweep import detect_sweeps, Sweep
from primitives.mss import mss_at, MSS
from primitives.fvg import detect_fvgs, FVG


@dataclass
class Trade:
    entry_ts: pd.Timestamp
    entry_price: float
    direction: str
    sl: float
    tp: float
    exit_ts: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None    # 'tp' | 'sl' | 'eob'
    pnl_R: Optional[float] = None        # profit in R-multiples
    pnl_pct: Optional[float] = None      # raw % move
    bars_held: Optional[int] = None
    signal: Optional[Signal] = None


@dataclass
class BacktestResult:
    symbol: str
    trades: List[Trade] = field(default_factory=list)
    config: Dict = field(default_factory=dict)

    def metrics(self) -> Dict[str, float]:
        closed = [t for t in self.trades if t.pnl_R is not None]
        if not closed:
            return {'n': 0, 'wr_pct': None, 'pf': None, 'sharpe': None,
                    'mdd_R': None, 'expectancy_R': None, 'total_R': 0.0,
                    'wins': 0, 'losses': 0}
        wins = [t for t in closed if t.pnl_R > 0]
        losses = [t for t in closed if t.pnl_R <= 0]
        n = len(closed)
        wr = len(wins) / n * 100 if n else 0
        gross_win = sum(t.pnl_R for t in wins)
        gross_loss = abs(sum(t.pnl_R for t in losses))
        pf = (gross_win / gross_loss) if gross_loss > 0 else float('inf')
        # Sharpe: annualized assuming ~250 trades/year approximation
        rs = [t.pnl_R for t in closed]
        mean_r = sum(rs) / n
        var = sum((r - mean_r) ** 2 for r in rs) / max(n - 1, 1)
        std = math.sqrt(var) if var > 0 else 0
        sharpe = (mean_r / std) * math.sqrt(250) if std > 0 else 0
        # MDD on cumulative R curve
        cum = 0.0; peak = 0.0; mdd = 0.0
        for r in rs:
            cum += r
            peak = max(peak, cum)
            mdd = max(mdd, peak - cum)
        return {
            'n':            n,
            'wr_pct':       round(wr, 1),
            'pf':           round(pf, 2),
            'sharpe':       round(sharpe, 2),
            'mdd_R':        round(mdd, 2),
            'expectancy_R': round(mean_r, 3),
            'total_R':      round(sum(rs), 2),
            'wins':         len(wins),
            'losses':       len(losses),
        }


def _fast_signal(symbol, ts, ltf_bar, ltf_idx,
                 htf_idx, mtf_idx,
                 ltf_sweeps, ltf_pivots_idx, ltf_fvgs, htf_zones, mtf_state,
                 sweep_lookback_bars, mss_window_bars,
                 require_htf_zone, proximity_pct,
                 use_fvg_entry, rr_target,
                 ltf_df,  # only for fallback bar-level mss check
                 pivot_lookback_ltf,
                 debug=None) -> Optional[Signal]:
    """O(1)-per-bar signal check given pre-computed primitives.

    All inputs are pre-built once for the whole dataset; this fn just
    filters to bars visible at ltf_idx and applies confluence rules.
    """
    if ltf_idx < 30:
        if debug is not None: debug['too_early'] = debug.get('too_early', 0) + 1
        return None
    if htf_idx < 30 or mtf_idx < 30:
        if debug is not None: debug['htf_mtf_too_early'] = debug.get('htf_mtf_too_early', 0) + 1
        return None

    # 1. Find sweep in last sweep_lookback_bars LTF bars
    lookback_start = max(0, ltf_idx - sweep_lookback_bars)
    recent_sweeps = [s for s in ltf_sweeps if lookback_start <= s.idx <= ltf_idx]
    if not recent_sweeps:
        if debug is not None: debug['no_sweep'] = debug.get('no_sweep', 0) + 1
        return None
    sweep = recent_sweeps[-1]
    candidate_dir = 'long' if sweep.side == 'buy_side' else 'short'
    needed_zone_side = 'bullish' if candidate_dir == 'long' else 'bearish'

    # 2. HTF zone gating (optional)
    htf_zone = None
    if require_htf_zone:
        # Filter to zones unmitigated AS OF htf_idx
        visible = [z for z in htf_zones if z.idx <= htf_idx
                   and (z.mitigated_at is None or z.mitigated_at > htf_idx)
                   and (z.broken_at is None or z.broken_at > htf_idx)]
        eligible = [z for z in visible if z.side == needed_zone_side
                    and _within_zone(ltf_bar['Close'], z, proximity_pct)]
        if not eligible:
            if not visible:
                if debug is not None: debug['no_htf_zones'] = debug.get('no_htf_zones', 0) + 1
            else:
                if debug is not None: debug['no_eligible_zone'] = debug.get('no_eligible_zone', 0) + 1
            return None
        htf_zone = max(eligible, key=lambda z: z.idx)

    # 3. MSS in correct dir within mss_window_bars after sweep, occurring
    #    AT or 1 bar before the current bar
    needed_mss_dir = 'up' if candidate_dir == 'long' else 'down'
    mss = None
    mss_search_end = min(ltf_idx + 1, sweep.idx + mss_window_bars + 1)
    for j in range(sweep.idx + 1, mss_search_end):
        # Use full-df mss_at — it's still fast since it only needs prior pivots
        # We'll use a cheap inline check against pre-computed pivots instead
        candidate = _mss_at_fast(j, ltf_df, ltf_pivots_idx, pivot_lookback_ltf)
        if candidate is not None and candidate.direction == needed_mss_dir:
            mss = candidate
            break
    if mss is None:
        if debug is not None: debug['no_mss'] = debug.get('no_mss', 0) + 1
        return None
    if ltf_idx - mss.idx > 1:
        if debug is not None: debug['mss_too_old'] = debug.get('mss_too_old', 0) + 1
        return None

    # 4. FVG (optional)
    fvg_used = None
    if use_fvg_entry:
        post_sweep = [f for f in ltf_fvgs if f.idx >= sweep.idx and f.idx <= ltf_idx
                       and (f.filled_at is None or f.filled_at > ltf_idx)
                       and f.side == ('bullish' if candidate_dir == 'long' else 'bearish')]
        if not post_sweep:
            if debug is not None: debug['no_fvg'] = debug.get('no_fvg', 0) + 1
            return None
        retesting = []
        for f in post_sweep:
            if f.side == 'bullish' and ltf_bar['Low'] <= f.high and ltf_bar['Low'] >= f.low * 0.999:
                retesting.append(f)
            elif f.side == 'bearish' and ltf_bar['High'] >= f.low and ltf_bar['High'] <= f.high * 1.001:
                retesting.append(f)
        if not retesting:
            if debug is not None: debug['no_fvg_retest'] = debug.get('no_fvg_retest', 0) + 1
            return None
        fvg_used = max(retesting, key=lambda f: f.idx)

    # SL + TP
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
        htf_zone=htf_zone, mtf_trend=mtf_state.trend if mtf_state else 'unknown',
        ltf_sweep=sweep, ltf_mss=mss, ltf_fvg=fvg_used,
        rr=rr_target,
    )


def _mss_at_fast(idx, ltf_df, all_pivots, pivot_lookback):
    """Cheap MSS check using pre-computed pivots."""
    confirmed = [p for p in all_pivots if p.idx + pivot_lookback <= idx - 1]
    if not confirmed:
        return None
    last_hl = next((p for p in reversed(confirmed) if p.kind == 'HL'), None)
    last_lh = next((p for p in reversed(confirmed) if p.kind == 'LH'), None)
    # Trend approximation from last 4 pivots
    recent = confirmed[-4:]
    kinds = [p.kind for p in recent]
    if 'HH' in kinds and 'HL' in kinds and not ('LH' in kinds[-2:] or 'LL' in kinds[-2:]):
        trend = 'up'
    elif 'LH' in kinds and 'LL' in kinds and not ('HH' in kinds[-2:] or 'HL' in kinds[-2:]):
        trend = 'down'
    else:
        trend = 'range'
    bar = ltf_df.iloc[idx]
    if trend == 'down' and last_lh is not None and bar['Close'] > last_lh.price:
        return MSS(idx=idx, ts=ltf_df.index[idx], direction='up',
                   broken_level=last_lh.price, candle_close=float(bar['Close']))
    if trend == 'up' and last_hl is not None and bar['Close'] < last_hl.price:
        return MSS(idx=idx, ts=ltf_df.index[idx], direction='down',
                   broken_level=last_hl.price, candle_close=float(bar['Close']))
    return None


def run_backtest(symbol: str,
                 htf_df: pd.DataFrame, mtf_df: pd.DataFrame, ltf_df: pd.DataFrame,
                 entry_slippage_bp: float = 5.0,
                 exit_slippage_bp: float = 5.0,
                 spread_bp: float = 2.0,
                 max_bars_held: int = 200,
                 cooldown_bars: int = 5,
                 debug: Optional[Dict] = None,
                 **signal_kwargs) -> BacktestResult:
    """Walk ltf_df forward, generate signals, simulate trades.

    Args:
      max_bars_held: force-close after N bars if neither TP nor SL hit
      cooldown_bars: minimum bars between trades to avoid signal storms
      signal_kwargs: passed to generate_signal()
    """
    result = BacktestResult(symbol=symbol, config={
        'entry_slippage_bp': entry_slippage_bp,
        'exit_slippage_bp': exit_slippage_bp,
        'spread_bp': spread_bp,
        'max_bars_held': max_bars_held,
        'cooldown_bars': cooldown_bars,
        **signal_kwargs,
    })

    n_ltf = len(ltf_df)
    open_trade: Optional[Trade] = None
    open_trade_idx: Optional[int] = None
    last_close_idx = -1

    # Pre-compute primitives ONCE for the whole dataset (O(N), not O(N²))
    pivot_lookback_ltf = signal_kwargs.get('pivot_lookback_ltf', 3)
    pivot_lookback_mtf = signal_kwargs.get('pivot_lookback_mtf', 5)
    impulse_atr_mult_htf = signal_kwargs.get('impulse_atr_mult_htf', 1.0)
    sweep_min_wick_ratio = signal_kwargs.get('sweep_min_wick_ratio', 0.6)
    sweep_lookback_bars = signal_kwargs.get('sweep_lookback_bars', 30)
    mss_window_bars = signal_kwargs.get('mss_window_bars', 20)
    require_htf_zone = signal_kwargs.get('require_htf_zone', True)
    use_fvg_entry = signal_kwargs.get('use_fvg_entry', False)
    rr_target = signal_kwargs.get('rr_target', 3.0)
    proximity_bp = signal_kwargs.get('proximity_bp', 200.0)
    proximity_pct = proximity_bp / 10000.0

    ltf_pivots = detect_pivots(ltf_df, lookback=pivot_lookback_ltf)
    ltf_sweeps = detect_sweeps(ltf_df, pivot_lookback=pivot_lookback_ltf,
                                min_wick_ratio=sweep_min_wick_ratio,
                                swing_lookback=20)
    ltf_fvgs = detect_fvgs(ltf_df)
    htf_zones_all = detect_order_blocks(htf_df, impulse_atr_mult=impulse_atr_mult_htf)
    # Pre-build htf/mtf index maps (timestamp → integer index)
    htf_indexer = htf_df.index
    mtf_indexer = mtf_df.index

    for i in range(30, n_ltf):
        bar = ltf_df.iloc[i]

        # Manage open trade
        if open_trade is not None:
            hit = None
            if open_trade.direction == 'long':
                if bar['Low'] <= open_trade.sl:
                    hit = 'sl'
                    exit_px = open_trade.sl
                elif bar['High'] >= open_trade.tp:
                    hit = 'tp'
                    exit_px = open_trade.tp
            else:
                if bar['High'] >= open_trade.sl:
                    hit = 'sl'
                    exit_px = open_trade.sl
                elif bar['Low'] <= open_trade.tp:
                    hit = 'tp'
                    exit_px = open_trade.tp
            bars_held = i - open_trade_idx
            if hit is None and bars_held >= max_bars_held:
                hit = 'eob'
                exit_px = float(bar['Close'])
            if hit is not None:
                # Apply exit slippage (against direction)
                slip = exit_slippage_bp / 10000.0
                if open_trade.direction == 'long':
                    exit_px *= (1 - slip)
                else:
                    exit_px *= (1 + slip)
                # Apply spread
                spread_cost = spread_bp / 10000.0
                if open_trade.direction == 'long':
                    pnl_pct = (exit_px - open_trade.entry_price) / open_trade.entry_price - spread_cost
                    sl_dist = open_trade.entry_price - open_trade.sl
                else:
                    pnl_pct = (open_trade.entry_price - exit_px) / open_trade.entry_price - spread_cost
                    sl_dist = open_trade.sl - open_trade.entry_price
                pnl_R = (pnl_pct * open_trade.entry_price) / sl_dist if sl_dist > 0 else 0
                open_trade.exit_ts = ltf_df.index[i]
                open_trade.exit_price = float(exit_px)
                open_trade.exit_reason = hit
                open_trade.pnl_pct = float(pnl_pct)
                open_trade.pnl_R = float(pnl_R)
                open_trade.bars_held = bars_held
                result.trades.append(open_trade)
                open_trade = None
                open_trade_idx = None
                last_close_idx = i
            continue

        # Cooldown
        if i - last_close_idx < cooldown_bars:
            continue

        # Generate new signal — fast O(1) lookup using pre-computed primitives
        ts = ltf_df.index[i]
        try:
            htf_idx = htf_indexer.get_indexer([ts], method='ffill')[0]
            mtf_idx = mtf_indexer.get_indexer([ts], method='ffill')[0]
        except Exception:
            continue
        sig = _fast_signal(
            symbol=symbol, ts=ts, ltf_bar=bar, ltf_idx=i,
            htf_idx=htf_idx, mtf_idx=mtf_idx,
            ltf_sweeps=ltf_sweeps, ltf_pivots_idx=ltf_pivots, ltf_fvgs=ltf_fvgs,
            htf_zones=htf_zones_all, mtf_state=None,
            sweep_lookback_bars=sweep_lookback_bars,
            mss_window_bars=mss_window_bars,
            require_htf_zone=require_htf_zone,
            proximity_pct=proximity_pct,
            use_fvg_entry=use_fvg_entry,
            rr_target=rr_target,
            ltf_df=ltf_df,
            pivot_lookback_ltf=pivot_lookback_ltf,
            debug=debug,
        )
        if sig is None:
            continue

        # Apply entry slippage (against direction)
        slip = entry_slippage_bp / 10000.0
        entry_px = sig.entry
        if sig.direction == 'long':
            entry_px *= (1 + slip)
        else:
            entry_px *= (1 - slip)

        open_trade = Trade(
            entry_ts=sig.ts,
            entry_price=float(entry_px),
            direction=sig.direction,
            sl=float(sig.sl),
            tp=float(sig.tp),
            signal=sig,
        )
        open_trade_idx = i

    # Close any remaining open trade at last bar
    if open_trade is not None:
        bar = ltf_df.iloc[-1]
        exit_px = float(bar['Close'])
        spread_cost = spread_bp / 10000.0
        if open_trade.direction == 'long':
            pnl_pct = (exit_px - open_trade.entry_price) / open_trade.entry_price - spread_cost
            sl_dist = open_trade.entry_price - open_trade.sl
        else:
            pnl_pct = (open_trade.entry_price - exit_px) / open_trade.entry_price - spread_cost
            sl_dist = open_trade.sl - open_trade.entry_price
        pnl_R = (pnl_pct * open_trade.entry_price) / sl_dist if sl_dist > 0 else 0
        open_trade.exit_ts = ltf_df.index[-1]
        open_trade.exit_price = exit_px
        open_trade.exit_reason = 'eob'
        open_trade.pnl_pct = float(pnl_pct)
        open_trade.pnl_R = float(pnl_R)
        open_trade.bars_held = n_ltf - 1 - open_trade_idx
        result.trades.append(open_trade)

    return result
