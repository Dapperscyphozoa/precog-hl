"""DIAGNOSE — one-shot full diagnostic.

The point: instead of asking the user to paste raw data from N endpoints,
this returns *every* slice we keep asking about in one JSON blob.

What it does:
  1. Reads /var/data/trades.csv directly
  2. Splits by system (A = native engines, B = CONFLUENCE_*)
  3. Computes P&L across rolling windows: 1h/6h/12h/24h/48h/72h/7d/14d
  4. Per-engine breakdown within each window (excluding RECONCILED +
     untagged_legacy noise)
  5. Cutover detection: for each engine, when did rolling 6h P&L last
     flip from +ve to -ve (i.e. "stopped working")?
  6. Hour-level cumulative P&L per system for plotting
  7. Cross-reference: deploy timeline (parsed from git, else None)

API: diagnose(hours_back=168) -> dict

Defaults: 7-day window for hour-level series; rolling bins as listed.
"""
import os
import csv
import time
import subprocess
from datetime import datetime, timezone, timedelta
from collections import defaultdict


SYSTEM_A_ENGINES = {
    'PIVOT', 'BB_REJ', 'INSIDE_BAR', 'PULLBACK',
    'WALL_BNC', 'WALL_EXH', 'WALL_ABSORB',
    'FUNDING_MR', 'LIQ_CSCD', 'ASIAN_SESSION', 'SPOOF',
    'TREND_CONT', 'QUEUED_REVERSAL_15m',
}
NOISE_ENGINES = {'RECONCILED', 'untagged_legacy'}
WINDOWS_HOURS = [1, 6, 12, 24, 48, 72, 168, 336]  # 1h..14d


def _parse_ts(s):
    if not s:
        return None
    try:
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _system_of(engine):
    """Return 'A', 'B', 'NOISE', or 'UNKNOWN'."""
    if not engine:
        return 'NOISE'
    if engine in NOISE_ENGINES:
        return 'NOISE'
    if engine.startswith('CONFLUENCE_'):
        return 'B'
    if engine in SYSTEM_A_ENGINES:
        return 'A'
    return 'UNKNOWN'


def _empty_bucket():
    return {'n': 0, 'w': 0, 'l': 0, 'b': 0, 'pnl': 0.0,
            'avg_win': None, 'avg_loss': None, 'wr_pct': None}


def _close_bucket(b):
    """Compute derived metrics on a bucket."""
    decided = b['w'] + b['l']
    b['wr_pct'] = round(b['w'] / decided * 100, 1) if decided else None
    b['pnl'] = round(b['pnl'], 4)
    if b['w'] and b.get('_win_sum') is not None:
        b['avg_win'] = round(b['_win_sum'] / b['w'], 4)
    if b['l'] and b.get('_loss_sum') is not None:
        b['avg_loss'] = round(b['_loss_sum'] / b['l'], 4)
    b.pop('_win_sum', None)
    b.pop('_loss_sum', None)
    return b


def _add_to_bucket(b, pnl):
    b['n'] += 1
    if pnl > 0:
        b['w'] += 1
        b.setdefault('_win_sum', 0.0)
        b['_win_sum'] += pnl
    elif pnl < 0:
        b['l'] += 1
        b.setdefault('_loss_sum', 0.0)
        b['_loss_sum'] += pnl
    else:
        b['b'] += 1
    b['pnl'] += pnl


def _git_recent_deploys(n=20):
    """Return [{'ts_utc': int, 'iso': str, 'sha': str, 'msg': str}, ...]
    for the last n commits to main. Best-effort, returns [] on any error."""
    try:
        cwd = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.check_output(
            ['git', '-C', cwd, 'log', 'main', '-n', str(n),
             '--pretty=format:%H|%ct|%s'],
            stderr=subprocess.DEVNULL, timeout=5).decode()
        deploys = []
        for line in out.strip().splitlines():
            parts = line.split('|', 2)
            if len(parts) != 3:
                continue
            sha, ts, msg = parts
            try:
                ts_i = int(ts)
            except Exception:
                continue
            deploys.append({
                'ts_utc': ts_i,
                'iso': datetime.fromtimestamp(ts_i, tz=timezone.utc).isoformat(),
                'sha': sha[:7],
                'msg': msg,
            })
        return deploys
    except Exception:
        return []


def diagnose(trade_log_path=None, hours_back=168):
    """Run the full diagnostic. Returns dict."""
    path = trade_log_path or os.environ.get('BUCKET_TRADE_LOG_PATH', '/var/data/trades.csv')
    if not os.path.exists(path):
        return {'err': f'trade log not found: {path}'}

    # 1) load all CLOSE rows
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r.get('event_type') != 'CLOSE':
                continue
            try:
                pnl = float(r.get('pnl') or 0)
            except Exception:
                continue
            ts = _parse_ts(r.get('timestamp', ''))
            if ts is None:
                continue
            engine = (r.get('engine') or '').strip() or 'untagged_legacy'
            rows.append({
                'ts': ts,
                'iso': r.get('timestamp', ''),
                'coin': r.get('coin', ''),
                'engine': engine,
                'side': r.get('side', ''),
                'regime': (r.get('regime') or '').strip().lower(),
                'pnl': pnl,
                'system': _system_of(engine),
            })
    rows.sort(key=lambda x: x['ts'])
    now = int(time.time())

    # 2) windowed buckets
    by_window = {}
    for h in WINDOWS_HOURS:
        cutoff = now - h * 3600
        recent = [r for r in rows if r['ts'] >= cutoff]
        # totals + system splits
        sys_buckets = {'A': _empty_bucket(), 'B': _empty_bucket(), 'NOISE': _empty_bucket()}
        engines = defaultdict(_empty_bucket)
        for r in recent:
            _add_to_bucket(sys_buckets.get(r['system'], sys_buckets['NOISE']), r['pnl'])
            _add_to_bucket(engines[r['engine']], r['pnl'])
        for k in sys_buckets:
            _close_bucket(sys_buckets[k])
        engines_closed = {}
        for k, v in engines.items():
            engines_closed[k] = _close_bucket(v)
        # rank engines by signed pnl
        engines_sorted = sorted(engines_closed.items(),
                                key=lambda kv: (kv[1].get('pnl') or 0))
        by_window[f'{h}h'] = {
            'n_closes': len(recent),
            'system_A': sys_buckets['A'],
            'system_B': sys_buckets['B'],
            'noise':    sys_buckets['NOISE'],
            'real_total_pnl': round(sys_buckets['A']['pnl'] + sys_buckets['B']['pnl'], 4),
            'worst_engines': engines_sorted[:5],
            'best_engines':  list(reversed(engines_sorted[-5:])),
        }

    # 3) hourly cumulative P&L per system (last hours_back hours)
    cutoff = now - hours_back * 3600
    rows_window = [r for r in rows if r['ts'] >= cutoff]
    hourly = defaultdict(lambda: {'A': 0.0, 'B': 0.0, 'NOISE': 0.0, 'n': 0})
    for r in rows_window:
        bucket_h = r['ts'] - (r['ts'] % 3600)
        hourly[bucket_h][r['system']] = hourly[bucket_h].get(r['system'], 0.0) + r['pnl']
        hourly[bucket_h]['n'] += 1
    hourly_sorted = sorted(hourly.items())
    cum_A = 0.0
    cum_B = 0.0
    series = []
    for bh, vals in hourly_sorted:
        cum_A += vals.get('A', 0.0)
        cum_B += vals.get('B', 0.0)
        series.append({
            'hour_utc': datetime.fromtimestamp(bh, tz=timezone.utc).isoformat(),
            'closes':   vals['n'],
            'pnl_A':    round(vals.get('A', 0.0), 4),
            'pnl_B':    round(vals.get('B', 0.0), 4),
            'cum_A':    round(cum_A, 4),
            'cum_B':    round(cum_B, 4),
        })

    # 4) cutover detection per system: last hour where rolling 6h pnl crossed
    #    from +ve to -ve (i.e. when did the system "stop working")
    cutover = {}
    for sys_key in ('A', 'B'):
        last_pos_to_neg_ts = None
        last_pos_to_neg_iso = None
        prev_roll = 0.0
        # rolling 6h sum
        for i, point in enumerate(series):
            window_start = i - 6 + 1
            if window_start < 0:
                continue
            roll = sum(p[f'pnl_{sys_key}'] for p in series[window_start:i+1])
            if prev_roll > 0 and roll <= 0:
                last_pos_to_neg_iso = point['hour_utc']
                last_pos_to_neg_ts = int(datetime.fromisoformat(point['hour_utc']).timestamp())
            prev_roll = roll
        cutover[f'system_{sys_key}'] = {
            'last_flip_pos_to_neg_iso': last_pos_to_neg_iso,
            'last_flip_pos_to_neg_ts':  last_pos_to_neg_ts,
        }

    # 5) deploy timeline + correlate
    deploys = _git_recent_deploys(20)
    correlations = []
    for sys_key, info in cutover.items():
        flip_ts = info['last_flip_pos_to_neg_ts']
        if not flip_ts:
            correlations.append({'system': sys_key, 'flip_iso': None,
                                 'nearest_deploy': None})
            continue
        nearest = None
        nearest_dt = None
        for d in deploys:
            dt = abs(d['ts_utc'] - flip_ts)
            if nearest_dt is None or dt < nearest_dt:
                nearest_dt = dt
                nearest = d
        correlations.append({
            'system': sys_key,
            'flip_iso': info['last_flip_pos_to_neg_iso'],
            'nearest_deploy': nearest,
            'gap_minutes': round(nearest_dt / 60, 1) if nearest_dt is not None else None,
        })

    # 6) summary verdict
    last_24h = by_window['24h']
    last_12h = by_window['12h']
    last_6h = by_window['6h']
    verdict_lines = []
    for sys_label, key in [('System A', 'system_A'), ('System B', 'system_B')]:
        s24 = last_24h[key]
        s12 = last_12h[key]
        s6  = last_6h[key]
        verdict_lines.append(
            f'{sys_label}: 6h={s6["pnl"]:+.4f} (n={s6["n"]}), '
            f'12h={s12["pnl"]:+.4f} (n={s12["n"]}), '
            f'24h={s24["pnl"]:+.4f} (n={s24["n"]}), '
            f'48h={by_window["48h"][key]["pnl"]:+.4f} (n={by_window["48h"][key]["n"]})'
        )

    return {
        'err': None,
        'now_utc': datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        'total_rows': len(rows),
        'rows_in_window': len(rows_window),
        'hours_back': hours_back,
        'verdict_summary': verdict_lines,
        'by_window': by_window,
        'system_cutover': cutover,
        'deploy_correlation': correlations,
        'recent_deploys': deploys,
        'hourly_series': series,
    }
