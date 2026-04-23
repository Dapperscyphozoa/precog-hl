# Adversarial Market Simulation — Red Team Log

**Logged:** 2026-04-23
**Status:** top-3 defenses deployed; remaining 5 deferred until empirical fragility evidence

---

## Failure Mode Inventory (ranked by destruction potential)

### Zone 1 — Regime transition pain
- BTC rips/dumps 3%+ in 30min, 1h detector lags 1-2 hours
- Multiple positions get serial-stopped during transition
- Expected damage: -2.5% account per event
- Probability: ~12%/week
- **Mitigation (deferred):** Asymmetric regime hysteresis (1-bar flip-out, 2-bar flip-in)

### Zone 2 — HL infrastructure failure
- API 5xx for 15+ min during open positions
- No circuit breaker; loop keeps trying, rate-limit cascade on recovery
- Expected damage: -3% to -8% account
- Probability: ~3%/week
- **Mitigation (deferred):** 3-consecutive-error circuit breaker, 5-min pause

### Zone 3 — Correlated liquidation cascade
- BTC dump triggers late-chase shorts; bounce liquidates them
- Enterprise grid doesn't penalize late-signal entries on extended moves
- Expected damage: +edge evaporated, net near-zero instead of strongly positive
- Probability: ~2%/week but recurring pattern
- **Mitigation (deferred):** Anti-chasing filter (reject if price moved >2x ATR in last 2 bars)

### Zone 4 — News shock during hold
- SEC action, exchange hack, flash crash 15-20% in 5 min
- Swan monitor only covers 6 assets; coin-specific news not caught
- Expected damage: -7% to -20% account worst case
- Probability: ~1-2%/week at any moment
- **Mitigation (deferred):** Swan-BLACK → flatten new entries for 30 min

### Zone 5 — Funding rate reversal trap ✅ AUDITED
- Funding flips sign; 3-day short pays 0.6% cumulative vs expected earn
- Funding filter exists in codebase but wiring into 15m rebuild unverified
- Expected damage: -0.5% to -1% per affected position
- Probability: ~5%/week
- **Mitigation deployed:** `funding_exit_audit()` — startup log exposes wiring state

### Zone 6 — Thin-book slippage
- Low-liquidity coins (WLFI, SKR, BIGTIME) during Sunday UTC 3-7am
- Modeled 0.08% slip vs actual 0.4-0.8%
- Expected damage: 5-10 bps per trade at current size; 10-30bps at $20k+
- Probability: ~4%/week (low hours only)
- **Mitigation (deferred to $20k scale):** Liquidity-weighted post-only routing

### Zone 7 — Signal clustering on same coin ✅ MITIGATED
- BB engine fires SELL 3× in 6 bars during chop; WR decays 62/48/22%
- Third signal wipes first two wins
- Expected damage: -$1 to -$3 per cluster event, recurring 2-3×/week
- Probability: 80-90% (likely happening right now)
- **Mitigation deployed:** `chop_cooldown_multiplier(regime)` — 2× CD_MS in chop

### Zone 8 — Stale regime detector data ✅ MITIGATED
- BTC 1h candle endpoint returns stale data (Sunday rollover, API issues)
- Regime detector runs on cached/wrong classification for hours
- Expected damage: -$5 to -$20 before detection
- Probability: ~0.5%/week
- **Mitigation deployed:** `regime_staleness_ok()` — abort signals if BTC 1h age >65min

---

## Deployed Defenses (live now)

### 1. Chop Cooldown Extension
- Location: `red_team.chop_cooldown_multiplier(regime)`
- Effect: 2× CD_MS when regime=chop (60min instead of 30min)
- Applied: at signal evaluation in `process()`, after engine-level CD check
- Fail-open on regime lookup error

### 2. Regime Staleness Gate
- Location: `red_team.regime_staleness_ok()`
- Effect: skips signal if BTC 1h candle > 65min old
- Applied: at signal fire, before regime-side blocker
- Fail-open on network error (don't block trading on API hiccup)

### 3. Funding Exit Audit
- Location: `red_team.funding_exit_audit()`
- Effect: startup log exposes whether funding_filter/funding_arb modules
  imported AND whether any "funding" daemon thread is running
- Applied: at module load (runs once)
- No behavior change — audit only

---

## Near-Term Fragility Watch (48h window)

| Trap | Probability | Expected loss if triggered |
|---|---|---|
| Regime transition pain | 25-35% | -$5 to -$15 |
| Signal clustering | 80-90% | -$2 to -$5 (now mitigated) |
| Chop whipsaw | 40-50% | -$3 to -$8 |
| Funding bleed | 30-40% | -$2 to -$6 |
| HL API micro-outage | 10-15% | -$3 to -$10 |
| News shock | 2-3% | -$20 to -$100 |

**Expected aggregate friction 48h: -$8 to -$25**
**Expected aggregate gain 48h: +$15 to +$40**
**Account floor in any constructible scenario: ~$475 (survives 20% catastrophic tail)**

---

## Red Flag Watch (intervention triggers)

Log for these patterns — if seen, investigate immediately:

1. `[regime]` flip in <1 hour from last flip (whipsaw — needs asymmetric hysteresis)
2. More than 3 same-coin same-side positions in 30 minutes (clustering survived mitigation)
3. `[microstructure] label=fake_breakout` 2+ times in an hour (trap zone active)
4. `[postmortem]` entry_gate veto rate > 30% (KB learning signals are bad)
5. Multiple BTC `candle err` (regime detector starving — Zone 8 variant)

---

## Deferred Until Data

At trigger from telemetry stack:
- **counterfactual 50 closes** → skip_better_pct analysis validates cascade-chase hypothesis
- **microstructure 200 closes** → fake_breakout regime correlation informs Zone 4 filter
- **signal_log 500 closes** → correlation between regime flips and clustered losses

Each unlocks a different deferred mitigation. Do not build preemptively without evidence.
