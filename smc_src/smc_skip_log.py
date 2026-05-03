"""
SMC v1.0 — Skip log. Lightweight gate-rejection ledger.
Separate from smc_trades.csv so high-volume skips don't drown the trade audit trail.
File: /var/data/smc_skips.csv
"""
import csv, os, time, threading

SKIP_FILE = '/var/data/smc_skips.csv'
_lock = threading.Lock()

HEADERS = [
    'event_ts_ms', 'alert_id', 'coin', 'side',
    'gate_failed', 'gate_reason', 'gate_value',
    'btc_trend_up', 'funding_rate', 'session_utc_hour',
    'concurrent_positions', 'equity_at_decision',
    'rr_to_tp2', 'atr14',
]


def append_skip(row):
    row = {k: row.get(k, '') for k in HEADERS}
    row['event_ts_ms'] = row.get('event_ts_ms') or int(time.time() * 1000)
    with _lock:
        os.makedirs(os.path.dirname(SKIP_FILE), exist_ok=True)
        new_file = not os.path.exists(SKIP_FILE)
        with open(SKIP_FILE, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=HEADERS, extrasaction='ignore')
            if new_file:
                w.writeheader()
            w.writerow(row)


def gate_breakdown(since_ms=None):
    """Count skips by gate_reason. Returns {reason: count}."""
    if not os.path.exists(SKIP_FILE):
        return {}
    out = {}
    with _lock, open(SKIP_FILE, 'r') as f:
        for r in csv.DictReader(f):
            if since_ms and int(r.get('event_ts_ms') or 0) < since_ms:
                continue
            reason = r.get('gate_reason', 'unknown')
            out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items(), key=lambda x: -x[1]))


def coin_skip_breakdown(since_ms=None):
    """Which coins skipped most? {coin: count}."""
    if not os.path.exists(SKIP_FILE):
        return {}
    out = {}
    with _lock, open(SKIP_FILE, 'r') as f:
        for r in csv.DictReader(f):
            if since_ms and int(r.get('event_ts_ms') or 0) < since_ms:
                continue
            c = r.get('coin', 'unknown')
            out[c] = out.get(c, 0) + 1
    return dict(sorted(out.items(), key=lambda x: -x[1]))


def tail(n=200):
    if not os.path.exists(SKIP_FILE):
        return []
    with _lock, open(SKIP_FILE, 'r') as f:
        rows = list(csv.DictReader(f))
    return rows[-n:]
