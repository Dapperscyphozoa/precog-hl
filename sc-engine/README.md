# SC-Engine — SMC Backtest Framework

Mechanical encoding of Carroll/SMC top-down framework for Gold (XAU) +
EUR/USD on free Yahoo Finance data. **Backtest-only**. No live trading
yet — that's Phase 6+.

## What this does

Walks historical OHLC data bar-by-bar applying the framework:

```
1. KEY ZONE on HTF (4H/Daily) — unmitigated supply/demand / order block
2. STRUCTURE on MTF (1H/15m)  — HH/HL for longs, LH/LL for shorts
3. TRIGGER on LTF (5m/1m)     — sweep → MSS → entry on FVG retest
4. SL beyond sweep wick. TP at SL × 3R (default).
```

Simulates trades with realistic slippage + spread, computes WR / PF /
Sharpe / MDD / Expectancy. Compares against the GO/NO-GO threshold:

| Metric | Pass |
|---|---|
| PF | ≥ 1.5 |
| Sharpe | ≥ 1.0 |
| Expectancy R | ≥ 0.3 |
| Sample size | n ≥ 50 |

If a market doesn't pass on out-of-sample data after 1 round of tuning,
the framework is rejected for that market — you don't fight the
backtest by overfitting.

## Quickstart

```bash
cd sc-engine
pip install -r requirements.txt

# Run with defaults (Gold + EUR/USD, daily HTF, 1h MTF, 15m LTF)
python3 backtest/run.py

# Single symbol
python3 backtest/run.py --symbols GC=F

# Different timeframes (yfinance limits: 15m → 60d, 1h → 730d, 1d → unlimited)
python3 backtest/run.py --htf 1d --mtf 1h --ltf 15m

# Tighter R:R, wider zone proximity
python3 backtest/run.py --rr 2.5 --proximity 80

# Skip FVG entry filter (entry at MSS close instead)
python3 backtest/run.py --no-fvg

# Save trade-by-trade results
python3 backtest/run.py --out backtest/results/run_001.json
```

## File map

```
sc-engine/
├── primitives/
│   ├── structure.py      # HH/HL/LH/LL pivot detection + trend label
│   ├── zones.py          # Order block / supply/demand zone detection
│   ├── sweep.py          # Liquidity sweep detection (wick out, close back)
│   ├── mss.py            # Market structure shift / break of structure
│   ├── fvg.py            # Fair value gap / imbalance detection
│   └── confluence.py     # HTF zone + MTF trend + LTF sweep+MSS+FVG → Signal
├── data/
│   └── loader.py         # yfinance pull with CSV cache
├── backtest/
│   ├── engine.py         # Walk-forward backtest with slippage / spread
│   └── run.py            # CLI runner — prints metrics, writes JSON
├── tests/
│   └── test_smoke.py     # End-to-end pipeline tests on synthetic OHLC
└── README.md
```

## Tunable parameters

Every primitive exposes its hyperparameters. Defaults are conservative
starting points — re-tune via `signal_kwargs` in `run_backtest()` or
the CLI flags above.

| Param | Default | Where | Effect |
|---|---|---|---|
| `pivot_lookback_mtf` | 5 | confluence | Bars on each side to confirm a swing pivot |
| `pivot_lookback_ltf` | 3 | confluence | Faster pivots for sweep detection |
| `impulse_atr_mult_htf` | 1.5 | zones | Impulse must be ≥ N×ATR to make a zone |
| `sweep_min_wick_ratio` | 1.0 | sweep | Wick must be ≥ N×body for sweep label |
| `proximity_bp` | 50 | confluence | LTF must be within N bp of HTF zone |
| `rr_target` | 3.0 | confluence | TP at SL distance × R |
| `use_fvg_entry` | True | confluence | Entry at FVG midpoint (vs MSS close) |

## Walk-forward validation (recommended)

Don't tune all your params on the same data window you grade.

```bash
# Pull fresh data
rm -f data/*.csv
python3 backtest/run.py --htf 1d --mtf 1h --ltf 15m \
  --out backtest/results/in_sample.json

# Manually slice 60% / 40% in your head:
#   In-sample: first 60% of date range — used for tuning
#   Out-sample: last 40% — must independently pass GO threshold
# (Walk-forward split logic to be added — for now, run on different
#  date ranges manually.)
```

If in-sample passes GO but out-of-sample fails: framework is overfit.
**Don't ship.** Reduce primitives or move on.

## Lookahead-safety

Every primitive function takes `as_of_idx` and only considers data up
to and including that bar. Pivots are only "confirmed" by their
lookback offset (a pivot at bar P is only visible at bar P+5 with
default `lookback=5`). This prevents the most common backtest fraud.

The backtest engine walks bars forward and constructs HTF/MTF
snapshots up to each LTF bar — no future data leaks into signals.

## What this DOESN'T do

- Live trading (Phase 6+ — separate engine)
- Multi-position management (one trade at a time per symbol)
- Position sizing (assumes 1R per trade — sizing is a wrapper above this)
- Walk-forward auto-split (run manually for now)
- Pine prototype (separate file — see Phase 3 of plan)

## Next steps once backtest passes GO

1. Pine prototype on TradingView for visual validation against real chart
2. Paper trade on HL testnet for 20-30 trades
3. Live small ($50-100) with identical logic
4. Scale 1.5x every 30 days conditional on continued GO performance

## Tests

```bash
cd sc-engine
python3 -m unittest tests.test_smoke -v
```

9 end-to-end tests on synthetic OHLC. Pass = pipeline runs cleanly,
no lookahead leak, metrics shape is correct. Does NOT validate that
signals are +EV — that's what the real-data backtest is for.
