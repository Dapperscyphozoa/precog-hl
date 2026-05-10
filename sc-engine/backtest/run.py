#!/usr/bin/env python3
"""SC-Engine backtest runner.

Usage:
    python3 backtest/run.py                          # default: GC=F + EURUSD=X
    python3 backtest/run.py --symbols GC=F           # gold only
    python3 backtest/run.py --htf 1d --mtf 1h --ltf 15m  # custom timeframes
    python3 backtest/run.py --rr 2.5 --proximity 80  # tune signal params

Pulls data via yfinance (cached to data/<symbol>_<interval>.csv after first fetch).
Runs the backtest, prints metrics, dumps trade log to backtest/results/.

GO/NO-GO threshold (per Carroll plan):
    PF >= 1.5
    Sharpe >= 1.0
    expectancy >= 0.3 R/trade
    n >= 50

Requirements:
    pip install pandas numpy yfinance
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Add parent to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.loader import load_all_tfs
from backtest.engine import run_backtest


GO_NO_GO = {
    'pf':           1.5,
    'sharpe':       1.0,
    'expectancy_R': 0.3,
    'n':            50,
}


def fmt_metric(name: str, value, threshold) -> str:
    if value is None:
        return f'  {name:>14}: N/A'
    if isinstance(value, float):
        v = f'{value:.2f}'
    else:
        v = str(value)
    if threshold is not None:
        ok = '✓' if (isinstance(value, (int, float)) and value >= threshold) else '✗'
        return f'  {name:>14}: {v:<12} (need >= {threshold}) {ok}'
    return f'  {name:>14}: {v}'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--symbols', nargs='+', default=['GC=F', 'EURUSD=X'])
    p.add_argument('--htf', default='1d')
    p.add_argument('--mtf', default='1h')
    p.add_argument('--ltf', default='15m')
    p.add_argument('--rr', type=float, default=3.0)
    p.add_argument('--proximity', type=float, default=200.0,
                   help='LTF proximity to HTF zone in basis points')
    p.add_argument('--fvg', action='store_true',
                   help='Require FVG retest (default: fire at MSS close)')
    p.add_argument('--no-htf-zone', action='store_true',
                   help='Skip HTF zone gating (signals on sweep+MSS only)')
    p.add_argument('--sweep-lookback', type=int, default=30)
    p.add_argument('--mss-window', type=int, default=20)
    p.add_argument('--impulse-atr', type=float, default=1.0)
    p.add_argument('--wick-ratio', type=float, default=0.6)
    p.add_argument('--max-bars-held', type=int, default=200)
    p.add_argument('--cooldown', type=int, default=5)
    p.add_argument('--no-cache', action='store_true')
    p.add_argument('--debug', action='store_true',
                   help='Print rejection-reason counters per market')
    p.add_argument('--invert', action='store_true',
                   help='INVERT signal direction (fade SMC). Real-data testing on '
                        'EURUSD 2021 showed direct SMC = -72R, inverted = +102R.')
    p.add_argument('--out', default=None, help='write JSON results to file')
    args = p.parse_args()

    use_cache = not args.no_cache
    print(f'\nSC-Engine Backtest')
    print(f'  HTF: {args.htf}  MTF: {args.mtf}  LTF: {args.ltf}')
    print(f'  R:R = {args.rr}, proximity = {args.proximity}bp, fvg_entry = {not args.no_fvg}')
    print('=' * 64)

    overall = {}
    for symbol in args.symbols:
        print(f'\n>>> {symbol}')
        try:
            htf_df, mtf_df, ltf_df = load_all_tfs(
                symbol, htf=args.htf, mtf=args.mtf, ltf=args.ltf,
                use_cache=use_cache,
            )
        except Exception as e:
            print(f'  load FAILED: {type(e).__name__}: {e}')
            continue
        print(f'  data: htf={len(htf_df)} mtf={len(mtf_df)} ltf={len(ltf_df)} bars')
        print(f'  range: {ltf_df.index[0]} → {ltf_df.index[-1]}')

        debug_counters = {} if args.debug else None
        result = run_backtest(
            symbol=symbol,
            htf_df=htf_df, mtf_df=mtf_df, ltf_df=ltf_df,
            max_bars_held=args.max_bars_held,
            cooldown_bars=args.cooldown,
            rr_target=args.rr,
            proximity_bp=args.proximity,
            use_fvg_entry=args.fvg,
            require_htf_zone=not args.no_htf_zone,
            sweep_lookback_bars=args.sweep_lookback,
            mss_window_bars=args.mss_window,
            impulse_atr_mult_htf=args.impulse_atr,
            sweep_min_wick_ratio=args.wick_ratio,
            invert_signal=args.invert,
            debug=debug_counters,
        )
        if debug_counters is not None:
            print('  DEBUG (rejection counters):')
            for k, v in sorted(debug_counters.items(), key=lambda kv: -kv[1]):
                print(f'    {k}: {v}')
        m = result.metrics()
        print('  METRICS:')
        print(fmt_metric('n', m.get('n'), GO_NO_GO['n']))
        print(fmt_metric('wr_pct', m.get('wr_pct'), None))
        print(fmt_metric('pf', m.get('pf'), GO_NO_GO['pf']))
        print(fmt_metric('sharpe', m.get('sharpe'), GO_NO_GO['sharpe']))
        print(fmt_metric('expectancy_R', m.get('expectancy_R'), GO_NO_GO['expectancy_R']))
        print(fmt_metric('mdd_R', m.get('mdd_R'), None))
        print(fmt_metric('total_R', m.get('total_R'), None))
        wins = m.get('wins') or 0
        losses = m.get('losses') or 0
        print(f'   wins/losses: {wins}/{losses}')

        # GO/NO-GO verdict
        go = (m.get('pf') or 0) >= GO_NO_GO['pf'] and \
             (m.get('sharpe') or 0) >= GO_NO_GO['sharpe'] and \
             (m.get('expectancy_R') or 0) >= GO_NO_GO['expectancy_R'] and \
             (m.get('n') or 0) >= GO_NO_GO['n']
        print(f'  VERDICT: {"GO ✓" if go else "NO-GO ✗"}')

        overall[symbol] = {
            'metrics': m,
            'go': go,
            'config': result.config,
            'trade_log': [
                {
                    'entry_ts': t.entry_ts.isoformat() if t.entry_ts else None,
                    'exit_ts': t.exit_ts.isoformat() if t.exit_ts else None,
                    'direction': t.direction,
                    'entry': t.entry_price,
                    'exit': t.exit_price,
                    'sl': t.sl, 'tp': t.tp,
                    'pnl_R': t.pnl_R, 'pnl_pct': t.pnl_pct,
                    'reason': t.exit_reason,
                    'bars_held': t.bars_held,
                }
                for t in result.trades
            ],
        }

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, 'w') as f:
            json.dump(overall, f, indent=2, default=str)
        print(f'\nResults written to {args.out}')

    # Exit non-zero if any symbol failed GO
    any_fail = any(not s['go'] for s in overall.values())
    sys.exit(1 if any_fail else 0)


if __name__ == '__main__':
    main()
