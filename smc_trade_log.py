"""
SMC v1.0 — Extended trade lifecycle logger.
Single source of truth for every event in a trade's life.
File: /var/data/smc_trades.csv
"""
import csv, os, time, json, threading

CSV_FILE = '/var/data/smc_trades.csv'
_lock = threading.Lock()

EVENTS = [
    # alert lifecycle
    'ALERT_RECV', 'GATE_PASS', 'GATE_FAIL',
    # order lifecycle
    'ARMED', 'REJECTED', 'EXPIRED',
    'FILLED', 'BE_MOVED',
    'CLOSED_TP', 'CLOSED_SL', 'CLOSED_BE', 'CLOSED_MARKET',
    # system
    'WS_STALE', 'HALT_TRIGGERED', 'UNHALT',
    'ORPHAN_PRUNED', 'RECONCILE_MISMATCH',
]

HEADERS = [
    # identity
    'event_ts_ms', 'event', 'trade_id', 'alert_id', 'coin',
    # gate trace (for ALERT_RECV / GATE_FAIL only)
    'gate_failed', 'gate_reason', 'gate_value',
    # signal payload
    'side', 'ob_top', 'sl_orig', 'tp1', 'tp2',
    'sweep_wick', 'atr14', 'rr_to_tp2', 'mss_close_ms',
    # context at decision
    'btc_trend_up', 'btc_trend_age_min', 'funding_rate',
    'session_utc_hour', 'concurrent_positions', 'equity_at_decision',
    # latency
    'webhook_recv_ms', 'submit_ms', 'fill_ms',
    'latency_pine_to_submit_ms', 'latency_submit_to_fill_ms',
    # execution
    'size', 'notional_usd', 'intended_px', 'fill_price', 'slippage_pct',
    # exit
    'exit_price', 'exit_time_ms', 'hold_minutes',
    'pnl_usd', 'pnl_r', 'fees_usd', 'funding_paid_usd', 'net_pnl_usd',
    'best_r', 'worst_r', 'mfe_pct', 'mae_pct',
    'sl_current_at_close', 'reason', 'close_event_source',
    # raw
    'hl_response_json', 'error',
]


def append(row):
    """Append a single event row. Missing fields are blank."""
    row.setdefault('event_ts_ms', int(time.time() * 1000))
    with _lock:
        os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
        new_file = not os.path.exists(CSV_FILE)
        with open(CSV_FILE, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=HEADERS, extrasaction='ignore')
            if new_file:
                w.writeheader()
            # serialise dicts/lists to JSON
            for k, v in list(row.items()):
                if isinstance(v, (dict, list)):
                    row[k] = json.dumps(v, separators=(',', ':'))
            w.writerow(row)


def tail(n=100):
    if not os.path.exists(CSV_FILE):
        return []
    with _lock:
        with open(CSV_FILE, 'r') as f:
            rows = list(csv.DictReader(f))
    return rows[-n:]


def filter_by(event=None, coin=None, since_ms=None):
    """Pull subset for analysis. Returns list of dicts."""
    if not os.path.exists(CSV_FILE):
        return []
    out = []
    with _lock, open(CSV_FILE, 'r') as f:
        for r in csv.DictReader(f):
            if event and r.get('event') != event:
                continue
            if coin and r.get('coin') != coin:
                continue
            if since_ms and int(r.get('event_ts_ms') or 0) < since_ms:
                continue
            out.append(r)
    return out


# ---------- helpers for hot-path logging ----------

def log_alert_recv(payload, webhook_recv_ms):
    append({
        'event': 'ALERT_RECV',
        'alert_id': payload.get('alert_id'),
        'coin': payload.get('coin'),
        'side': payload.get('side'),
        'ob_top': payload.get('ob_top'),
        'sl_orig': payload.get('sl_price'),
        'tp1': payload.get('tp1'),
        'tp2': payload.get('tp2'),
        'sweep_wick': payload.get('sweep_wick'),
        'atr14': payload.get('atr14'),
        'rr_to_tp2': payload.get('rr_to_tp2'),
        'mss_close_ms': payload.get('mss_close_ms'),
        'webhook_recv_ms': webhook_recv_ms,
        'latency_pine_to_submit_ms': webhook_recv_ms - int(payload.get('mss_close_ms', webhook_recv_ms)),
    })


def log_gate_fail(payload, gate_num, gate_name, value, ctx=None):
    """Lightweight skip log. Mirrored to smc_skip_log for high-volume tail-ability."""
    row = {
        'event': 'GATE_FAIL',
        'alert_id': payload.get('alert_id'),
        'coin': payload.get('coin'),
        'side': payload.get('side'),
        'gate_failed': gate_num,
        'gate_reason': gate_name,
        'gate_value': value,
    }
    if ctx:
        row.update({
            'btc_trend_up': ctx.get('btc_trend_up'),
            'funding_rate': ctx.get('funding_rate'),
            'session_utc_hour': ctx.get('session_utc_hour'),
            'concurrent_positions': ctx.get('concurrent_positions'),
            'equity_at_decision': ctx.get('equity_at_decision'),
        })
    append(row)
    # mirror to skip log
    try:
        from smc_skip_log import append_skip
        append_skip(row)
    except ImportError:
        pass


def log_armed(armed, hl_response, submit_ms, ctx):
    append({
        'event': 'ARMED',
        'trade_id': armed['trade_id'],
        'alert_id': armed['alert_id'],
        'coin': armed['coin'],
        'side': armed['side'],
        'ob_top': armed['ob_top'],
        'sl_orig': armed['sl_price'],
        'tp1': armed.get('tp1'),
        'tp2': armed['tp2'],
        'atr14': armed.get('atr14'),
        'rr_to_tp2': armed.get('rr_to_tp2'),
        'mss_close_ms': armed.get('mss_close_ms'),
        'btc_trend_up': ctx.get('btc_trend_up'),
        'btc_trend_age_min': ctx.get('btc_trend_age_min'),
        'funding_rate': ctx.get('funding_rate'),
        'session_utc_hour': ctx.get('session_utc_hour'),
        'concurrent_positions': ctx.get('concurrent_positions'),
        'equity_at_decision': ctx.get('equity_at_decision'),
        'submit_ms': submit_ms,
        'latency_pine_to_submit_ms': submit_ms - int(armed.get('mss_close_ms', submit_ms)),
        'size': armed['size'],
        'notional_usd': armed['size'] * armed['ob_top'],
        'intended_px': armed['ob_top'],
        'hl_response_json': hl_response,
    })


def log_filled(pos, fill_ms):
    submit_ms = pos.get('submit_ms') or fill_ms
    intended = pos.get('intended_px') or pos.get('ob_top')
    fill_px = pos.get('fill_price')
    slip = ((fill_px - intended) / intended * 100) if intended and fill_px else 0
    append({
        'event': 'FILLED',
        'trade_id': pos['trade_id'],
        'alert_id': pos.get('alert_id'),
        'coin': pos['coin'],
        'fill_price': fill_px,
        'intended_px': intended,
        'slippage_pct': round(slip, 4),
        'fill_ms': fill_ms,
        'latency_submit_to_fill_ms': fill_ms - submit_ms,
        'size': pos['fill_size'],
        'notional_usd': pos['fill_size'] * fill_px,
    })


def log_be_moved(pos, new_sl):
    append({
        'event': 'BE_MOVED',
        'trade_id': pos['trade_id'],
        'coin': pos['coin'],
        'sl_current_at_close': new_sl,
        'best_r': pos.get('best_r'),
    })


def log_close(pos, exit_event, exit_px, exit_time_ms, fees_usd=0, funding_paid_usd=0,
              source='ws_userFill', reason=None):
    """exit_event ∈ CLOSED_TP / CLOSED_SL / CLOSED_BE / CLOSED_MARKET"""
    risk = abs(pos['fill_price'] - pos['sl_orig']) or 1e-9
    pnl_per_unit = exit_px - pos['fill_price']  # long-only
    pnl_usd = pnl_per_unit * pos['fill_size']
    pnl_r = pnl_per_unit / risk
    hold_min = (exit_time_ms - pos['fill_time_ms']) / 60000
    net = pnl_usd - fees_usd - funding_paid_usd
    append({
        'event': exit_event,
        'trade_id': pos['trade_id'],
        'alert_id': pos.get('alert_id'),
        'coin': pos['coin'],
        'fill_price': pos['fill_price'],
        'exit_price': exit_px,
        'exit_time_ms': exit_time_ms,
        'hold_minutes': round(hold_min, 2),
        'pnl_usd': round(pnl_usd, 4),
        'pnl_r': round(pnl_r, 3),
        'fees_usd': round(fees_usd, 4),
        'funding_paid_usd': round(funding_paid_usd, 4),
        'net_pnl_usd': round(net, 4),
        'best_r': round(pos.get('best_r', 0), 3),
        'worst_r': round(pos.get('worst_r', 0), 3),
        'mfe_pct': round(pos.get('mfe_pct', 0), 4),
        'mae_pct': round(pos.get('mae_pct', 0), 4),
        'sl_current_at_close': pos.get('sl_current'),
        'reason': reason,
        'close_event_source': source,
    })


def log_system(event, **fields):
    """WS_STALE, HALT_TRIGGERED, UNHALT, ORPHAN_PRUNED, RECONCILE_MISMATCH."""
    append({'event': event, **fields})
