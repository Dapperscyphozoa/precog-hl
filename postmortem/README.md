# PreCog Post-Mortem Tuning Engine

Per-component forensic analysis of every HL close. 21 parallel agents
diagnose which signal components failed, a synthesizer cross-checks,
and the tuner applies surgical parameter deltas within hardcoded bounds.

**HL-ONLY.** Does not touch any MT4 code path.

## Flow

```
HL close → record_close() → run_postmortem_async(pos, coin, pnl_pct)
                                        ↓
                            daemon thread spawned (non-blocking)
                                        ↓
                           build_trade + build_context (per component)
                                        ↓
                           21 agents in parallel (Claude Haiku)
                                        ↓
                              synthesizer (Claude Sonnet)
                                        ↓
                          tuner applies approved deltas within bounds
                                        ↓
                           signal_params table updated live
                                        ↓
                      next signal reads new params via get_param()
```

## Components tuned (21)

`rsi, pivot, cooldown, bollinger, adx, ema, ob, wall, cvd, fvg, fib, sr,
structure, funding, session, oi, whale, liq, regime, sl, tp`

Each has 1-3 params, each with hardcoded bounds in `bounds.py`.

## Guardrails (5 layers)

Every proposed delta must pass all 5 gates to apply:

1. **Bounds check** (`bounds.py`) — absolute min/max + max_step_per_tune
2. **Synthesis approval** — head synthesizer must say APPROVE
3. **Confidence floor** — agent confidence ≥ 0.55 (env: `POSTMORTEM_MIN_CONFIDENCE`)
4. **Sample gating** — min_samples_between_tunes per param (prevents oscillation)
5. **Dry-run switch** — `POSTMORTEM_DRY_RUN=1` blocks all writes (for testing)

## Env vars

| Var | Default | Purpose |
|-----|---------|---------|
| `ANTHROPIC_API_KEY` | — | **Required.** Module no-ops silently without it. |
| `POSTMORTEM_ENABLED` | `1` | Global kill switch. Set `0` to disable. |
| `POSTMORTEM_DRY_RUN` | `0` | `1` = agents run, findings logged, no param writes. |
| `POSTMORTEM_MODEL` | `claude-haiku-4-5` | Per-component agent model. |
| `POSTMORTEM_SYNTH_MODEL` | `claude-sonnet-4-5` | Synthesizer model. |
| `POSTMORTEM_MIN_CONFIDENCE` | `0.55` | Floor for applying deltas. |
| `POSTMORTEM_MAX_WORKERS` | `6` | Parallel agent concurrency. |
| `POSTMORTEM_DB` | `/var/data/postmortem.db` | SQLite path on Render disk. |

## Endpoints (all read-only except noted)

- `GET /postmortem/ping` — smoke test
- `GET /postmortem/status` — engine state + recent runs
- `GET /postmortem/params?coin=BTC` — current tuned params
- `GET /postmortem/vetos?all=1` — active vetos
- `GET /postmortem/log?limit=50` — recent forensic runs
- `GET /postmortem/findings/<log_id>` — per-agent output for one run
- `GET /postmortem/history?coin=BTC&component=rsi` — param change history
- `GET /postmortem/bounds` — full bounds table for all components
- `POST /postmortem/reset/<coin>` — wipe tuned params for a coin (requires `X-Webhook-Secret`)
- `POST /postmortem/veto/<coin>/<component>` — manual veto (requires auth)
- `POST /postmortem/veto/<coin>/<component>/clear` — clear veto (requires auth)

## Signal engine integration

To make a signal engine respect tuned params:

```python
from postmortem import get_param, get_veto

# Instead of hardcoded RSI threshold:
rsi_hi = get_param(coin, 'rsi', 'sell_threshold', default=75.0)

# Check for vetos before firing:
if get_veto(coin, 'rsi'):
    return None  # system learned this component doesn't work for this coin
```

Calls are cheap (30s in-memory cache) and safe (never raise, always
return a value — bounds default → caller default → None).

## Safety

- Runs in a daemon thread — never blocks the main trading loop
- All exceptions swallowed inside the runner — trading continues even if
  post-mortem crashes
- MT4 path (`mt4_trade_closed()`) does NOT call into this module
- No writes to any existing file on the `/var/data` disk — postmortem
  owns its own `postmortem.db` file
- If `ANTHROPIC_API_KEY` is missing, the module logs the close and exits
  without calling Claude

## Rollback

If the system goes wrong:

```bash
# Disable globally
# On Render: set env var POSTMORTEM_ENABLED=0, restart

# Reset one coin
curl -X POST -H "X-Webhook-Secret: $SECRET" \
  https://cyber-psycho.onrender.com/postmortem/reset/BTC

# Reset everything (nuclear)
sqlite3 /var/data/postmortem.db "DELETE FROM signal_params; DELETE FROM component_vetos;"
```

Reverting the signal-engine integration is separate — the default-value
fallback in `get_param()` means removing the call reverts to hardcoded
behavior transparently.
