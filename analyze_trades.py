#!/usr/bin/env python3
"""Offline trade analyzer.

Reads a trades.csv (the trade_ledger output) and produces per-bucket breakdowns
with Wilson confidence intervals on win rate.

Usage:
    python3 analyze_trades.py [path/to/trades.csv]

Default path: /var/data/trades.csv (production location). Pull the file off
Render with `render shell <service>` and `cat /var/data/trades.csv` then save
locally before running.

Buckets:
    by_engine, by_coin, by_hour, by_side, by_close_reason, by_hold_bucket
    edge_vs_outcome (only if expected_edge_at_entry was logged)

Each bucket reports:
    n             — sample size
    wr_pct        — realized win rate
    wilson_95     — [lo, hi] 95% confidence interval on WR
    mean_pnl      — mean realized PnL (USD)
    sum_pnl       — total realized PnL (USD)
    wins/losses   — counts
    funding_share — % of bucket trades with non-zero funding_paid_pct (instrumentation health)

Wilson CI is the right interval for proportions on small samples. Don't trust a
WR number without it. If the lower bound of the 95% CI is below 50%, you do
not have evidence of edge yet — keep collecting trades.
"""
import csv
import math
import os
import sys
from collections import defaultdict
from datetime import datetime


def wilson_ci(wins, n, z=1.96):
    """Wilson score interval for a binomial proportion. Returns (lo, hi) in [0,1]."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None


def to_float(s, default=None):
    if s is None or s == '':
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def hold_bucket(seconds):
    """Bucket holds into human-readable bands."""
    if seconds is None or seconds < 0:
        return 'unknown'
    m = seconds / 60.0
    if m < 5: return '0_5m'
    if m < 15: return '5_15m'
    if m < 60: return '15_60m'
    if m < 4 * 60: return '1_4h'
    if m < 12 * 60: return '4_12h'
    return '12h_plus'


def edge_bucket(edge):
    """Bucket expected_edge_at_entry into bands. Edge is a fraction (0.001 = 10bps)."""
    if edge is None:
        return 'unknown'
    bps = edge * 10000.0
    if bps < 0: return 'neg'
    if bps < 5: return '0_5bps'
    if bps < 20: return '5_20bps'
    if bps < 50: return '20_50bps'
    if bps < 100: return '50_100bps'
    return '100bps_plus'


def load_trades(path):
    """Load CSV and join ENTRY+CLOSE on trade_id. Returns list of completed trades.

    Each completed trade is a dict with:
      trade_id, coin, engine, side, entry_price, exit_price, pnl,
      close_reason, sl_pct, tp_pct, expected_edge_at_entry, funding_paid_pct,
      entry_ts, close_ts, hold_sec
    """
    if not os.path.exists(path):
        print(f"[analyze_trades] no file at {path}", file=sys.stderr)
        return []
    entries = {}
    completed = []
    with open(path, 'r', newline='') as f:
        for r in csv.DictReader(f):
            tid = (r.get('trade_id') or '').strip()
            if not tid:
                continue
            ev = (r.get('event_type') or '').strip().upper()
            ts = parse_iso(r.get('timestamp', ''))
            if ev == 'ENTRY':
                entries[tid] = {
                    'trade_id': tid,
                    'coin': r.get('coin', ''),
                    'engine': r.get('engine', '') or 'UNKNOWN',
                    'side': r.get('side', ''),
                    'entry_price': to_float(r.get('entry_price')),
                    'sl_pct': to_float(r.get('sl_pct')),
                    'tp_pct': to_float(r.get('tp_pct')),
                    'expected_edge_at_entry': to_float(r.get('expected_edge_at_entry')),
                    'entry_ts': ts,
                }
            elif ev == 'ENTRY_UPDATE':
                # Post-fill protection params landing after enforce_protection.
                # Merge into the canonical ENTRY record without disturbing
                # entry_price / entry_ts (those are immutable post-fill).
                e = entries.get(tid)
                if e is None:
                    continue
                for k in ('sl_pct', 'tp_pct', 'expected_edge_at_entry'):
                    v = to_float(r.get(k))
                    if v is not None:
                        e[k] = v
            elif ev == 'CLOSE':
                e = entries.pop(tid, None)
                if not e:
                    continue
                close_ts = ts
                hold_sec = None
                if e['entry_ts'] and close_ts:
                    hold_sec = (close_ts - e['entry_ts']).total_seconds()
                completed.append({
                    **e,
                    'exit_price': to_float(r.get('exit_price')),
                    'pnl': to_float(r.get('pnl'), 0.0),
                    'close_reason': r.get('close_reason', '') or 'unknown',
                    'funding_paid_pct': to_float(r.get('funding_paid_pct')),
                    'close_ts': close_ts,
                    'hold_sec': hold_sec,
                })
    return completed


def bucket_stats(trades, key_fn):
    """Group trades by key_fn(trade) and compute per-bucket stats."""
    buckets = defaultdict(list)
    for t in trades:
        k = key_fn(t)
        buckets[k].append(t)
    out = {}
    for k, ts in buckets.items():
        n = len(ts)
        pnls = [t['pnl'] or 0.0 for t in ts]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        wr = wins / n if n else 0.0
        lo, hi = wilson_ci(wins, n)
        funding_present = sum(1 for t in ts if t.get('funding_paid_pct') is not None)
        out[k] = {
            'n': n,
            'wins': wins,
            'losses': losses,
            'wr_pct': round(wr * 100, 1),
            'wilson_95_lo_pct': round(lo * 100, 1),
            'wilson_95_hi_pct': round(hi * 100, 1),
            'mean_pnl': round(sum(pnls) / n, 4) if n else 0.0,
            'sum_pnl': round(sum(pnls), 4),
            'funding_logged_pct': round(funding_present / n * 100, 1) if n else 0.0,
        }
    return out


def hour_of_day(t):
    ts = t.get('entry_ts')
    if not ts:
        return 'unknown'
    return f"{ts.hour:02d}"


def render(title, stats):
    if not stats:
        return
    print(f"\n=== {title} ===")
    rows = sorted(stats.items(), key=lambda kv: -kv[1]['n'])
    width = max(8, max(len(str(k)) for k, _ in rows))
    print(f"{'bucket'.ljust(width)}  {'n':>5}  {'wr%':>5}  "
          f"{'wilson_95%':>13}  {'mean_pnl':>10}  {'sum_pnl':>11}  {'fund%':>6}")
    for k, s in rows:
        wilson = f"[{s['wilson_95_lo_pct']:.1f},{s['wilson_95_hi_pct']:.1f}]"
        print(f"{str(k).ljust(width)}  {s['n']:>5}  {s['wr_pct']:>5.1f}  "
              f"{wilson:>13}  {s['mean_pnl']:>10.4f}  {s['sum_pnl']:>11.4f}  "
              f"{s['funding_logged_pct']:>6.1f}")


def header_summary(trades):
    n = len(trades)
    if n == 0:
        return
    pnls = [t['pnl'] or 0.0 for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / n
    lo, hi = wilson_ci(wins, n)
    total = sum(pnls)
    funding_logged = sum(1 for t in trades if t.get('funding_paid_pct') is not None)
    edge_logged = sum(1 for t in trades if t.get('expected_edge_at_entry') is not None)
    holds_known = [t['hold_sec'] for t in trades if t.get('hold_sec') is not None]
    median_hold = (sorted(holds_known)[len(holds_known) // 2] if holds_known else None)

    print("=" * 64)
    print(f"  TRADES: {n}")
    print(f"  WIN RATE: {wr*100:.1f}%   wilson_95=[{lo*100:.1f}, {hi*100:.1f}]")
    print(f"  TOTAL PnL: {total:.4f} USD   mean={total/n:.4f}")
    if median_hold is not None:
        print(f"  MEDIAN HOLD: {median_hold/60:.1f} min")
    print(f"  Edge logged: {edge_logged}/{n} ({edge_logged/n*100:.0f}%)   "
          f"Funding logged: {funding_logged}/{n} ({funding_logged/n*100:.0f}%)")
    if lo < 0.5:
        print("  >>> Wilson lower bound <50% — WR is not yet distinguishable from a coin flip.")
    print("=" * 64)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else '/var/data/trades.csv'
    trades = load_trades(path)
    if not trades:
        print(f"No completed trades in {path}", file=sys.stderr)
        sys.exit(0 if os.path.exists(path) else 1)

    header_summary(trades)
    render('by_engine', bucket_stats(trades, lambda t: t.get('engine') or 'UNKNOWN'))
    render('by_coin', bucket_stats(trades, lambda t: t.get('coin') or '?'))
    render('by_side', bucket_stats(trades, lambda t: t.get('side') or '?'))
    render('by_close_reason', bucket_stats(trades, lambda t: t.get('close_reason') or '?'))
    render('by_hold_bucket', bucket_stats(trades, lambda t: hold_bucket(t.get('hold_sec'))))
    render('by_hour_utc', bucket_stats(trades, hour_of_day))
    render('by_expected_edge_band',
           bucket_stats(trades, lambda t: edge_bucket(t.get('expected_edge_at_entry'))))


if __name__ == '__main__':
    main()
