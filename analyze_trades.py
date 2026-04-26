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
                    'engine': r.get('engine', '') or 'untagged_legacy',
                    'side': r.get('side', ''),
                    'entry_price': to_float(r.get('entry_price')),
                    'sl_pct': to_float(r.get('sl_pct')),
                    'tp_pct': to_float(r.get('tp_pct')),
                    'expected_edge_at_entry': to_float(r.get('expected_edge_at_entry')),
                    'regime': (r.get('regime') or '').strip() or None,
                    'realized_slippage_pct': to_float(r.get('realized_slippage_pct')),
                    'entry_ts': ts,
                }
            elif ev == 'ENTRY_UPDATE':
                # Post-fill protection params landing after enforce_protection.
                # Merge into the canonical ENTRY record without disturbing
                # entry_ts (immutable post-fill). entry_price IS updateable
                # — confluence pre-writes signal entry, then ENTRY_UPDATE
                # corrects it to the actual fill price.
                e = entries.get(tid)
                if e is None:
                    continue
                for k in ('sl_pct', 'tp_pct', 'expected_edge_at_entry',
                          'realized_slippage_pct', 'entry_price'):
                    v = to_float(r.get(k))
                    if v is not None:
                        e[k] = v
                # regime is a string, not a float
                _new_regime = (r.get('regime') or '').strip()
                if _new_regime:
                    e['regime'] = _new_regime
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
                    # 2026-04-26: pnl default None (not 0). A close with no
                    # pnl recorded is UNKNOWN, not a breakeven. Conflating
                    # the two understated WR — saw it on CONFLUENCE_SWING:
                    # 1W/1L/1unknown was reading as 33% WR instead of 50%.
                    'pnl': to_float(r.get('pnl')),
                    'close_reason': r.get('close_reason', '') or 'unknown',
                    'funding_paid_pct': to_float(r.get('funding_paid_pct')),
                    'mfe_pct': to_float(r.get('mfe_pct')),
                    'mae_pct': to_float(r.get('mae_pct')),
                    'close_ts': close_ts,
                    'hold_sec': hold_sec,
                })
    return completed


def bucket_stats(trades, key_fn):
    """Group trades by key_fn(trade) and compute per-bucket stats.

    2026-04-26: WR computed as wins / (wins + losses). Breakevens and
    unknowns excluded from the denominator — a flat-PnL trade isn't
    a loss, and a trade with no recorded pnl shouldn't be counted at all.
    Wilson CI bounds the WR estimate over the DECIDED population only.
    """
    buckets = defaultdict(list)
    for t in trades:
        k = key_fn(t)
        buckets[k].append(t)
    out = {}
    for k, ts in buckets.items():
        n = len(ts)
        # Separate decided/breakeven/unknown
        pnls_known = [t['pnl'] for t in ts if t.get('pnl') is not None]
        unknown = n - len(pnls_known)
        wins = sum(1 for p in pnls_known if p > 0)
        losses = sum(1 for p in pnls_known if p < 0)
        breakeven = sum(1 for p in pnls_known if p == 0)
        decided = wins + losses
        wr = (wins / decided) if decided else None
        lo, hi = wilson_ci(wins, decided) if decided else (0.0, 0.0)
        funding_present = sum(1 for t in ts if t.get('funding_paid_pct') is not None)
        out[k] = {
            'n': n,
            'wins': wins,
            'losses': losses,
            'breakeven': breakeven,
            'unknown': unknown,
            'wr_pct': round(wr * 100, 1) if wr is not None else None,
            'wilson_95_lo_pct': round(lo * 100, 1) if decided else None,
            'wilson_95_hi_pct': round(hi * 100, 1) if decided else None,
            'mean_pnl': round(sum(pnls_known) / len(pnls_known), 4) if pnls_known else 0.0,
            'sum_pnl': round(sum(pnls_known), 4),
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
    # n = total / w/l/be/un = decided/breakeven/unknown breakdown
    print(f"{'bucket'.ljust(width)}  {'n':>4} {'w':>3}/{'l':>3}/{'be':>3}/{'un':>3}  "
          f"{'wr%':>5}  {'wilson_95%':>13}  {'mean_pnl':>10}  {'sum_pnl':>11}  {'fund%':>6}")
    for k, s in rows:
        wr_str = f"{s['wr_pct']:>5.1f}" if s.get('wr_pct') is not None else "  —  "
        if s.get('wilson_95_lo_pct') is not None:
            wilson = f"[{s['wilson_95_lo_pct']:.1f},{s['wilson_95_hi_pct']:.1f}]"
        else:
            wilson = "      —      "
        print(f"{str(k).ljust(width)}  {s['n']:>4} {s['wins']:>3}/{s['losses']:>3}/{s.get('breakeven',0):>3}/{s.get('unknown',0):>3}  "
              f"{wr_str}  {wilson:>13}  {s['mean_pnl']:>10.4f}  {s['sum_pnl']:>11.4f}  "
              f"{s['funding_logged_pct']:>6.1f}")


def header_summary(trades):
    n = len(trades)
    if n == 0:
        return
    pnls_known = [t['pnl'] for t in trades if t.get('pnl') is not None]
    unknown = n - len(pnls_known)
    wins = sum(1 for p in pnls_known if p > 0)
    losses = sum(1 for p in pnls_known if p < 0)
    breakeven = sum(1 for p in pnls_known if p == 0)
    decided = wins + losses
    wr = (wins / decided) if decided else None
    lo, hi = wilson_ci(wins, decided) if decided else (0.0, 0.0)
    total = sum(pnls_known)
    funding_logged = sum(1 for t in trades if t.get('funding_paid_pct') is not None)
    edge_logged = sum(1 for t in trades if t.get('expected_edge_at_entry') is not None)
    holds_known = [t['hold_sec'] for t in trades if t.get('hold_sec') is not None]
    median_hold = (sorted(holds_known)[len(holds_known) // 2] if holds_known else None)

    print("=" * 64)
    print(f"  TRADES: {n}  (decided={decided} W={wins} L={losses} BE={breakeven} unknown={unknown})")
    if wr is not None:
        print(f"  WIN RATE: {wr*100:.1f}%   wilson_95=[{lo*100:.1f}, {hi*100:.1f}]   (over decided trades)")
    else:
        print(f"  WIN RATE: —   no decided trades to compute over")
    print(f"  TOTAL PnL: {total:.4f} USD   mean={(total/len(pnls_known)):.4f} per known trade" if pnls_known else f"  TOTAL PnL: 0   no pnl recorded")
    if median_hold is not None:
        print(f"  MEDIAN HOLD: {median_hold/60:.1f} min")
    print(f"  Edge logged: {edge_logged}/{n} ({edge_logged/n*100:.0f}%)   "
          f"Funding logged: {funding_logged}/{n} ({funding_logged/n*100:.0f}%)")
    if wr is not None and lo < 0.5:
        print("  >>> Wilson lower bound <50% — WR is not yet distinguishable from a coin flip.")
    if unknown > 0:
        print(f"  >>> {unknown} trades have no recorded pnl — data quality issue, not counted in WR.")
    print("=" * 64)


# RECONCILED + untagged_legacy = bookkeeping artifacts, not strategy decisions.
# Excluded from strategy-quality analysis. Reported separately.
NOISE_ENGINES = {'RECONCILED', 'untagged_legacy', 'UNKNOWN', ''}

def _engine_label(raw):
    r = (raw or '').strip()
    if not r:
        return 'untagged_legacy'
    return r

def is_noise_engine(name):
    return _engine_label(name) in NOISE_ENGINES


def _excursion_bucket(t):
    """Distinguish 'good entry / bad exit' from 'bad entry'."""
    mfe = t.get('mfe_pct')
    mae = t.get('mae_pct')
    pnl = t.get('pnl') or 0
    if mfe is None and mae is None:
        return 'no_data'
    if mfe is not None and mfe > 0.005 and pnl <= 0:
        return 'hit_mfe_then_reversed'
    if mfe is not None and mfe < 0.001 and pnl < 0:
        return 'bad_entry_no_mfe'
    if mae is not None and mae < -0.01 and pnl > 0:
        return 'survived_deep_mae'
    if pnl > 0:
        return 'clean_win'
    return 'clean_loss'


def _summary_dict(trades):
    """Same numbers header_summary prints, returned as a dict."""
    n = len(trades)
    if n == 0:
        return {'n': 0, 'wr_pct': None, 'wilson_95_lo_pct': None,
                'wilson_95_hi_pct': None, 'sum_pnl': 0.0, 'mean_pnl': 0.0,
                'wins': 0, 'losses': 0, 'breakeven': 0, 'unknown': 0,
                'decided': 0, 'median_hold_min': None,
                'edge_logged_pct': 0.0, 'funding_logged_pct': 0.0}
    pnls_known = [t['pnl'] for t in trades if t.get('pnl') is not None]
    unknown = n - len(pnls_known)
    wins = sum(1 for p in pnls_known if p > 0)
    losses = sum(1 for p in pnls_known if p < 0)
    breakeven = sum(1 for p in pnls_known if p == 0)
    decided = wins + losses
    wr = (wins / decided) if decided else None
    lo, hi = wilson_ci(wins, decided) if decided else (0.0, 0.0)
    funding_logged = sum(1 for t in trades if t.get('funding_paid_pct') is not None)
    edge_logged = sum(1 for t in trades if t.get('expected_edge_at_entry') is not None)
    holds_known = [t['hold_sec'] for t in trades if t.get('hold_sec') is not None]
    median_hold = (sorted(holds_known)[len(holds_known) // 2] if holds_known else None)
    return {
        'n': n, 'decided': decided,
        'wins': wins, 'losses': losses, 'breakeven': breakeven, 'unknown': unknown,
        'wr_pct': round(wr * 100, 1) if wr is not None else None,
        'wilson_95_lo_pct': round(lo * 100, 1) if decided else None,
        'wilson_95_hi_pct': round(hi * 100, 1) if decided else None,
        'sum_pnl': round(sum(pnls_known), 4),
        'mean_pnl': round(sum(pnls_known) / len(pnls_known), 4) if pnls_known else 0.0,
        'median_hold_min': round(median_hold / 60.0, 1) if median_hold is not None else None,
        'edge_logged_pct': round(edge_logged / n * 100, 1),
        'funding_logged_pct': round(funding_logged / n * 100, 1),
    }


def analyze_to_dict(path='/var/data/trades.csv', since_ts=None):
    """Programmatic entrypoint — returns the same analysis the CLI prints,
    structured as a dict ready for JSON serialization.

    `since_ts`: optional unix-seconds floor. Trades with entry_ts older are
    dropped — useful for windowing post-deploy data only.
    """
    trades = load_trades(path)
    if since_ts is not None:
        cutoff = datetime.fromtimestamp(float(since_ts))
        trades = [t for t in trades if t.get('entry_ts') and t['entry_ts'] >= cutoff]
    real_trades = [t for t in trades if not is_noise_engine(t.get('engine'))]
    noise_trades = [t for t in trades if is_noise_engine(t.get('engine'))]
    return {
        'path': path,
        'since_ts': since_ts,
        'total_trades': len(trades),
        'real_trades': len(real_trades),
        'noise_trades': len(noise_trades),
        'real_summary': _summary_dict(real_trades),
        'noise_summary': _summary_dict(noise_trades),
        'real_by_engine':         bucket_stats(real_trades, lambda t: _engine_label(t.get('engine'))),
        'real_by_coin':           bucket_stats(real_trades, lambda t: t.get('coin') or '?'),
        'real_by_side':           bucket_stats(real_trades, lambda t: t.get('side') or '?'),
        'real_by_close_reason':   bucket_stats(real_trades, lambda t: t.get('close_reason') or '?'),
        'real_by_hold_bucket':    bucket_stats(real_trades, lambda t: hold_bucket(t.get('hold_sec'))),
        'real_by_hour_utc':       bucket_stats(real_trades, hour_of_day),
        'real_by_expected_edge_band': bucket_stats(real_trades, lambda t: edge_bucket(t.get('expected_edge_at_entry'))),
        'real_by_regime':         bucket_stats(real_trades, lambda t: t.get('regime') or 'unknown'),
        'real_by_excursion_pattern': bucket_stats(real_trades, _excursion_bucket),
        'noise_by_engine':        bucket_stats(noise_trades, lambda t: _engine_label(t.get('engine'))),
    }


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else '/var/data/trades.csv'
    trades = load_trades(path)
    if not trades:
        print(f"No completed trades in {path}", file=sys.stderr)
        sys.exit(0 if os.path.exists(path) else 1)

    # 2026-04-26: split real-engine signals from bookkeeping noise so
    # engine quality isn't polluted by reconciler/untagged trades.
    real_trades = [t for t in trades if not is_noise_engine(t.get('engine'))]
    noise_trades = [t for t in trades if is_noise_engine(t.get('engine'))]

    print("\n" + "█" * 64)
    print("  REAL ENGINES — strategy quality (excludes RECONCILED + untagged)")
    print("█" * 64)
    header_summary(real_trades)
    render('by_engine (real)', bucket_stats(real_trades, lambda t: _engine_label(t.get('engine'))))
    render('by_coin (real)', bucket_stats(real_trades, lambda t: t.get('coin') or '?'))
    render('by_side (real)', bucket_stats(real_trades, lambda t: t.get('side') or '?'))
    render('by_close_reason (real)', bucket_stats(real_trades, lambda t: t.get('close_reason') or '?'))
    render('by_hold_bucket (real)', bucket_stats(real_trades, lambda t: hold_bucket(t.get('hold_sec'))))
    render('by_hour_utc (real)', bucket_stats(real_trades, hour_of_day))
    render('by_expected_edge_band (real)',
           bucket_stats(real_trades, lambda t: edge_bucket(t.get('expected_edge_at_entry'))))
    render('by_regime (real)', bucket_stats(real_trades, lambda t: t.get('regime') or 'unknown'))

    # MFE/MAE excursion analysis — distinguishes "good entry / bad exit"
    # from "bad entry". _excursion_bucket lives at module scope so the
    # programmatic /analyze endpoint shares the same definition.
    render('by_excursion_pattern (real)', bucket_stats(real_trades, _excursion_bucket))

    if noise_trades:
        print("\n" + "─" * 64)
        print(f"  NOISE / BOOKKEEPING ({len(noise_trades)} trades) — RECONCILED + untagged_legacy")
        print(f"  These are reconciler-driven closes, adopted positions, and")
        print(f"  pre-attribution-fix legacy rows. NOT strategy decisions.")
        print(f"  PnL counts toward equity but not toward engine evaluation.")
        print("─" * 64)
        render('by_engine (noise)', bucket_stats(noise_trades, lambda t: _engine_label(t.get('engine'))))
        noise_pnl = sum(t['pnl'] for t in noise_trades if t.get('pnl') is not None)
        print(f"\n  noise total PnL: {noise_pnl:.4f}  (informational — don't use to judge engines)\n")


if __name__ == '__main__':
    main()
