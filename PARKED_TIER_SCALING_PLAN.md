# PARKED — Full 12-parameter tier scaling plan (revisit after Council baseline data)

Council voted 4-1 against shipping all of this now (Nov 2025 deliberation).
Revisit when: 7-day forward walk on the smaller council-approved package
completes AND framework WR ≥ 50% confirms the signal works.

## The 12 levers identified

### CRITICAL — directly affects WR
1. MSS body thresholds (`body > X×rng AND body > Y×ATR`)
2. OB consolidation threshold (`body < Z×ATR` for "consolidating" bars)
3. SL buffer past wick (`sl_buffer_ticks`, `sl_min_buffer_pct`)

### HIGH — changes risk profile
4. RISK_PCT per trade (currently flat 0.25%)
5. min_rr_to_tp1 (currently flat 1.5)
6. proximity_pct for "at zone" detection (currently flat 0.5%)
7. displacement_atr_mult in HTF zone detection (currently flat 1.5)

### MEDIUM — efficiency
8. SETUP_EXPIRE_S (currently flat 4h)
9. RUNNER_BE_TIMEOUT_S (currently flat 2h)
10. TRACKER_TIMEOUT_S (currently flat 24h)
11. MAX_NOTIONAL_PCT (currently flat 20%)
12. FEE_RT in paper accounting (currently flat 0.06% RT, understates tier 6 reality)

## Council objections to revisit

- **Quant:** noise-floor hypothesis needs empirical grounding (30d 5m data, 1 coin/tier, measure false-MSS rate per tier). Don't optimize on n=7.
- **Microstructure:** cap is wrong proxy. Use `spread_bps + 1/sqrt(top_of_book_usd)` measured live. Already pulled by wall_confluence_check, free to extract.
- **Risk Manager:** 7 tiers × 12 params = 84 cells. Need ~30 trades/cell = 2520 paper trades to validate. Months of FW. Collapse to 3 tiers.
- **Software:** if revisited, implement as single `TierProfile` dataclass passed through all stages, not 12 sprinkled lookups.
- **Devil's Advocate:** parameter scaling won't fix a broken framework signal. Establish baseline first.

## Re-entry criteria

Only revisit this plan if:
1. The smaller council-approved package (revert MSS + tiered SL buffer + spread/depth instrumentation) shows WR >= 50% over >= 7 days forward walk
2. AND the spread/depth log data shows clear stratification by tier (validating noise-floor hypothesis)
3. AND >= 30 closed trades to give per-tier WR signal

If those three conditions are met, then implement using the SoftwareEng-recommended TierProfile pattern, with Risk Manager's 3-tier collapse, on the most evidence-backed levers first (likely 1, 2, 3 already done; then 6, 7, 8).
