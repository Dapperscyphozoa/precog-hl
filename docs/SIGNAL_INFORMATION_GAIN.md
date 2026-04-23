# Signal Information Gain — Future Work Backlog

**Logged:** 2026-04-23
**Status:** a priori framework; builds signal telemetry now, deferred optimization
**Validates when:** 500+ closes + 1000+ bars of parallel signal logging
**Priority:** Stage 4-5 (after timing refinement, after sizing/ensemble/regime-detector work)

---

## Premise

Confluence that looks like "3 signals agreeing" is often 1 underlying signal measured 3 ways. Mutual information quantifies how much each input truly contributes — vs how much is restating what you already knew.

For PreCog: many indicators in the decision pipeline likely share 60-80%+ of their information. Stacking them inflates false confidence in redundant agreement.

---

## Signal Information Gain (priors, 0-1 scale)

### Uniquely contributing signals (HIGH info gain)
| Signal | Info gain | Notes |
|---|---|---|
| Regime detector (BTC 1h + 30m) | 0.85 | Universal multiplier |
| Funding rate | 0.75 | Positioning, price-independent |
| Liquidation clusters | 0.70 | Stop-hunt topology (not currently used) |
| Session / time-of-day | 0.65 | Participant mix, independent dimension |
| CVD divergence | 0.55 | Tape-level aggressor info |
| Swan event monitor | 0.50 | Cross-asset regime detector |

### Primary engines (MEDIUM info gain, with overlap)
| Signal | Info gain | Notes |
|---|---|---|
| BB engine | 0.45 | Unique in isolation |
| PV engine | 0.35 | Redundant with BB when both fire |
| MR engine | 0.35 | Redundant with BB on mean-reversion states |
| IB engine | 0.30 | Sparse signals, some redundancy |
| VS engine | 0.30 | Overlaps with storm regime |
| TR engine | 0.25 | Redundant with regime detector in trend regimes |

### Low info gain / likely removable
| Signal | Info gain | Notes |
|---|---|---|
| V3 trend (confidence 20pts) | 0.15 | Redundant with regime detector |
| 1H pullback (confidence 15pts) | 0.15 | Redundant with regime + BB pierce |
| 5m momentum (confidence 10pts) | 0.10 | Near-leakage with 15m signal bar |
| Candle pattern (1pt) | 0.05 | Noise on 15m crypto |
| OB/FVG (1pt each) | 0.05 | Human pattern-rec without predictive power |

### Variable info gain
| Signal | Info gain | Notes |
|---|---|---|
| News / auto-macro | 0.40 | Peaks during events, zero otherwise |
| Bybit lead | 0.25 | Short-window edge, decays in minutes |

---

## Redundancy Clusters Identified

### Cluster 1 — "RSI / momentum family" (~70% shared info)
BB, PV, MR, IB engines + V3 trend + TR (RSI-aligned) + 5m momentum

**Action (future):** BB as primary, require ensemble vote for PV/MR, drop 5m momentum

### Cluster 2 — "Trend / regime family" (~80% shared info)
Regime detector + V3 trend + EMA cross + TR engine + BTC 4h trend

**Action:** Regime detector as canonical. Strip V3 trend from confidence score.

### Cluster 3 — "Volatility family" (~65% shared info)
VS engine + storm/calm classifier + ATR ETIT + swan vol component

**Action:** Weight VS lower in storm regimes

### Cluster 4 — "Exogenous cross-asset" (~20% shared info)
Swan events + news + DXY/Oil/SPY + funding + liquidations

**Action (future):** Expand. These are the signals with genuine unique information.

---

## Proposed Rebalancing (when empirical data validates)

### Remove
- 5m momentum in confidence scoring (near-leakage)
- OB/FVG/candle pattern points (noise on 15m)
- Per-coin WR ticker gates on post-enterprise signals (double-filtering)
- MT4-specific gates (Yahoo pullback) from HL path (risk surface)

### Downweight
- V3 trend: 20pts → 8pts
- TR engine: 1.0× → 0.5× in trend regimes
- PV engine: treat as same signal as BB within 4-bar window
- IB engine: raise threshold or deprecate (sparse + redundant)

### Upweight
- Funding rate → promote from filter to signal (15pt bonus on direction alignment)
- Liquidation clusters → add as 10pt bonus near cluster levels
- Session → multiplier not adjustment (1.2× London/NY, 0.7× Asian)
- CVD divergence → +15% position size bonus when aligned with engine

---

## Information-Gain-Corrected Attribution

| Source | Linear attribution | Info-gain attribution | Δ |
|---|---|---|---|
| Regime detector | 23% | **32%** | +9pp |
| Engine ensemble (BB/PV/MR voted) | 42% | **26%** | −16pp |
| Exogenous (funding/liq/session) | 5% | **18%** | +13pp |
| Timing/execution | 14% | 14% | 0 |
| Cost drag | −19% | −14% | +5pp |
| Redundant noise | silent drag | removed | — |

---

## Expected Impact (estimated)

- False-positive reduction: 15-25% (redundant "confluence" stops firing)
- WR improvement: +3-5pp raw
- σ reduction: ~15% (less amplification of regime-wrong trades)
- Trade frequency: −10-15% (mostly losers removed)
- EV per trade: **+0.22R** modeled

Smaller than timing refinement (+0.38R), but lower implementation risk.

---

## Data Required Before Implementation

1. **500+ closed trades** (baseline for signal outcome correlation)
2. **1000+ bars of parallel signal logging** — every bar, every coin, which signals fired/would have fired
3. **Per-regime separation** — signal redundancy differs by regime (bull-calm vs chop)
4. **Nightly MI calculation** — `I(signal_A; signal_B | outcome)` for all signal pairs

---

## Action Priority (after prior backlog items)

| Priority | Action |
|---|---|
| — | Build signal state logger (this task, in progress) |
| — | Build nightly redundancy calculator (cron, future) |
| 4 | Apply rebalancing based on measured MI |
| 5 | Re-run enterprise grid with cleaned filter stack |

---

## Trigger

When signal state logger accumulates 1000+ bar-signals AND trade count hits 500+ closes:
- Run empirical MI calculation
- Replace prior info-gain scores with measured values
- Re-rank redundancy clusters
- Implement downweight/remove/upweight per empirical data

Until then: framework is prior, not measurement. Do not optimize.
