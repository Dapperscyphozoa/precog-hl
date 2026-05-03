# SMC v1.0 — Extended Logging Integration

## What this adds (vs original plan)

| | Original | Extended |
|---|---|---|
| Skipped alerts | Not logged | Every gate failure → `smc_skips.csv` + GATE_FAIL row in trades |
| Slippage | Not tracked | `slippage_pct` per fill (intended_px vs fill_price) |
| Latency | Not tracked | `pine→submit` and `submit→fill` ms per trade |
| MFE/MAE | best_r only | best_r + worst_r + mfe_pct + mae_pct |
| Fees | Not tracked | `fees_usd`, `funding_paid_usd`, `net_pnl_usd` |
| HL response | Not stored | Full JSON on ARMED + REJECTED |
| Portfolio context | Not stored | concurrent_positions, equity_at_decision |
| Daily rollup | Manual | Auto nightly → `smc_daily.csv` |
| System events | Not logged | WS_STALE, HALT_TRIGGERED, ORPHAN_PRUNED |

## Files

| File | Purpose | Volume |
|---|---|---|
| `smc_trade_log.py` | Extended trade lifecycle CSV writer | ~50 events/day |
| `smc_skip_log.py` | Lightweight gate-skip ledger | ~500 events/day |
| `smc_daily_rollup.py` | Nightly aggregate generator | 1 row/day |
| `smc_position_tick_patch.py` | MFE/MAE sampling in position_tick | — |

## Output files (Render persistent disk)

```
/var/data/smc_trades.csv     # full lifecycle (47 columns)
/var/data/smc_skips.csv      # gate failures (14 columns)
/var/data/smc_daily.csv      # daily rollup (35 columns)
```

## Wire-up in smc_engine.py

```python
import time
import smc_trade_log
from smc_config import SMC_CONFIG

def handle_smc_alert(payload):
    webhook_recv_ms = int(time.time() * 1000)
    payload['webhook_recv_ms'] = webhook_recv_ms

    # Always log receipt
    smc_trade_log.log_alert_recv(payload, webhook_recv_ms)

    ctx = _build_decision_context()  # btc_trend, funding, session, equity, etc.

    gates = [
        (1, 'webhook_secret', lambda: payload.get('secret') == WEBHOOK_SECRET),
        (2, 'schema',         lambda: all(f in payload for f in REQUIRED_FIELDS)),
        (3, 'dedupe',         lambda: not dedupe_check(payload['alert_id'])),
        (4, 'short_signal',   lambda: payload['side'] != 'SELL'),
        (5, 'halt_flag',      lambda: not state.halt_flag),
        (6, 'major_excluded', lambda: payload['coin'] not in SMC_CONFIG['excluded_majors']),
        (7, 'session',        lambda: not _in_skip_session()),
        (8, 'rr_min',         lambda: payload['rr_to_tp2'] >= SMC_CONFIG['min_rr_to_take']),
        (9, 'btc_trend',      lambda: state.btc_trend_up),
        (10, 'funding_max',   lambda: ctx['funding_rate'] < SMC_CONFIG['funding_max_adverse_per_hour']),
        (11, 'position_cap',  lambda: ctx['concurrent_positions'] < SMC_CONFIG['max_concurrent_positions']),
        (12, 'coin_open',     lambda: payload['coin'] not in state.positions),
        (13, 'coin_armed',    lambda: not _coin_armed(payload['coin'])),
    ]

    for num, name, check in gates:
        try:
            if not check():
                value = _gate_value_for(num, payload, ctx)
                smc_trade_log.log_gate_fail(payload, num, name, value, ctx)
                # gate 4 short-signal additionally HALTS
                if num == 4:
                    state.halt_flag = True
                    state.halt_reason = f'short_signal_{payload["coin"]}'
                    smc_trade_log.log_system('HALT_TRIGGERED',
                                             coin=payload['coin'],
                                             reason=state.halt_reason)
                    pushover_alert(f"SMC HALT: short on {payload['coin']}")
                return {'status': f'gate_fail_{name}'}, 200
        except Exception as e:
            smc_trade_log.log_gate_fail(payload, num, name, str(e), ctx)
            return {'status': 'gate_error', 'error': str(e)}, 500

    return submit_smc_trade(payload, ctx)
```

## Wire-up in smc_execution.py

```python
def submit_smc_trade(payload, ctx):
    coin = payload['coin']
    notional = SMC_CONFIG['force_notional_usd']
    size = round_size(coin, notional / payload['ob_top'])
    trade_id = f"smc-{payload['alert_id']}"

    submit_ms = int(time.time() * 1000)
    with flight_guard.acquire(coin):
        result = atomic_entry.submit_atomic(...)

    armed = {**payload, 'trade_id': trade_id, 'size': size,
             'submit_ms': submit_ms, 'submitted_at_ms': submit_ms,
             'expires_at_ms': submit_ms + 5*3600*1000}

    if not result['accepted']:
        smc_trade_log.append({
            'event': 'REJECTED',
            'trade_id': trade_id,
            'alert_id': payload['alert_id'],
            'coin': coin,
            'submit_ms': submit_ms,
            'hl_response_json': result,
            'error': result.get('error'),
        })
        return {'status': 'submit_failed'}, 200

    state.armed[trade_id] = armed
    smc_trade_log.log_armed(armed, result, submit_ms, ctx)
    threading.Timer(SMC_CONFIG['limit_expiry_minutes']*60,
                    expire_if_unfilled, args=[trade_id]).start()
    return {'status': 'armed', 'trade_id': trade_id}, 200
```

## Wire-up on fill (hl_user_ws callback)

```python
def on_smc_fill(coin, side, size, px, ts_ms, oid, cloid):
    if not cloid: return
    armed = _match_armed_by_cloid(cloid)
    if not armed: return

    pos = {**armed,
           'state': 'OPEN',
           'fill_price': px,
           'fill_size': size,
           'fill_time_ms': ts_ms,
           'sl_orig': armed['sl_price'],
           'sl_current': armed['sl_price'],
           'best_r': 0.0, 'worst_r': 0.0,
           'mfe_pct': 0.0, 'mae_pct': 0.0,
           'be_done': False,
           'submit_ms': armed['submit_ms'],
           'intended_px': armed['ob_top']}
    state.positions[coin] = pos
    state.armed.pop(armed['trade_id'], None)
    smc_trade_log.log_filled(pos, ts_ms)
```

## Wire-up on close (hl_user_ws position-flat callback)

```python
def on_smc_position_closed(coin, exit_px, exit_ts_ms, fees_usd=0, funding_paid_usd=0):
    pos = state.positions.get(coin)
    if not pos or not pos['trade_id'].startswith('smc-'): return

    # determine exit event
    if pos.get('forced_close'):
        ev = 'CLOSED_MARKET'
        reason = pos.get('close_reason')
    elif pos.get('be_done') and exit_px <= pos['sl_current']:
        ev = 'CLOSED_BE'
        reason = 'be_buffer_hit'
    elif exit_px <= pos['sl_orig']:
        ev = 'CLOSED_SL'
        reason = 'sl_hit'
    else:
        ev = 'CLOSED_TP'
        reason = 'tp_hit'

    smc_trade_log.log_close(pos, ev, exit_px, exit_ts_ms,
                            fees_usd=fees_usd,
                            funding_paid_usd=funding_paid_usd,
                            source='ws_userFill', reason=reason)
    state.positions.pop(coin, None)
```

## Schedule (smc_monitors.py)

```python
import schedule
import smc_daily_rollup

schedule.every(15).minutes.do(position_tick)        # MFE/MAE + BE + time-stop
schedule.every().hour.do(refresh_btc_trend)
schedule.every().hour.do(refresh_funding_rates)
schedule.every().day.at("00:05").do(refresh_universe)
schedule.every().day.at("23:55").do(                # nightly rollup
    lambda: smc_daily_rollup.generate_rollup(
        current_equity=position_ledger.get_equity()))
```

## Routes (smc_app.py additions)

```python
@app.route('/smc/skips', methods=['GET'])
def skips():
    return jsonify({
        'tail': smc_skip_log.tail(int(request.args.get('n', 100))),
        'gate_breakdown_24h': smc_skip_log.gate_breakdown(
            since_ms=int(time.time()*1000) - 86_400_000),
        'coin_breakdown_24h': smc_skip_log.coin_skip_breakdown(
            since_ms=int(time.time()*1000) - 86_400_000),
    })

@app.route('/smc/daily', methods=['GET'])
def daily():
    n = int(request.args.get('n', 30))
    return jsonify(smc_daily_rollup.tail(n))

@app.route('/smc/weekly', methods=['GET'])
def weekly():
    return jsonify(smc_daily_rollup.weekly_summary(
        weeks=int(request.args.get('weeks', 4))))
```

## Verification (post-deploy smoke tests)

1. POST mock alert → expect `ALERT_RECV` row in `/smc/trades?n=5`.
2. POST same alert_id again → expect `GATE_FAIL` with `gate_reason=dedupe`.
3. POST SHORT alert → expect `GATE_FAIL` + `HALT_TRIGGERED` row + halt_flag=true.
4. After 24h live → curl `/smc/skips` → confirm gate_breakdown shows expected distribution (most should be `rr_min` or `funding_max`).
5. After first close → confirm `slippage_pct`, `mfe_pct`, `mae_pct`, `net_pnl_usd` all populated.
6. After midnight UTC → curl `/smc/daily` → confirm row exists with `cumulative_net_pnl`.

## Storage budget

| File | Row size | Daily | Annual |
|---|---|---|---|
| smc_trades.csv | ~700 B | 50 events × 700 B = 35 KB | 12.8 MB |
| smc_skips.csv | ~250 B | 500 × 250 B = 125 KB | 45.6 MB |
| smc_daily.csv | ~600 B | 1 × 600 B | 219 KB |

Total ~58 MB/year — well within Render disk limits.
