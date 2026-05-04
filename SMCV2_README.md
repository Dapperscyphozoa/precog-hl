# SMC v2 — Standalone Service

**Branch:** `smc-v2`  •  **File:** `smc_v2_service.py`  •  **Runtime:** Python 3 background worker

## What it is

Top-down SMC strategy (HTF=4H → MTF=1H → LTF=15m) running the **R3 config** validated against 179-coin Hyperliquid universe over 52 days.

**Backtest performance (R3, n≥2 zero-win blacklist applied):**
- 365 setups / 52 days
- 60% WR
- +0.77R avg per trade
- +281R total
- 147/179 coins active, 96 profitable

## Isolation from PreCog v8.28

- Same HL wallet (uses `HL_ADDRESS` / `HL_PRIVATE_KEY`)
- All cloids prefixed `smcv2_` — never collides with PreCog's order IDs
- Separate state file: `/var/data/smc_v2_state.json`
- Reads HL `user_state` only to detect TP1 fills on its own positions
- Does not touch PreCog's state, code, or open positions

## Sizing (env vars)

| Var | Default | Purpose |
|---|---|---|
| `SMCV2_NOTIONAL_USD` | 25 | Fixed $ notional per trade |
| `SMCV2_LEVERAGE` | 10 | Initial leverage hint (HL applies per-coin caps) |
| `SMCV2_MAX_CONCURRENT` | 20 | Max simultaneous open positions |
| `SMCV2_LIVE` | 0 | Set to `1` to enable real order submission |

## Strategy params (LOCKED — R3)

| Param | Value |
|---|---|
| htf_lb | 5 |
| htf_displace | 1.75 |
| htf_max_age | 540 (4H bars = 90 days) |
| ltf_lb | 4 |
| sweep_vol | 1.0 |
| mss_vol | 1.0 |
| displace | 2.0 |
| sl_buf_pct | 0.0003 |
| approach_pct | 0.03 |
| rr_min | 2.25 |
| timeout_bars | 40 |

## Blacklist (10 coins, n≥2, zero wins in backtest)

`IP, ATOM, AIXBT, ENS, OP, SKR, STRK, WLFI, kLUNC, BLAST`

## Pipeline

1. **HTF (4H):** detect bias from HH/HL or LH/LL chain. Find unmitigated OB/FVG zones.
2. **MTF (1H):** gate — last HL holds (BULL) or last LH holds (BEAR).
3. **LTF (15m):** state machine
   - IDLE → IN_ZONE: price within `approach_pct` of HTF zone matching bias
   - IN_ZONE → SWEPT: liquidity sweep with vol confirmation
   - SWEPT → ARMED: MSS body close past LH/HL with displacement + vol
   - ARMED → FILL: price retests entry level
4. **Execution:** limit entry + native SL + native TP1 (50%) + native TP2 (50%)
5. **Management:** TP1 fill → cancel SL, place new SL at entry (BE)
6. **Time stop:** 10 hours from fill

## Deploy

### Render

1. Render dashboard → New → Background Worker
2. Connect repo `Dapperscyphozoa/precog-hl`, branch `smc-v2`
3. Build: `pip install -r requirements.txt`
4. Start: `python3 smc_v2_service.py`
5. Add 1GB persistent disk at `/var/data`
6. Env vars: `HL_ADDRESS`, `HL_PRIVATE_KEY`, set `SMCV2_LIVE=1` when ready

(See `render-smcv2.yaml` for full spec.)

### Local test (dry run)

```bash
export HL_ADDRESS=0x...
export HL_PRIVATE_KEY=0x...
export SMCV2_LIVE=0
export SMCV2_STATE_PATH=./smc_v2_state.json
python3 smc_v2_service.py
```

## Kill switch

Stop the Render service. Open positions remain on HL — close manually or let TP/SL/time-stop handle them.

## Logs

`stdout` / Render service logs. Format: `[ISO timestamp] message`.

Key events: `FIRE`, `ENTRY FILLED`, `TP1 FILLED — moving SL to BE`, `RUNNER CLOSED`, `TIME STOP`.

## Observability after deploy

After 14 days live, compare against backtest:
- Setups/day: predicted ~7
- WR: predicted 60%
- Avg R: predicted +0.77

If material divergence, diagnose before changing params.
