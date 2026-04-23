# Interaction Edge Decomposition — Future Work Backlog

**Logged:** 2026-04-23
**Status:** a priori framework (0 closed trades at time of logging)
**Validates when:** 100+ live closes accumulated
**Priority:** defer until empirical data available

---

## Summary of priors

Built from OOS enterprise grid (190 coins × 2160 configs × 5-fold CV).
Linear model edge = +1.86R/trade.
Interaction-adjusted edge = +1.96R/trade (wider variance structure).

Path distribution:
- +2.5R per trade, 70% of the time (clean conditions)
- +0.3R per trade, 15% of the time (near-gate or transition)
- −0.6R per trade, 15% of the time (fragility zones)

---

## Interaction Matrix (modeled)

|  | Signal | Regime | Timing | Cost |
|---|---|---|---|---|
| **Signal** | — | 1.24 ↑ | 0.87 ↓ | 0.69 ↓ |
| **Regime** | 1.24 ↑ | — | 1.23 ↑ | 1.22 ↑ |
| **Timing** | 0.87 ↓ | 1.23 ↑ | — | −0.03 ✗ |
| **Cost** | 0.69 ↓ | 1.22 ↑ | −0.03 ✗ | — |

**Regime is the universal multiplier** — compounds every other factor positively.
**Timing × Cost is destructive** — scalping-style inversion.

---

## Fragility Zones Identified

### Zone A — Regime Transition
- Trigger: detector flip in last 2h, hysteresis engaged, 1h vs 30m disagreement
- Impact: −0.45R/trade, σ spikes 2.1 → 3.4
- Frequency: 30h/month across ~10 transitions
- Mitigation: pause 2 bars post-flip OR 50% size during disagreement OR require top-K ensemble vote

### Zone B — Low Liquidity + Strong Signal + Short Hold
- Trigger: EIGHTY_89 tier + momentum breakout + TP ≤ 5%
- Impact: +0.12R gross → −0.05R net (slippage ≥ modeled)
- Frequency: 3-6 trades/day (scales with account size)
- Mitigation: post-only limits on small notional, avoid TR engine on EIGHTY_89 during high VIX

### Zone C — Consecutive Same-Direction Signals in Chop
- Trigger: multiple BB/MR on same coin within 2-4 bars during chop
- Impact: 1st +1.2R, 2nd +0.4R, 3rd −0.8R (exponential decay)
- Frequency: 2-3/month but devastating per occurrence
- Mitigation: regime-specific cooldown (2× longer in chop), top-K ensemble voting, per-coin cluster detection

### Zone D — Funding Rate Storm + Direction Conflict
- Trigger: funding > 0.05%/8h against position direction
- Impact: −0.25R/trade from funding bleed
- Frequency: 5-15% of positions
- Mitigation: verify funding-aware exit still active in 15m rebuild

### Zone E — Near-Gate Signals + Low Sample Size (CRITICAL)
- Trigger: Wilson lb 0.50-0.52 AND n < 10 (all bear-calm fits this)
- Impact: −0.40R/trade
- Frequency: 12% of universe (all 7 bear-calm configs)
- Mitigation: Wilson-lb-weighted sizing (0.5× at lb=0.50, 1.0× at lb=0.60+)

---

## Action Priority (interaction-aware)

| Priority | Action | Expected gain/trade | Cost to implement |
|---|---|---|---|
| 1 | Regime detector accuracy + transition confidence scoring | +0.40R | medium |
| 2 | Wilson-lb-weighted position sizing | +0.20R | low |
| 3 | Top-K ensemble voting (≥2/3 configs to fire) | +0.15R | low |
| 4 | Regime-specific cooldowns (2× in chop) | +0.10R | low |
| 5 | Funding-aware exit verification | +0.08R | trivial |
| 6 | Post-only limit orders on small notional | +0.06R | medium |

**Top 3 = +0.75R/trade = 38% improvement over modeled edge.**

---

## Data Required Before Implementation

Priority order:

1. **Per-regime WR** — 20 closes per regime (validates multiplier)
2. **Per-Wilson-lb-tier WR** — 30 closes by lb range (validates Zone E)
3. **Hold-duration × cost impact** — avg hold time per outcome class
4. **Transition-window WR** — closes tagged stable vs flipping-last-2h (Zone A)
5. **Consecutive-signal decay** — sequence-tagged trades (Zone C)

Until this data exists, the framework is a prior, not a measurement.

---

## Conditional Edge Tables (modeled)

### Signal quality conditional on regime correctness
| Condition | Signal edge |
|---|---|
| Regime correct | +1.48R |
| Regime wrong | +0.22R |
| Regime boundary (transition) | −0.31R |

Signal edge is **~7× stronger in correct regime.** Regime is a multiplier, not a summand.

### Timing conditional on signal strength
| Signal strength | Timing edge |
|---|---|
| Strong (Wilson lb > 0.60, Sharpe > 3) | +0.62R |
| Medium (lb 0.50-0.60) | +0.28R |
| Weak (near-gate) | +0.05R |

Timing infrastructure ROI scales with signal quality.

### Cost drag conditional on hold duration
| Hold | % of R consumed by cost |
|---|---|
| <1 candle | 37% |
| 1-6 candles | 18% |
| 6-24 candles | 9% |
| >24 candles | 4% |

---

## Nonlinear Model

### Wrong (additive)
```
edge = signal + regime + timing − costs + luck
```

### Correct (multiplicative)
```
edge = signal × regime_multiplier × timing_multiplier × (1 − cost_drag)

signal: 1.30R base
regime_multiplier: 0.15 (wrong) to 1.40 (correct)
timing_multiplier: 0.85 (slipped) to 1.15 (on-close)
cost_drag: 0.04 (long hold) to 0.37 (short hold)
```

### Examples

Perfect storm UNI trade:
```
1.45 × 1.40 × 1.15 × (1 − 0.09) = 2.12R
```

Fragility-zone SOL trade:
```
0.80 × 0.35 × 0.80 × (1 − 0.14) = 0.19R
```

**11× variation between best and worst interaction states.**

---

## Marginal value of factor improvement

| Factor at weakest | Poor → Average | Good → Excellent |
|---|---|---|
| Signal quality | +0.60R | +0.20R |
| **Regime alignment** | **+1.30R** | +0.15R |
| Timing | +0.30R | +0.05R |
| Cost management | +0.20R | +0.10R |

**Regime detection accuracy (poor → average) is highest-leverage lever.**

---

## Revised True Edge Attribution

| Component | Linear % | Interaction-adjusted % |
|---|---|---|
| Signal quality | 42% | 35% |
| Regime multiplier | 23% | 31% |
| Timing | 14% | 14% |
| Cost drag | −19% | −14% |
| Interactions captured | 0% | +12% |
| Luck (variance) | 21% of outcome | 21% of outcome |
| Fragility zones | unmodeled | −9% expected |

Net: Base +1.86R + pairwise lift +0.38R − fragility drag −0.28R = **+1.96R/trade modeled**

---

## When to revisit

- **After 30 closes:** basic WR validation, abandon framework if per-regime WR deviates >15pp from OOS
- **After 100 closes:** first empirical interaction matrix, replace modeled cells with observed
- **After 250 closes:** fragility zones empirical, priority list re-ranked by observed impact
- **After 500 closes:** full Monte Carlo revalidation of edge model, architect Stage 4 improvements

Keep this document. Do not optimize until data replaces prior.
