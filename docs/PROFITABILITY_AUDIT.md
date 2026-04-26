# Profitability Audit — claude/bot-profitability-analysis-6NqTg

Static review of the precog-hl bot. No live PnL data was accessible from this environment (state lives on Render at `/var/data`); findings are from code only.

## TL;DR

The bot has a sophisticated signal stack, but profitability is being eroded by **three open-loop leaks** and **one execution-cost blind spot**:

1. **No minimum-edge gate.** Trades fire even when friction eats most of the gross edge.
2. **Funding cost is never accrued to position PnL.** Holds >1h leak unmodeled cost that exceeds the entire fee budget.
3. **Per-engine PnL is computed but nothing consumes it.** Broken engines keep firing at full conviction.
4. **`convex_scorer` is fully built but telemetry-only.** A real signal (R:R asymmetry × tail-win) is logged and ignored.

Fix order, by expected impact / effort ratio: **#1 → #3 → #4 → #2**.

---

## Critical leaks

### L1 — No minimum-edge gate (CRITICAL)
**Where:** `gates.py:182-211`, `confluence_worker.py:_size_and_fire` line 195-256.
**Evidence:** `grep -rn "min_edge\|MIN_EDGE\|net_expected\|rr_min"` returns zero hits in any signal/gate file.

A signal can pass all 9 gates, all 6 confluence filters, and still have:
- `tp_pct = 0.5%`, `sl_pct = 0.25%` (2:1 R:R)
- Modeled friction = 0.09% fees + 0.16% slippage = **0.23% round-trip**
- Net expected = 0.5% − 0.23% = **0.27%** vs −0.48% loss → real R:R ≈ **1.18:1**, not 2:1

Worse, exit market-fill slippage on volatile bars is unmodeled, so realized friction is higher than 0.23%.

**Fix:**
```python
# gates.py — new gate after gate 8
def gate_min_edge(tp_pct, sl_pct, fee_rt=0.0009, slip_rt=0.0016, min_net_rr=1.5):
    net_tp = tp_pct - (fee_rt + slip_rt)
    net_sl = sl_pct + (fee_rt + slip_rt)
    if net_tp <= 0:
        return False, f"net_tp {net_tp:.4f} <= 0 (friction wipes profit)"
    if (net_tp / net_sl) < min_net_rr:
        return False, f"net_rr {net_tp/net_sl:.2f} < {min_net_rr}"
    return True, "ok"
```
Wire into the gate stack and into `confluence_worker._size_and_fire` before order send. Track rejects in shadow_trades to validate.

---

### L2 — Funding cost not accrued (CRITICAL)
**Where:** searched `cumulative_funding|funding_accrued|funding_cost` → zero hits.
**`funding_arb.py`** reads HL/Binance/OKX rates only for *signaling*. **`funding_engine.py`** signals reversal trades. Neither reduces realized PnL on held positions.

At +0.02%/hr typical funding, a 6h hold pays **0.12%** — **larger than the entire 0.09% fee budget**. On a 1.5% TP, that's 8% of gross PnL silently lost. Long swing-style holds are the most exposed.

**Fix:**
```python
# trade_ledger.py — extend close path
funding_paid_pct = compute_funding_for_hold(coin, side, entry_ts, close_ts)
net_pnl = gross_pnl - 2*FEE - funding_paid_pct
```
Plus a hold-time gate: if expected hold > 4h, require larger TP to absorb funding. Source funding from existing `funding_arb` cache — the data is already being fetched.

---

### L3 — Per-engine PnL is open-loop (HIGH)
**Where:** `precog.py:766, 779, 2027, 3851` (computed); commit `d19018b` just unblocked engine carry-through.
**Evidence:** `grep` for any consumer of `by_engine` shows **only display** in `/stats` and `/confluence` endpoints. No code disables an engine based on its PnL.

If `WALL_ABSORB` had −5% realized over its last 50 fills, the system still fires it at full size. `edge_decay.py` watches *aggregate* drift, not per-engine.

**Fix:** Mirror `coin_killswitch.py` for engines:
```python
# engine_killswitch.py (new)
# Disable if last 30 trades for engine X: pnl_sum < -3% OR wr < 35% OR consec_losses >= 5.
# Check is_disabled(engine) in confluence_worker._size_and_fire before placing order.
```
This is exactly the kind of feedback loop the recent commits were prepping for. Closing the loop is the lowest-effort win in this audit.

---

### L4 — `convex_scorer` built but unused (HIGH)
**Where:** `convex_scorer.py:8` — `"NO LIVE SIZING IMPACT until explicitly activated"`.
The score (R:R × tail-win % / variance) is logged to `/app/convex_scores.jsonl`. Tail-win tracking is live (`_TAIL_STATS`).

This is the highest-quality unused signal in the stack. Activating it as a sizing modifier (capped, e.g., 0.7×–1.3×) on engines/coins that have ≥100 logged outcomes is far cheaper than trying to build a new alpha.

**Fix:** Read the trigger guard at `convex_scorer.py:202`, set `MIN_OUTCOMES_FOR_LIVE = 100`, and have `_size_and_fire` multiply notional by `convex_scorer.size_mult(coin, engine)`. Bound the multiplier and shadow-test for 1 week before unbounding.

---

## Medium leaks

### M1 — Fee constant is wrong, maker rebate ignored
- `shadow_trades.py:99` → `FEE_ROUND_TRIP = 0.0007` should be **0.0009** (HL taker is 4.5 bps, not 3.5 bps)
- `tuner_worker.py:127` → `FEE = 0.00045` is correct (one-way) but assumes 100% taker
- **Maker rebate (−0.001%)** is nowhere. If you fill SL/TP as maker, your shadow PnL is pessimistic by ~0.02%/trade; if you fill entries as maker, it's optimistic about overall taker share. Either way: tracking is wrong.

**Fix:** Add `realized_fee` to trade ledger (record what HL actually charged). Stop using a constant.

### M2 — SL/TP market-fill slippage not modeled
`atomic_entry.py:68` has `DEFAULT_SLIP = 0.005` for entry IOC, but SL/TP triggers fill at market with no slippage budget. On fast moves slippage can be 20–50 bps — invisible to backtest.

**Fix:** Capture fill price vs trigger price in `enforce_protection.py` close path and surface as `realized_slippage` per trade.

### M3 — `coin_killswitch` uses default expected WR = 75
`coin_killswitch.py:47` defaults `expected_wr_pct=75`. README claims **80.2% WR** for the locked strategy. With drop trigger of 20pp, current threshold = 55% WR. Should be **60.2%**.

**Fix:** Pass actual per-coin expected WR from `all_grid_results.json` instead of the constant.

### M4 — Backtest gates regenerated only on deploy
`all_ticker_gates.json`, `all_grid_results.json` mtime = deploy time. No cron / scheduler regenerates them. After ~7-14 days, they're trading dead edge if regimes shifted.

**Fix:** Wire `tuner_worker` to re-run weekly and atomically swap gate files. Log the diff so you see what changed.

### M5 — BE buffer 0.002 is *barely* above friction
Recent commit `3279776` raised buffer 0 → 0.002 (20 bps). Round-trip friction on price terms is ~12.5 bps. Margin = 7.5 bps. One bad slip = SL hit at a "lock" trade.

**Fix:** Raise to **0.003** (30 bps) — still small in margin terms (~4.5% on 15× leverage) but doubles the safety margin against slippage.

---

## Low / cleanup

- **`tier_killswitch.py`** tracks tiers (`PURE`, `NINETY_99`, …) that don't appear in the current Confluence/Wall/Funding signal flow. Likely legacy from a previous architecture. Verify it's still wired or delete.
- **`ensemble_voter.py`** has top-K voting logic but isn't called by `confluence_worker`. Either wire it or remove.
- **`forward_walk.py`** is offline-only telemetry. If you trust it, the natural next step is to gate live signals by tier WR from it.

---

## What I cannot tell you from static review

- Actual realized PnL by engine, coin, hour, regime
- Live taker/maker fill ratio
- Realized slippage distribution
- Whether `WALL_ABSORB` / `FUNDING_MR` / `WALL_EXH` are net-positive or net-negative

Pull `/var/data/trades.csv` and `convex_scores.jsonl` off Render and run the breakdown locally. The recent `engine` carry-through (commit `d19018b`) makes that breakdown possible for the first time.

---

## Suggested first sprint (ordered)

1. Add `gate_min_edge` (L1). Shadow-only for 24h, then live.
2. Add `engine_killswitch` (L3). Threshold conservative at first.
3. Pull last 7 days of trades off Render, break down by engine, regime, hour. Decide which engines stay on.
4. Activate `convex_scorer` size multiplier (L4) for engines with ≥100 outcomes, bounded 0.7-1.3×.
5. Add funding accrual to `trade_ledger` close path (L2). Add expected-funding-cost gate for holds >4h.

Items 1-2 are <50 LOC each and address the largest expected leaks.
