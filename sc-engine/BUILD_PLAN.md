# SC ENGINE — BUILD PLAN (adapted)

**Adapted version of the original plan, reconciled with what was actually
built 2026-04-30 → 2026-05-02. Phases 0–4 done in a different shape than
the original plan; this document reflects the real state. Phases 5–8 are
the road ahead.**

---

## CONTEXT (paste this whole block to fresh chat as session bootstrap)

I'm Cyber. I run CP Engine (algorithmic trading) across Hyperliquid (HL).
PRECOG handles HL crypto via profit-engine.js on Render (server
`srv-d6ugg2vdiees73dh8pr0`, SANDEVISTAN). MAIN wallet `0x3eDa...`,
PRECOG wallet `0x2801...`, PRECOG_HL_KEY `0x5842...` (verified owner not
agent). Vault at `/Users/zecvic/Obsidian/Cyber`. I ship without asking
— no "want me to" / "shall I proceed." Execute next steps. Concise
responses, no preamble.

**SC ENGINE = parallel trading system.** Codifies discretionary SMC
into a mechanical algorithm. Separate from PRECOG. Same master wallet,
isolated HL sub-account.

**Status as of 2026-05-02:** backtest framework live in
`precog-hl/sc-engine/` branch `sc-engine-shadow-backtest`. Python.
Data via yfinance. Engine validated, perf-fixed (10k bars in 10s),
9/9 tests passing. **No live execution yet.**

---

## THE FRAMEWORK BEING CODIFIED

Top-down 3-timeframe stack:

1. **HTF (4H/Daily):** Identify "key zone" = unmitigated supply/demand
   or order block where price reacted hard previously
2. **MTF (1H/15m):** Confirm market structure — HH/HL for longs,
   LH/LL for shorts, into the zone
3. **LTF (5m/1m):** Trigger sequence:
   - Liquidity sweep (price spikes past local high/low, snaps back)
   - Market structure shift (MSS) — break of last LTF swing in opposite
     direction
   - Entry on MSS close OR retest of FVG / OB created by the MSS candle
4. **SL:** beyond sweep wick + 10% buffer of sweep distance
5. **TP:** SL distance × R-multiple (default 3R)
6. **Risk:** 1% per trade max, fixed-R

**Edge thesis:** institutions need liquidity to fill big orders.
Stops sit at obvious highs/lows. The sweep IS the institutional fill.
The reversal IS the position being built.

---

## INSTRUMENTS

**Data source for backtest: yfinance** (free, no key, sufficient for the
GO/NO-GO threshold check).

| Asset | yfinance symbol | Live source (later) |
|---|---|---|
| Gold | GC=F (CME futures continuous) | TBD (HL XAUT if liquid; CME GC if going broker route) |
| FX major | EURUSD=X (spot) | TBD (HL FX perp if exists; OANDA if not) |

**Critical open question, blocking Phase 5+:** HL has not been verified
to list EUR/USD perp. Standard FX majors aren't in HL's HIP-3 universe
last checked. **Before any live infrastructure, run:**
```
curl https://api.hyperliquid.xyz/info -d '{"type":"meta"}' | jq '.universe[].name'
```
If EUR/USD is absent, decide: substitute with a different second market
(DXY proxy, JPY/USD, or stay XAUT-only) or move FX to a non-HL venue.

XAUT on HL is plausibly listed as a synthetic perp; verify volume +
spread before relying on it.

---

## ARCHITECTURE — DECIDED

### Wallet isolation: HL sub-account (NOT separate wallet)

```
MAIN wallet 0x3eDa... (cold)
  ├── PRECOG sub-account ($X) — existing, BTC/ETH/SOL only
  └── SC sub-account ($Y) — new, XAUT + (FX or substitute) only
```

True margin isolation. Independent equity curves. Same KYC.

### Symbol segregation rule

SC only trades XAUT (and the second market). PRECOG never touches them.
Hard config flag `ALLOWED_SYMBOLS` per engine.

---

## REPO STRUCTURE (as-built)

```
/precog-hl/                          # existing repo (PRECOG + SC)
├── precog.py                        # PRECOG SA — LOCKED, do not touch
├── confluence_*.py                  # PRECOG SB
└── sc-engine/                       # ← SC ENGINE lives here (Python)
    ├── README.md
    ├── requirements.txt             # pandas, numpy, yfinance
    ├── primitives/
    │   ├── structure.py             # HH/HL/LH/LL pivot detection + trend
    │   ├── zones.py                 # Order block / supply-demand zones
    │   ├── sweep.py                 # Liquidity sweep
    │   ├── mss.py                   # Market structure shift
    │   ├── fvg.py                   # Fair value gap
    │   └── confluence.py            # Combines primitives → Signal
    ├── data/
    │   └── loader.py                # yfinance pull with CSV cache
    ├── backtest/
    │   ├── engine.py                # Walk-forward simulator (O(N), not O(N²))
    │   └── run.py                   # CLI runner with GO/NO-GO check
    └── tests/
        └── test_smoke.py            # 9 end-to-end tests on synthetic OHLC
```

**Branch:** `sc-engine-shadow-backtest` (PR #66). Not merged to main —
experimental. Lives separate from PRECOG main code.

---

## BUILD SEQUENCE

### PHASE 0 — INFRASTRUCTURE (DEFERRED)

**Status: not done.** Plan called for HL sub-account, $50 test capital,
and HL connection verification. Skipped because backtest path doesn't
require live HL. Required before Phase 5 (live execution).

Tasks pending:
- [ ] **VERIFY HL UNIVERSE FIRST.** Confirm XAUT + second market exist.
  If not, pivot the second market.
- [ ] Create HL sub-account for SC, fund $50
- [ ] Verify funding flow MAIN ↔ SC sub
- [ ] Add HL agent key to .env

### PHASE 1 — DATA LAYER (DONE in different shape)

**As-built:** `data/loader.py` pulls yfinance with CSV cache. Yahoo limits:
- 1m → last 7 days
- 5m / 15m → last 60 days
- 1h → last 730 days
- 1d → unlimited

**Sufficient for backtest.** HL data feed deferred to Phase 5+.

### PHASE 2 — SMC PRIMITIVES (DONE)

All five built as standalone Python modules + composed in `confluence.py`:

| Primitive | Module | Tunable params |
|---|---|---|
| Structure (HH/HL/LH/LL) | `primitives/structure.py` | `pivot_lookback`, `atr_filter_mult` |
| Zones (OB / S+D) | `primitives/zones.py` | `impulse_atr_mult`, `impulse_lookforward`, `zone_use_wicks` |
| Sweep | `primitives/sweep.py` | `pivot_lookback`, `min_wick_ratio`, `swing_lookback` |
| MSS | `primitives/mss.py` | `pivot_lookback`, `require_close` |
| FVG | `primitives/fvg.py` | `min_gap_atr_mult` |
| Confluence | `primitives/confluence.py` | All of the above + `proximity_bp`, `sweep_lookback_bars`, `mss_window_bars`, `use_fvg_entry`, `rr_target`, `require_htf_zone` |

**Lookahead-safe** — every function takes `as_of_idx`, only sees data
up to and including that bar. Pivots only confirmed by their lookback
offset. Fast engine pre-computes once and filters by visibility.

### PHASE 3 — PINE PROTOTYPE (DEFERRED)

**Status: not done.** Recommended before Phase 5 live execution. Visual
validation on TradingView XAUUSD chart against the backtest results.
If primitives look wrong on real charts, fix before committing capital.

Pine v5 reference will be added when needed.

### PHASE 4 — BACKTEST (DONE — runner ready, awaiting first real-data run)

**As-built:** `backtest/engine.py` walks LTF data forward, generates
signals via `confluence.py`, simulates fills with realistic slippage
(5bp entry + 5bp exit + 2bp spread default).

**Runner:** `backtest/run.py` — single command produces metrics + GO/NO-GO
verdict for each market.

```bash
cd ~/sc-engine-test && git pull && cd sc-engine && python3 backtest/run.py
```

**GO/NO-GO threshold (Carroll plan):**
- PF ≥ 1.5
- Sharpe ≥ 1.0
- Expectancy ≥ 0.3 R/trade
- Sample n ≥ 50

**CLI flags for tuning:**
```
--rr 3.0                 R-multiple TP
--proximity 200          HTF zone proximity in bp
--sweep-lookback 30      bars to look back for sweep
--mss-window 20          bars after sweep MSS can occur
--impulse-atr 1.0        impulse must be N×ATR
--wick-ratio 0.6         sweep wick:body ratio
--fvg                    require FVG retest (default: fire at MSS close)
--no-htf-zone            skip HTF zone gating (debug mode)
--debug                  print rejection-reason counters per market
```

**Validated on synthetic SMC-pattern data:**
- 10k bars in ~10 seconds (was hanging on O(N²); now O(N))
- 15 signals fired with HTF zone, 86% WR, +2.38R expectancy
- 48 signals fired without HTF zone, 27% WR, +0.07R expectancy
- Slow-engine cross-validated same metrics over 42 minutes

**Synthetic ≠ real. Real-data backtest results will differ.**

**Walk-forward validation: still TODO.** Current runner backtests on full
window. Need to add explicit train/validate split (60/40) and fail GO if
out-of-sample doesn't independently pass threshold.

### PHASE 5 — RISK + EXECUTION (TODO)

Pre-requisite: Phase 4 GO verdict on at least one market.

- [ ] `position_sizer.py` — 1% risk, ATR-scaled SL, contract size calc
- [ ] `caps.py` — max 2 concurrent positions, daily -3% halt, weekly
  -5% halt, $5 minimum equity floor (mirror PRECOG halt logic)
- [ ] `hl_client.py` — sub-account aware order placement
- [ ] `order_manager.py` — entry market/limit, SL/TP brackets, retry
  on slippage, optional trail

### PHASE 6 — PAPER TRADE (TODO)

- [ ] Run engine on HL testnet OR live with $10–50 stake
- [ ] Monitor every signal vs backtest expectation
- [ ] 20–30 paper trades minimum

### PHASE 7 — LIVE SMALL (TODO)

- [ ] $50–100 in SC sub-account
- [ ] Live trades, identical logic to paper
- [ ] Daily review against journal
- [ ] Halt + diagnose any drift between expected and actual fills

### PHASE 8 — DEPLOYMENT + SCALING (TODO)

- [ ] Deploy to Render as `sc-engine` service (NOT webhook-driven,
  scan-loop)
- [ ] State persistence on Render disk
- [ ] **Scale 1.5x every 30 days** (revised down from original 2x)
  conditional on continued GO performance + no drawdown breach
- [ ] Pause-on-DD criteria: -5% MTD = halt scale, -10% MTD = halt trading
- [ ] Add monitoring → Obsidian via existing `sync.py`

---

## TECHNICAL DECISIONS (current)

| Decision | Choice | Reason |
|---|---|---|
| Language | **Python** (was Node.js in original plan) | Faster validation cycle, pandas/numpy ecosystem, matches what's working |
| Location | `precog-hl/sc-engine/` subfolder | Already shipped, no migration cost |
| Data (backtest) | yfinance | Free, sufficient for GO/NO-GO check |
| Data (live) | HL via `@nktkas/hyperliquid` SDK or Python equivalent | TBD Phase 5 |
| Architecture | Scan loop, NOT webhook | Diagnosable, no TV/webhook dependency |
| State | JSON on disk + Render disk | Same as PRECOG |
| Backtest framework | Custom Python | Match production code path; vectorbt later if needed |
| Pine version | v5 | Phase 3 deferred |
| Account isolation | HL sub-account | Margin isolation |
| Risk per trade | 1% fixed-R | Industry standard |
| Max concurrent | 2 positions | XAUT + second, no doubling up |
| Halt logic | -3% day, -5% week, $5 min | Mirror PRECOG safety |
| Scaling | 1.5x / 30 days | Revised from 2x for safety |

---

## DIAGNOSIS SCOPE (mirror PRECOG SA rule)

When debugging SC: engine loaded, scan loop running, HL API succeeding,
state persisting. **Never diagnose via:** webhooks, TV alerts, EA polling,
tickCount, webhooksReceived. Scan-loop architecture only.

---

## OBSIDIAN INTEGRATION (PLANNED)

Auto-sync via existing `sync.py` extension:
- `20-Trading/SC-Engine/state.md` — engine state every 15min
- `20-Trading/SC-Engine/log-YYYY-MM-DD.md` — daily Render log dump
- `20-Trading/Trade-Log/YYYY-MM-DD.md` — fills appended (PRECOG + SC merged)
- `20-Trading/SC-Engine/backtest-results.md` — every backtest run logged

---

## OPEN QUESTIONS (still blocking)

1. **HL EUR/USD perp existence — verify before Phase 5.** If absent,
   pivot second market.
2. **HL XAUT funding mechanics** — does it track LBMA or float independently?
3. **Walk-forward backtest** — current runner backtests full window; add
   explicit train/validate split before declaring GO.
4. **Pine prototype** — visual validation on TradingView before live capital.

---

## STATUS SNAPSHOT (2026-05-02)

| Phase | Status |
|---|---|
| 0 — Infrastructure | DEFERRED (sub-account + HL universe verification) |
| 1 — Data layer | DONE via yfinance (HL TBD Phase 5) |
| 2 — SMC primitives | DONE (Python, 9/9 tests, lookahead-safe) |
| 3 — Pine prototype | DEFERRED (recommended pre-Phase 5) |
| 4 — Backtest | RUNNER READY, validated on synthetic; awaiting first real-data run + walk-forward |
| 5 — Risk + execution | TODO |
| 6 — Paper trade | TODO |
| 7 — Live small | TODO |
| 8 — Deployment + scaling | TODO |

**Next concrete step:** run `backtest/run.py` on real Yahoo data for
Gold + EUR/USD. If GO threshold is hit on either, proceed to walk-forward
split. If both fail, pivot framework or abandon mechanical encoding.

---

## START COMMAND FOR FRESH CHAT

```
git fetch origin sc-engine-shadow-backtest
git checkout sc-engine-shadow-backtest
cd sc-engine
pip install -r requirements.txt
python3 backtest/run.py
```

Result: per-market metrics + GO/NO-GO verdict. Paste output to drive
the next decision.
