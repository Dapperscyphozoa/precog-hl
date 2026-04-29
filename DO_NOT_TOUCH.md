# DO NOT TOUCH — LOCKED CONFIGURATION

**Status:** LOCKED as of 2026-04-29 22:55 UTC
**Owner:** Cyber (operator)
**Authority required for any change:** Direct written confirmation from Cyber, in-conversation, before any modification.

---

## Why this file exists

On 2026-04-29, an SB-targeted update from Claude Code (`sb-rbac-throttle` merge, PR #46dd7d8) silently disrupted SA. SA had been quietly profitable for 15h producing +$2.20 across 8 closes. Subsequent attempts to "fix" SA via SL tightening (PR #34: 10bp SL, PR #44: 30bp SL) were based on incorrect assumptions about the engine's MAE/MFE distribution and would have stopped every winning trade before MFE developed.

The verified-from-data winning configuration is recorded below. Any deviation must be explicitly approved.

---

## LOCKED PARAMETERS

### Engine SL (Stop Loss)

| Engine | LOCKED VALUE | Env var |
|---|---|---|
| BB_REJ | **0.015 (150bp)** | `SL_OVERRIDE_BB_REJ=0.015` |
| PIVOT | **0.015 (150bp)** | `SL_OVERRIDE_PIVOT=0.015` |

**Why 150bp:** Verified winning trades on 2026-04-29 had MAE up to 80bp before turning profitable. The MAE→MFE journey is the trade. Tighter SL stops the engine before its edge develops.

**Evidence:**
- LTC SELL: MAE -80bp → MFE +265bp → realized +$1.06
- ADA SELL: MAE -10bp → MFE +86bp → realized +$0.35
- STABLE BUY: MAE 0bp → MFE +187bp → realized +$0.68
- TRB SELL (×2): MAE 0bp → MFE +113bp → realized +$0.07, +$0.22

If SL had been 30bp: LTC stops at -30bp before recovering, turns into a loss. Estimated portfolio swing: from +$2.20 to ~−$1.00.

### Engine TP (Take Profit) — per-coin defaults, do not override

| Coin | TP | Source |
|---|---|---|
| STABLE | 80bp | per-coin gates.py |
| ADA | 100bp | per-coin gates.py |
| LTC | 50bp | per-coin gates.py |
| TRB | 100bp | per-coin gates.py |
| WLFI | 100bp | per-coin gates.py |

**Do NOT set `TP_OVERRIDE_BB_REJ` or `TP_OVERRIDE_PIVOT`.** The per-coin TPs were tuned on 600+ trades pre-PR-#34 and are working.

### Allowlist (verified engines)

```
VERIFIED_ENGINES_ONLY=1
VERIFIED_ENGINES_ALLOWLIST=BB_REJ,LIQ_CSCD,PIVOT,CONFLUENCE_DAY+NEWS,CONFLUENCE_DAY+SNIPER
```

**Locked rationale:** These five engines have verified positive sum_pnl ex-kFLOKI on the lifetime ledger. Adding more engines re-introduces verified losers (BTC_WALL+* family). Removing any engine reduces firing surface below sustainability.

### Verified-loser baseline (hardcoded blocks)

```python
_VERIFIED_LOSER_BASELINE = {
    'HL',                              # n=24, sum=-$5.29
    'CONFLUENCE_BTC_WALL+DAY',         # marginal loser
    'CONFLUENCE_BTC_WALL+NEWS',        # n=64, sum=-$1.72
    'CONFLUENCE_BTC_WALL+SNIPER',      # n=56, sum=-$0.77
}
```

**Do NOT remove from baseline.** These are verified-from-data money-losers. Any addition is one-way (must come with sample evidence).

### Bad-entry kill

```
BAD_ENTRY_KILL=1
BAD_ENTRY_MIN_MFE=0.0005     # 5bp
BAD_ENTRY_AGE_SEC=60
BAD_ENTRY_MAX_AGE_SEC=90
```

**Verified working:** Killed two STABLE entries at $0.0001 and $0.00 (no real loss) when they failed to develop MFE in 60s. Saves bleed on no-progress entries without affecting winners (winners had MFE > 5bp inside 60s).

### Hour veto

```
HOUR_VETO_HOURS=        # empty / disabled
```

**Locked off:** Bucket filter + bad-entry kill + Wilson auto-disable provide better-targeted protection. Hour veto was redundant.

### Notional + entry execution

```
FORCE_NOTIONAL_USD=44
MAKER_ONLY_ENTRY=1
```

**Verified working:** Maker entries on winning trades showed negative realized_slippage (price improvement) on 4 of 5 wins.

### System B

**Status:** Working as designed. SB fires rarely (1-3/day) via DAY+NEWS / DAY+SNIPER. The high-volume SB engines (BTC_WALL+*) were verified losers and are correctly blocked. Do not "fix" SB by re-enabling those.

---

## CHANGE PROCEDURE

Before modifying any LOCKED parameter:

1. **State the proposed change in writing.**
2. **Provide verified data evidence** (e.g., n trades, sum_pnl, MAE/MFE distribution from `/analyze` or `/trades`).
3. **Get direct confirmation from Cyber** in conversation. Words like "ship it", "go", "do it", "yes" — explicit, not inferred.
4. **Update this file** in the same PR as the change, recording the new locked value, the rationale, and the evidence.
5. **No silent changes.** No "while we're here" tweaks. No "small fix" that touches anything in this list.

## What is NOT locked

- Adding new engines to `VERIFIED_ENGINES_ALLOWLIST` (requires evidence + confirmation, but is additive)
- Bucket filter thresholds
- Wilson auto-disable thresholds
- Status / diagnostic endpoints
- Logging
- Code refactors that don't touch behavior

## What is ABSOLUTELY locked

- BB_REJ SL
- PIVOT SL
- Per-coin TP behavior (don't add overrides)
- Allowlist contents (additions require evidence; removals require explicit confirmation)
- Verified-loser baseline (don't remove)
- Notional sizing
- Maker-only entry

---

## Incident log

### 2026-04-29 — Claude Code SB-RBAC merge disrupted SA
- Merge `sb-rbac-throttle` (PR #46dd7d8) was advertised as "SA path untouched" but introduced an import + execution path that coincided with SA going silent for 5 hours
- Reverted via `Revert: remove all SB RBAC changes` deploy at 22:07 UTC
- During the same window, SL was tightened from 50bp → 10bp (PR #34) and 10bp → 30bp (PR #44) under faulty math that would have stopped every winning trade before MFE developed
- Verified-from-data winning SL is 150bp (per-coin gates.py default)
- This file written and committed to prevent recurrence

