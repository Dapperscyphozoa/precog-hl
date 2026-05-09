# V9 HALT MECHANISMS

V9 (`srv-d7u59hl0lvsc73enssrg`) is LIVE on the HL wallet. Three halt paths in priority order:

## 1. HALT_FILE — zero-restart, fastest (RECOMMENDED for ad-hoc halts)

```bash
# Open V9 Render Shell (dashboard → service → Shell tab)
touch /var/data/v9_halted    # halt: next placement attempt skipped (~30s max)
rm    /var/data/v9_halted    # resume
```

- Effect: `_is_halted()` reads filesystem on every order attempt
- State persists across redeploys (file lives on the persistent disk)
- **Cancels and `market_close` still work** — emergency exits intentionally never gated
- Tick header shows `⚠ HALTED` line on every halted tick
- Engine logs `HALTED: skip {coin} {side} ...` first time per tick + summary count

## 2. LIVE_TRADING env var — durable, requires restart

```bash
curl -X PUT \
  -H "Authorization: Bearer $RENDER_TOKEN" \
  -H "Content-Type: application/json" \
  https://api.render.com/v1/services/srv-d7u59hl0lvsc73enssrg/env-vars/LIVE_TRADING \
  -d '{"value":"0"}'
```

- Render auto-redeploys on env change (~3 min)
- After restart, halt is effective immediately
- Use for durable halts (across operator handoffs) — env is the source of truth in dashboard

**KNOWN GOTCHA:** os.environ in a running Python process is captured at start. Even though
`_is_halted()` re-reads `os.environ.get('LIVE_TRADING')` on every call, the value is the
process snapshot — env changes only propagate on restart. The dynamic re-read is still
useful in case engine code modifies its own environ (e.g. for tests).

## 3. SUSPEND SERVICE — nuclear

Render dashboard → service → Suspend. Stops everything including reconcile/tracker.
Use only when 1+2 unavailable. Pendings may stay open mid-bracket.

## Safety order

`touch HALT_FILE` → `LIVE_TRADING=0` → `suspend service`

## Telemetry

- Tick header shows `⚠ HALTED` when halt active
- `HALTED: skip {coin} ...` logged on first skipped order per tick
- `HALTED: total N order placements skipped this tick` summary line at tick end (if N > 1)

## What is NOT halted

- `cancel_order` — operators must be able to clear resting orders during halt
- `market_close` — emergency exits must always work to close existing positions

## Verifying halt is active

After touching the halt-file, watch the next tick header:

```
[hh:mm:ssZ] ━━ TICK #N ━━
[hh:mm:ssZ]   ⚠ HALTED — LIVE_TRADING=1, halt_file=True. New orders will be skipped...
```

If a setup forms during halt, expect:

```
[hh:mm:ssZ]   HALTED: skip BTC BUY ENTRY sz=0.001 px=80000 reduce_only=False
[hh:mm:ssZ]           (LIVE_TRADING=1, halt_file_exists=True)
[hh:mm:ssZ]   HALTED: total 4 order placements skipped this tick
```
