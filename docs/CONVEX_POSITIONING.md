# Convex Positioning — Future Work Backlog

**Logged:** 2026-04-23
**Status:** a priori framework; silent scorer deployed, no live sizing impact
**Validates when:** 100+ closes analyzed via `/convex` endpoint
**Priority:** Stage 4+ (after sizing/ensemble/regime-detector work)

---

## Premise

Not all +EV trades have the same payoff shape.
62% WR with R:R 3.0 = asymmetric (occasional large wins).
83% WR with R:R 1.5 = linear (steady small wins).
Same EV, radically different Kelly-optimal sizing.

Current PreCog uses fixed 0.5% risk per trade. This leaves edge on the table
by treating convex and linear trades identically.

---

## Convexity Score (0-1)

```
convexity = (R:R × tail_win_pct) / max(variance_cost + fee_drag, 0.01)

R:R           = TP/SL ratio (enterprise grid: 2.0-3.3)
tail_win_pct  = fraction of wins that exceeded TP by >=20%
variance_cost = σ of trade PnL (per coin+engine)
fee_drag      = 0.0023 (0.23% roundtrip)
```

Sigmoid-normalized to 0-1. Thresholds tuned so:
- raw 6 → ~0.5 (moderate)
- raw 12 → ~0.8 (strong)

---

## Size Multiplier (WHEN ACTIVATED)

| Score | Classification | Multiplier |
|---|---|---|
| 0.80-1.00 | Strong convex | **1.5x** |
| 0.50-0.80 | Mild convex | 1.0x |
| 0.20-0.50 | Linear | 0.7x |
| <0.20 | Concave | 0.3x or skip |

Final position size (when live):
```
size = base_risk(0.5%) × tier_mult × convexity_mult × wilson_lb_mult
```

Hard caps:
- No single trade > 1.0% risk, ever (2× base)
- Total convex-sized exposure ≤ 8% equity at any moment
- 5 consecutive convex-sized losses → revert to uniform 0.5% for 24h

---

## Estimated Impact (priors)

- Gross edge per trade: **+0.15R**
- Variance: +5% (acceptable skew — bigger wins, similar losses)
- Max drawdown: unchanged (hard-capped per-trade)
- Sharpe: +0.3 improvement modeled

---

## Current Telemetry

Deployed silent scorer writes to `/app/convex_scores.jsonl`.
Endpoint: `GET /convex`

Records at signal fire:
- coin, side, engine
- tp_pct, sl_pct, rr
- tail_win_pct (from historical wins of this coin+engine)
- variance_cost (from σ of historical outcomes)
- normalized score (0-1)
- size_multiplier_if_activated (not applied)
- actual_size (current uniform 0.5%)

Records at close:
- coin, engine, pnl_pct, win
- tail_win (True if MFE exceeded TP by >=20%)
- mfe_pct (peak favorable excursion if tracked)

Updates in-memory tail-stats cache per (coin, engine).

---

## Trigger Analysis at 100 Outcomes

When `/convex` shows `trigger_fired: true`:

1. Compare WR + avg_pnl across convexity buckets:
   - convex (0.80+)
   - mild (0.50-0.80)
   - linear (0.20-0.50)
   - concave (<0.20)

2. Decision rule:
   - If convex bucket outperforms linear by >=0.3R per trade → **ACTIVATE**
   - If within 0.1R (no meaningful difference) → **ABANDON** (framework was wrong)
   - If linear outperforms convex → **INVERT** (rare, investigate data quality)

3. On activation:
   - Apply size_multiplier at signal fire (patch into size calc in precog.py)
   - Monitor first 50 live-sized trades for degradation
   - If degrades below baseline, revert

---

## Data Quality Dependencies

- **MFE tracking** — current precog.py doesn't track max_favorable_excursion_pct.
  Fallback: uses pnl_pct > 0 as tail-win proxy (overestimates tail rate).
  TODO: add MFE tracking to position monitor loop (15m resolution is fine).

- **Per-coin+engine sample size** — need 5+ wins per (coin, engine) pair
  before tail_win_pct deviates from default 0.25. At current 30-50 trades/day,
  expect per-pair sample size to matter after week 1.

- **Variance cost stability** — σ computed from last 500 log entries.
  Thin coverage for new coins; degrades gracefully via default 0.025.

---

## Why It Works Here (if it works)

Enterprise grid already filtered for +EV. Convexity adds a second dimension:
among +EV trades, which have the best payoff shape.

UNI at R:R 3.0 isn't just "better than flat" — it's asymmetric in a way that
rewards size. Conversely, tight-TP coins (YZY 3%, TRX 3%) at R:R 2.0 are linear:
adding size doesn't improve expected outcome much, but increases variance cost.

If the hypothesis holds, post-activation:
- Capital flows preferentially to convex setups (UNI, ZRO, LAYER, TNSR)
- Linear setups (XRP, HBAR, tight-TP alts) get reduced allocation
- Overall EV improves, Sharpe improves

---

## Why Defer

- Tail-win-pct requires historical wins data. We have zero closes yet.
- Variance cost requires outcome distribution. Zero data.
- Sigmoid thresholds are prior-tuned, need empirical calibration.
- Risk of over-sizing the wrong configs before we've verified the OOS grid holds live.

---

## Revisit Trigger

At 100 closed outcomes AND tail_stats_cache shows coverage on 10+ coin+engine pairs:
- Run empirical WR × avg_pnl by convexity bucket analysis
- Apply activation decision rule above
- If activated: monitor 50 more trades before considering Stage 5 expansion
- If abandoned: remove framework, free the telemetry slot

Until then: silent telemetry only. Do not activate sizing.
