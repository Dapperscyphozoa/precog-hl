# System B Decoupling from System A

**2026-04-29 directive:** SA is locked. SB operates independently. Signal
flow on SB must not be gated by SA's verified-engines-only allowlist.

## What changed

### Before
`confluence_worker.py:324` called `precog._engine_disabled(name, coin)`.
That function applies (in order):

1. SA's `VERIFIED_ENGINES_ONLY` allowlist — **strangles SB.**
2. SA's `VERIFIED_LOSER_BASELINE` — includes HL (SA-only) plus 3 SB combos.
3. SA's manual `DISABLE_ENGINES` env list.
4. SA's auto-pause and per-coin × per-engine pause.

When the SA allowlist is `BB_REJ,LIQ_CSCD,PIVOT,CONFLUENCE_DAY+NEWS,
CONFLUENCE_DAY+SNIPER`, only 2 SB combos pass (`+DAY+NEWS`, `+DAY+SNIPER`).
Every other SB confluence combo gets dropped, regardless of its own merit.

### After
`confluence_worker.py` calls a new `_sb_engine_disabled()` defined in the
same module. SB-only logic:

1. **SB-side verified-loser baseline** (hardcoded):
   - `CONFLUENCE_BTC_WALL+NEWS`
   - `CONFLUENCE_BTC_WALL+SNIPER`
   - `CONFLUENCE_BTC_WALL+DAY`

   `HL` is intentionally **not** included — it's an SA tag, irrelevant to SB.
2. `CONF_DISABLE_ENGINES` env (SB-only kill list, supports `prefix*`).

That's it. SB does **not** read `VERIFIED_ENGINES_ONLY`, `VERIFIED_ENGINES_ALLOWLIST`,
or `DISABLE_ENGINES`. SA's allowlist no longer affects SB.

## SB-only env tunables

| Env | Default | Effect |
|---|---|---|
| `SB_VERIFIED_LOSER_VETO` | `1` | If `0`, even the hardcoded SB-loser baseline is bypassed (full freedom). |
| `CONF_DISABLE_ENGINES` | `""` | Comma-separated SB tags to additionally block. Wildcard: `CONFLUENCE_FUNDING+*`. |
| `CONFLUENCE_MAX_POSITIONS` | `25` | Bumped from 16 — restored to original SB design. |
| `CLUSTER_THROTTLE_ENABLED` | `1` | Per (engine, side) burst limit (3 fires/5min). Kept on as defense. |
| `CONF_MIN_SYS` | `2` | Min systems agreeing for confluence fire. |
| `CONF_MIN_DOMAINS` | `2` | Min orthogonal data-domains agreeing. |

## Status visibility

`/confluence_status` (or `confluence_worker.status()`) now includes a
`sb_filter` block:

```json
{
  "sb_filter": {
    "verified_loser_veto_enabled": true,
    "verified_loser_baseline": ["CONFLUENCE_BTC_WALL+DAY", "CONFLUENCE_BTC_WALL+NEWS", "CONFLUENCE_BTC_WALL+SNIPER"],
    "conf_disable_engines_env": "",
    "max_positions": 25,
    "risk_pct": 0.01,
    "allowed_sides": ["BUY", "SELL"],
    "decoupled_from_sa_allowlist": true
  }
}
```

`decoupled_from_sa_allowlist: true` is the canary — if it disappears, SA is
back in the loop.

## Why SB was historically "profitable"

Audit of lifetime closes (ex-kFLOKI ghost) shows SB is a mix:

| Combo | n | WR | sum_pnl | Verdict |
|---|---|---|---|---|
| `CONFLUENCE_DAY+NEWS` | 15 | 36% | **+$1.51** | Real edge (high mean PnL) |
| `CONFLUENCE_DAY+SNIPER` | 5 | 80% | +$0.03 | Clean small sample |
| `CONFLUENCE_BTC_WALL+OBI` | 27 | 47% | +$0.11 | Marginal+ |
| `CONFLUENCE_BTC_WALL+DAY+SNIPER` | 23 | 50% | +$0.06 | Marginal+ |
| `CONFLUENCE_NEWS+SNIPER` | 16 | 50% | +$0.003 | Marginal |
| `CONFLUENCE_BTC_WALL+NEWS` | 64 | 40% | -$1.72 | **Loser (blocked)** |
| `CONFLUENCE_BTC_WALL+SNIPER` | 59 | 38% | -$0.74 | **Loser (blocked)** |
| `CONFLUENCE_BTC_WALL+DAY` | 19 | 40% | -$0.51 | **Loser (blocked)** |

The original SB design intentionally fires across many combos, finds the
winners, lets Wilson auto-disable + bucket filter cull drift. Over-narrowing
to the 5-engine SA allowlist killed that exploration.

This change restores SB's combinatorial signal volume while keeping the
3 verified losers blocked.

## SA lockout verification

`tests/test_sb_only.py::TestSALockout::test_no_precog_modifications`
asserts `git diff origin/main HEAD -- precog.py` is empty. Any future
SB ship must not touch SA — the test will catch it.
