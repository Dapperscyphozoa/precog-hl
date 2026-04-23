# Counterfactual Engine — Future Work Backlog

**Logged:** 2026-04-23
**Status:** silent telemetry active, no live impact
**Validates when:** 50+ closes analyzed via `/counterfactual`
**Priority:** Stage 4 (after sizing/ensemble work)

---

## Premise

Other telemetry tells you what happened. Counterfactual tells you
what almost happened. The gap is unrealized edge — actionable in
ways raw WR isn't.

Per closed trade, replay 4 alternatives on the actual 15m bars that
occurred during the hold.

---

## Four Counterfactuals per Trade

### 1. Delayed Entry
- Shift entry +1 bar, +3 bars into future
- Re-run TP/SL sim with fees+slippage
- Delta vs actual P&L = "cost of immediacy"

### 2. Skipped
- Zero P&L — did we avoid a loss or miss a profit?
- Useful for signals firing in fragility zones

### 3. Resized
- 0.5×, 1.5×, 2.0× actual position
- Linear scaling (liq risk ignored at telemetry layer)
- Identifies size-unlock edge for specific configs

### 4. Signal Removed
- Strip one confluence input (RSI filter, regime gate, engine)
- Requires join with signal_logger.jsonl
- Placeholder for now — Stage 5 analysis

---

## Regret Metrics

```
max_regret = best_alternative_pnl - actual_pnl
actual_was_best = (max_regret <= 0)
```

Positive regret = alternative would have outperformed.
Negative regret = actual was optimal.

Aggregated at /counterfactual:
- `actual_was_best_pct` — fraction of trades where actual path was optimal
- `delay_1_better_pct` — fraction where +1 bar delay would improve
- `delay_3_better_pct` — same for +3 bars
- `skip_better_pct` — fraction where skipping would have saved money
- `size_2x_better_pct` — fraction where doubling size gains > losses
- by_regime breakdown — regime-conditional regret

---

## Interpretation Rules at Trigger (50 closes)

### Delay analysis
- If `delay_1_better_pct > 0.45` → market orders chronically entering too early
  → activate 1-bar confirmation wait (becomes timing refinement filter)
- If `delay_1_better_pct < 0.35` → current immediate-entry is optimal
  → no change

### Skip analysis
- If `skip_better_pct > 0.45` → losing signals dominate; gate is leaking
  → investigate conditions where skip outperforms trade
  → could trigger regime-conditional signal filter

### Resize analysis
- If `size_2x_better_pct > 0.60` → system is systematically under-sized
  → cross-validate with convex scorer (may trigger early activation)
- If `size_0.5x_better_pct > 0.55` → system over-sized
  → reduce base risk pct

### Signal-removal analysis (deferred to Stage 5)
- Requires signal_logger parallel data
- "Removing RSI filter: WR unchanged, trade count +20%"
  → RSI filter is dead weight
- "Removing regime gate: WR drops 15pp"
  → regime gate is load-bearing

---

## Current Telemetry

Endpoint: `GET /counterfactual`
Log: `/app/counterfactual.jsonl`

Every close runs 4 counterfactuals in daemon thread:
- Pulls 15m bars around entry (44-bar window)
- Simulates TP/SL at each alternative entry/size
- Computes delta vs actual
- Writes record with regret metrics

---

## Limitations

- **No API calls per counterfactual** — uses fetched bars only
- **Linear resize scaling** — ignores liq risk at large multipliers (fine for telemetry)
- **Signal-removal = placeholder** — needs Stage 5 cross-join with signal_logger
- **Delay ≥ 3 bars may hit max_hold** — some trades too short to meaningfully delay

---

## Revisit Trigger

At 50 closed outcomes:
- Pull `/counterfactual` full stats
- Apply interpretation rules above
- Possibly trigger:
  - 1-bar delay filter activation (timing refinement prerequisite)
  - Resizing adjustment to base risk %
  - Skip-condition filter (what makes skip-better trades different?)

At 200+ closes: signal-removal analysis becomes tractable with
enough signal_logger data to join on (coin, bar_ts).

Until trigger: silent telemetry only.
