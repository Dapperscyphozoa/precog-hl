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
from primitives.confluence import generate_signal, Signal


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


def run_backtest(symbol: str,
                 htf_df: pd.DataFrame, mtf_df: pd.DataFrame, ltf_df: pd.DataFrame,
                 entry_slippage_bp: float = 5.0,
                 exit_slippage_bp: float = 5.0,
                 spread_bp: float = 2.0,
                 max_bars_held: int = 200,
                 cooldown_bars: int = 5,
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

        # Generate new signal
        sig = generate_signal(symbol, htf_df, mtf_df, ltf_df, i, **signal_kwargs)
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
