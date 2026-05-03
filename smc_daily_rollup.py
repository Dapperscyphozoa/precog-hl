"""
SMC v1.0 — Daily rollup.
Aggregates smc_trades.csv + smc_skips.csv into a daily summary row.
Run nightly via schedule.every().day.at("23:55").do(generate_rollup).

Writes /var/data/smc_daily.csv. One row per UTC day.
"""
import csv, os, time, threading
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import smc_trade_log
import smc_skip_log

ROLLUP_FILE = '/var/data/smc_daily.csv'
_lock = threading.Lock()

HEADERS = [
    'date_utc',
    # alert flow
    'alerts_recv', 'alerts_passed', 'alerts_skipped',
    'gate_kill_breakdown',          # JSON {gate_reason: count}
    # order flow
    'armed', 'rejected', 'expired', 'filled',
    # trade outcomes
    'closes_total', 'wins', 'losses', 'breakeven',
    'win_rate', 'avg_r', 'sum_r', 'best_r_day', 'worst_r_day',
    # money
    'gross_pnl_usd', 'fees_usd', 'funding_paid_usd', 'net_pnl_usd',
    # exec quality
    'avg_slippage_pct', 'avg_latency_pine_to_submit_ms', 'avg_latency_submit_to_fill_ms',
    'avg_hold_minutes',
    # exits
    'closes_tp', 'closes_sl', 'closes_be', 'closes_market',
    # mfe/mae
    'avg_mfe_pct', 'avg_mae_pct',
    # system
    'ws_stale_events', 'orphans_pruned',
    # equity
    'equity_eod', 'cumulative_net_pnl',
]


def _utc_day_bounds(date_str=None):
    """Returns (start_ms, end_ms) for given YYYY-MM-DD or today."""
    if date_str is None:
        d = datetime.now(timezone.utc).date()
    else:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), d.isoformat()


def _safe_float(v, default=0.0):
    try:
        return float(v) if v not in ('', None) else default
    except (ValueError, TypeError):
        return default


def _avg(values):
    vals = [v for v in values if v not in ('', None)]
    if not vals:
        return 0.0
    return sum(_safe_float(v) for v in vals) / len(vals)


def generate_rollup(date_str=None, current_equity=None):
    """Build aggregate row for the given UTC day. Default = today."""
    start_ms, end_ms, date_iso = _utc_day_bounds(date_str)

    # ---- alert flow ----
    alerts = smc_trade_log.filter_by(event='ALERT_RECV', since_ms=start_ms)
    alerts = [r for r in alerts if int(r.get('event_ts_ms', 0)) < end_ms]
    armed = [r for r in smc_trade_log.filter_by(event='ARMED', since_ms=start_ms)
             if int(r.get('event_ts_ms', 0)) < end_ms]
    rejected = [r for r in smc_trade_log.filter_by(event='REJECTED', since_ms=start_ms)
                if int(r.get('event_ts_ms', 0)) < end_ms]
    expired = [r for r in smc_trade_log.filter_by(event='EXPIRED', since_ms=start_ms)
               if int(r.get('event_ts_ms', 0)) < end_ms]
    filled = [r for r in smc_trade_log.filter_by(event='FILLED', since_ms=start_ms)
              if int(r.get('event_ts_ms', 0)) < end_ms]

    # gate breakdown
    skip_breakdown = {}
    for r in smc_skip_log.tail(10000):
        ts = int(r.get('event_ts_ms', 0))
        if start_ms <= ts < end_ms:
            reason = r.get('gate_reason', 'unknown')
            skip_breakdown[reason] = skip_breakdown.get(reason, 0) + 1

    # ---- closes ----
    close_events = ['CLOSED_TP', 'CLOSED_SL', 'CLOSED_BE', 'CLOSED_MARKET']
    closes = []
    for ev in close_events:
        rows = smc_trade_log.filter_by(event=ev, since_ms=start_ms)
        rows = [r for r in rows if int(r.get('event_ts_ms', 0)) < end_ms]
        closes.extend(rows)

    wins = [c for c in closes if _safe_float(c.get('pnl_r')) > 0]
    losses = [c for c in closes if _safe_float(c.get('pnl_r')) < 0]
    breakeven = [c for c in closes if _safe_float(c.get('pnl_r')) == 0]
    win_rate = (len(wins) / len(closes)) if closes else 0

    rs = [_safe_float(c.get('pnl_r')) for c in closes]
    pnls = [_safe_float(c.get('pnl_usd')) for c in closes]
    fees = [_safe_float(c.get('fees_usd')) for c in closes]
    funding = [_safe_float(c.get('funding_paid_usd')) for c in closes]
    nets = [_safe_float(c.get('net_pnl_usd')) for c in closes]

    # ---- exec quality ----
    slippages = [_safe_float(r.get('slippage_pct')) for r in filled]
    latencies_pine_submit = [_safe_float(r.get('latency_pine_to_submit_ms')) for r in armed]
    latencies_submit_fill = [_safe_float(r.get('latency_submit_to_fill_ms')) for r in filled]
    holds = [_safe_float(c.get('hold_minutes')) for c in closes]

    # ---- system ----
    ws_stale = [r for r in smc_trade_log.filter_by(event='WS_STALE', since_ms=start_ms)
                if int(r.get('event_ts_ms', 0)) < end_ms]
    orphans = [r for r in smc_trade_log.filter_by(event='ORPHAN_PRUNED', since_ms=start_ms)
               if int(r.get('event_ts_ms', 0)) < end_ms]

    # ---- cumulative net pnl ----
    cum_net = _cumulative_net_pnl_through(end_ms)

    row = {
        'date_utc': date_iso,
        'alerts_recv': len(alerts),
        'alerts_passed': len(armed),
        'alerts_skipped': sum(skip_breakdown.values()),
        'gate_kill_breakdown': skip_breakdown,
        'armed': len(armed),
        'rejected': len(rejected),
        'expired': len(expired),
        'filled': len(filled),
        'closes_total': len(closes),
        'wins': len(wins),
        'losses': len(losses),
        'breakeven': len(breakeven),
        'win_rate': round(win_rate, 4),
        'avg_r': round(_avg(rs), 3),
        'sum_r': round(sum(rs), 3),
        'best_r_day': round(max(rs) if rs else 0, 3),
        'worst_r_day': round(min(rs) if rs else 0, 3),
        'gross_pnl_usd': round(sum(pnls), 4),
        'fees_usd': round(sum(fees), 4),
        'funding_paid_usd': round(sum(funding), 4),
        'net_pnl_usd': round(sum(nets), 4),
        'avg_slippage_pct': round(_avg(slippages), 4),
        'avg_latency_pine_to_submit_ms': round(_avg(latencies_pine_submit), 0),
        'avg_latency_submit_to_fill_ms': round(_avg(latencies_submit_fill), 0),
        'avg_hold_minutes': round(_avg(holds), 2),
        'closes_tp': sum(1 for c in closes if c.get('event') == 'CLOSED_TP'),
        'closes_sl': sum(1 for c in closes if c.get('event') == 'CLOSED_SL'),
        'closes_be': sum(1 for c in closes if c.get('event') == 'CLOSED_BE'),
        'closes_market': sum(1 for c in closes if c.get('event') == 'CLOSED_MARKET'),
        'avg_mfe_pct': round(_avg([c.get('mfe_pct') for c in closes]), 4),
        'avg_mae_pct': round(_avg([c.get('mae_pct') for c in closes]), 4),
        'ws_stale_events': len(ws_stale),
        'orphans_pruned': len(orphans),
        'equity_eod': current_equity if current_equity is not None else '',
        'cumulative_net_pnl': round(cum_net, 4),
    }
    _write_row(row)
    return row


def _cumulative_net_pnl_through(end_ms):
    """Sum net_pnl_usd across all closes up to end_ms."""
    if not os.path.exists(smc_trade_log.CSV_FILE):
        return 0.0
    total = 0.0
    with open(smc_trade_log.CSV_FILE, 'r') as f:
        for r in csv.DictReader(f):
            ev = r.get('event', '')
            if ev.startswith('CLOSED_'):
                ts = int(r.get('event_ts_ms', 0) or 0)
                if ts < end_ms:
                    total += _safe_float(r.get('net_pnl_usd'))
    return total


def _write_row(row):
    import json
    if isinstance(row.get('gate_kill_breakdown'), dict):
        row['gate_kill_breakdown'] = json.dumps(row['gate_kill_breakdown'])
    with _lock:
        os.makedirs(os.path.dirname(ROLLUP_FILE), exist_ok=True)
        new_file = not os.path.exists(ROLLUP_FILE)
        # rewrite if today's row already exists (re-runs are idempotent)
        existing = []
        if not new_file:
            with open(ROLLUP_FILE, 'r') as f:
                existing = list(csv.DictReader(f))
            existing = [r for r in existing if r.get('date_utc') != row['date_utc']]
        existing.append(row)
        with open(ROLLUP_FILE, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=HEADERS, extrasaction='ignore')
            w.writeheader()
            for r in existing:
                w.writerow(r)


def tail(n=30):
    if not os.path.exists(ROLLUP_FILE):
        return []
    with _lock, open(ROLLUP_FILE, 'r') as f:
        rows = list(csv.DictReader(f))
    return rows[-n:]


def weekly_summary(weeks=4):
    """Aggregate last N weeks. Returns list of {iso_week, ...}."""
    rows = tail(weeks * 7 + 7)
    by_week = defaultdict(lambda: {
        'alerts_recv': 0, 'armed': 0, 'closes_total': 0, 'wins': 0, 'losses': 0,
        'sum_r': 0.0, 'net_pnl_usd': 0.0, 'fees_usd': 0.0,
    })
    for r in rows:
        d = datetime.strptime(r['date_utc'], '%Y-%m-%d').date()
        iso_year, iso_week, _ = d.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
        agg = by_week[key]
        agg['alerts_recv'] += int(r.get('alerts_recv') or 0)
        agg['armed'] += int(r.get('armed') or 0)
        agg['closes_total'] += int(r.get('closes_total') or 0)
        agg['wins'] += int(r.get('wins') or 0)
        agg['losses'] += int(r.get('losses') or 0)
        agg['sum_r'] += _safe_float(r.get('sum_r'))
        agg['net_pnl_usd'] += _safe_float(r.get('net_pnl_usd'))
        agg['fees_usd'] += _safe_float(r.get('fees_usd'))
    out = []
    for k in sorted(by_week.keys()):
        agg = by_week[k]
        agg['iso_week'] = k
        agg['win_rate'] = (agg['wins'] / agg['closes_total']) if agg['closes_total'] else 0
        out.append(agg)
    return out[-weeks:]
