# Reflexivity Detection — Future Work Backlog

**Logged:** 2026-04-23
**Status:** silent telemetry deployed, no live impact
**Validates when:** 75+ closes analyzed via /reflexivity
**Priority:** Stage 4 (after counterfactual activation decision)

---

## Premise

A firing signal is one of three things:
1. LEADING — price hasn't moved yet, edge intact
2. CONFIRMING — price moved a bit, riding early wave
3. CHASING — price already moved a lot, we're exit liquidity

OOS backtests treat all three the same. Live, they have wildly different
payoffs. Reflexivity detection measures where in the move we're firing.

---

## Three Measured Dimensions

### 1. Crowding (0-1)
How many other participants likely saw this?
- vol_ratio: current vol / 20-bar median (weight 60%)
- funding_crowd: |funding rate| / 0.0015 (weight 40%)

### 2. Move Position (0-1)
How deep into the move are we?
- distance from nearest recent swing low (BUY) or high (SELL)
- normalized by 14-bar ATR
- <0.3 ATR = early, 1.5+ ATR = chase

### 3. Reaction-to-Reaction (0-1)
Are we trading the echo?
- prior_4bar_btc_return / current_bar_btc_return
- High ratio = echo of earlier move

---

## Combined Risk + Recommendation

```
reflexivity_risk = 0.4 * crowding + 0.3 * move_position + 0.3 * reaction_score
```

| Risk | Recommendation |
|---|---|
| 0.0-0.3 | LEAD |
| 0.3-0.5 | FOLLOW |
| 0.5-0.7 | SKEPTICAL |
| 0.7-1.0 | AVOID |

---

## What It Protects Against

- Zone 3 (cascade chasing) — late-wave entries enterprise grid lets through
- Zone 4 (news shocks) — echo-bar trades after news
- Zone 7 (signal clustering) — when prior fires are part of the pile

---

## Activation Rule at 75 Closes

Pull `/reflexivity`, compare bucket WR + avg PnL:

- If `LEAD.avg_pnl - AVOID.avg_pnl >= 0.25R` → **ACTIVATE** live filter
  (skip AVOID signals, reduce SKEPTICAL to 0.5× size)
- If delta is 0.0-0.25R → monitor to 150 closes
- If AVOID outperforms LEAD → abandon (inverted edge, investigate)
- If delta negligible → abandon framework

---

## Expected Impact (if activated)

- +0.20R per trade EV gain (late-wave losers filtered)
- -12% trade frequency (filtered losers disproportionately)
- +0.15 Sharpe modeled

---

## Current Telemetry

Endpoint: `GET /reflexivity`
Log: `/app/reflexivity.jsonl`

At signal fire, records:
- crowding score, vol_ratio, funding_rate
- move_position, distance_atr
- reaction_score
- combined risk + recommendation

At close, pairs outcome with signal score via (coin, bar_ts) join.

---

## Data Quality Notes

- Funding cache TTL: 10min (one `metaAndAssetCtxs` fetch covers all coins)
- BTC bars cache: 60s shared across all coin scorings
- Target coin bars: fresh per scoring (30-bar window)
- Estimated overhead: ~1-3s per signal scoring (all network-bound)

---

## Revisit Trigger

75 closed outcomes → empirical bucket analysis → activation decision.

Until then: silent telemetry. Do not apply filter.
