"""EDGE_AUDIT — per-engine breakdown with Wilson CIs.

Closes the loop on godmode action 5: rolling per-engine, per-coin,
per-regime, per-hour P&L with statistical confidence bounds.

Why this matters:
  - Wilson auto-disable already kills engine-level losers
  - But within a +EV engine, some coins/regimes/hours may be winners
    while others drag the average down
  - Per-(engine, coin) filtering can lift WR materially without
    needing more notional, more engines, or more frequency
  - Wilson LCB (lower confidence bound) prevents over-fitting to noise

API:
  audit(engine=None, days=7, min_n=3, z=1.645) -> dict

Returns:
  - overall: n, wins, losses, wr_pct, mean_pnl, wilson_wr_lcb_pct,
    mean_pnl_95_lcb (t-distribution lower bound), ev_per_trade_usd
  - by_coin: top 25 by n, with same metrics + recommendation flag
  - by_regime: chop/bull-calm/bear-calm/etc
  - by_hour_utc: 0..23
  - by_close_reason: tp/sl/timeout/protection/etc
  - mfe_mae_distribution: pct of trades reaching N bp MFE
  - recommended_allowlist: coins where wilson_wr_lcb >= TARGET_WR_FLOOR

If engine is None, returns top 10 engines by n.
"""
import os
import csv
import math
import time
from datetime import datetime, timezone
from collections import defaultdict


NOISE_ENGINES = {'RECONCILED', 'untagged_legacy'}
DEFAULT_FEE_RT_PCT = 0.0006  # 6 bp = 2*maker, structural floor used for net-of-fees EV


def _wilson_lcb(wins, n, z=1.645):
    """Wilson 95% one-sided lower confidence bound on proportion."""
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return max(0.0, centre - half)


def _mean_lcb(values, z=1.645):
    """Normal-approx 95% one-sided lower confidence bound on mean."""
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    stderr = math.sqrt(var) / math.sqrt(n)
    return m - z * stderr


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


def _summarize(rows, label, key_fn, min_n=3, z=1.645, target_wr_lcb=0.55):
    """Group rows by key_fn, summarize each group. Returns sorted list."""
    groups = defaultdict(list)
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        groups[k].append(r)
    out = []
    for k, group in groups.items():
        n = len(group)
        if n < 1:
            continue
        wins = sum(1 for r in group if r['pnl'] > 0)
        losses = sum(1 for r in group if r['pnl'] < 0)
        decided = wins + losses
        pnls = [r['pnl'] for r in group]
        sum_pnl = sum(pnls)
        mean_pnl = sum_pnl / n if n else 0
        wr_lcb = _wilson_lcb(wins, decided, z) if decided else 0
        mean_lcb = _mean_lcb(pnls, z) if n >= 2 else None
        avg_mfe = None
        avg_mae = None
        mfes = [r.get('mfe_pct') for r in group if r.get('mfe_pct') is not None]
        maes = [r.get('mae_pct') for r in group if r.get('mae_pct') is not None]
        if mfes:
            avg_mfe = round(sum(mfes) / len(mfes) * 100, 4)
        if maes:
            avg_mae = round(sum(maes) / len(maes) * 100, 4)
        rec_keep = (n >= min_n and decided >= min_n and wr_lcb >= target_wr_lcb
                    and (mean_lcb is None or mean_lcb > -DEFAULT_FEE_RT_PCT * 100))
        out.append({
            label: k,
            'n': n,
            'wins': wins,
            'losses': losses,
            'wr_pct':       round(wins / decided * 100, 1) if decided else None,
            'wilson_wr_lcb_pct': round(wr_lcb * 100, 1),
            'mean_pnl_usd': round(mean_pnl, 4),
            'sum_pnl_usd':  round(sum_pnl, 4),
            'mean_pnl_lcb_usd': round(mean_lcb, 4) if mean_lcb is not None else None,
            'avg_mfe_pct':  avg_mfe,
            'avg_mae_pct':  avg_mae,
            'recommended_keep': rec_keep,
        })
    out.sort(key=lambda x: -x['n'])
    return out


def audit(engine=None, days=7, min_n=3, z=1.645, target_wr_lcb=0.55, trade_log_path=None):
    """Per-engine / per-coin / per-regime / per-hour edge audit.

    Args:
      engine: filter to a specific engine; None returns top 10 engines + recommendation
      days: lookback window
      min_n: minimum sample size for "recommended" flag
      z: confidence multiplier (1.645 = 95% one-sided)
      target_wr_lcb: required WR Wilson LCB for recommended_keep flag
      trade_log_path: defaults to env or /var/data/trades.csv
    """
    path = trade_log_path or os.environ.get('BUCKET_TRADE_LOG_PATH', '/var/data/trades.csv')
    if not os.path.exists(path):
        return {'err': f'trade log not found: {path}'}

    cutoff_ts = time.time() - days * 86400
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r.get('event_type') != 'CLOSE':
                continue
            engine_name = (r.get('engine') or '').strip()
            if not engine_name or engine_name in NOISE_ENGINES:
                continue
            ts = _parse_ts(r.get('timestamp', ''))
            if ts is None or ts < cutoff_ts:
                continue
            try:
                pnl = float(r.get('pnl') or 0)
            except Exception:
                continue
            try:
                mfe = float(r.get('mfe_pct') or 0) if r.get('mfe_pct') not in (None, '') else None
            except Exception:
                mfe = None
            try:
                mae = float(r.get('mae_pct') or 0) if r.get('mae_pct') not in (None, '') else None
            except Exception:
                mae = None
            rows.append({
                'ts': ts,
                'iso': r.get('timestamp', ''),
                'engine': engine_name,
                'coin': (r.get('coin') or '').strip(),
                'side': (r.get('side') or '').strip(),
                'regime': (r.get('regime') or '').strip().lower(),
                'pnl': pnl,
                'mfe_pct': mfe,
                'mae_pct': mae,
                'close_reason': (r.get('close_reason') or '').strip(),
                'hour_utc': datetime.fromtimestamp(ts, tz=timezone.utc).hour,
            })

    if not rows:
        return {'err': None, 'note': 'no closed trades in lookback', 'days': days, 'engine': engine}

    # If no engine filter, return per-engine summary + drill-down for each
    if engine is None:
        per_engine = _summarize(rows, 'engine', lambda r: r['engine'],
                                min_n=min_n, z=z, target_wr_lcb=target_wr_lcb)
        # For each engine, attach a thin per-coin list (top 5 by n)
        for e in per_engine[:10]:
            e_rows = [r for r in rows if r['engine'] == e['engine']]
            e['top_coins'] = _summarize(e_rows, 'coin', lambda r: r['coin'],
                                        min_n=min_n, z=z, target_wr_lcb=target_wr_lcb)[:5]
        return {
            'err': None,
            'engine': None,
            'days': days,
            'min_n': min_n,
            'z': z,
            'target_wr_lcb_pct': target_wr_lcb * 100,
            'total_rows_window': len(rows),
            'engines': per_engine[:10],
        }

    # Engine-specific deep audit
    engine_rows = [r for r in rows if r['engine'] == engine]
    if not engine_rows:
        return {'err': None, 'note': f'no closed trades for {engine} in lookback',
                'days': days, 'engine': engine}

    overall_summary = _summarize(engine_rows, 'engine', lambda r: r['engine'],
                                  min_n=min_n, z=z, target_wr_lcb=target_wr_lcb)
    overall = overall_summary[0] if overall_summary else None

    by_coin   = _summarize(engine_rows, 'coin',   lambda r: r['coin'],   min_n=min_n, z=z, target_wr_lcb=target_wr_lcb)
    by_regime = _summarize(engine_rows, 'regime', lambda r: r['regime'] or 'unknown', min_n=min_n, z=z, target_wr_lcb=target_wr_lcb)
    by_hour   = _summarize(engine_rows, 'hour_utc', lambda r: r['hour_utc'], min_n=min_n, z=z, target_wr_lcb=target_wr_lcb)
    by_close  = _summarize(engine_rows, 'close_reason', lambda r: r['close_reason'] or 'unknown', min_n=min_n, z=z, target_wr_lcb=target_wr_lcb)
    by_side   = _summarize(engine_rows, 'side', lambda r: r['side'], min_n=min_n, z=z, target_wr_lcb=target_wr_lcb)

    # MFE/MAE distribution thresholds
    mfe_thresholds_bp = [5, 10, 20, 30, 50, 100]
    mfe_dist = {}
    for thr in mfe_thresholds_bp:
        thr_frac = thr / 10000.0
        n_with = sum(1 for r in engine_rows if r['mfe_pct'] is not None and r['mfe_pct'] >= thr_frac)
        mfe_dist[f'>={thr}bp'] = {
            'n': n_with,
            'pct': round(n_with / len(engine_rows) * 100, 1) if engine_rows else None,
        }

    # Recommendation: coins where WR LCB >= target AND n >= min_n
    recommended_allowlist = [c['coin'] for c in by_coin if c.get('recommended_keep')]
    discouraged = [c['coin'] for c in by_coin if not c.get('recommended_keep') and c['n'] >= min_n]

    return {
        'err': None,
        'engine': engine,
        'days': days,
        'min_n': min_n,
        'z': z,
        'target_wr_lcb_pct': target_wr_lcb * 100,
        'total_rows_engine': len(engine_rows),
        'overall': overall,
        'by_coin': by_coin[:25],
        'by_regime': by_regime,
        'by_hour_utc': sorted(by_hour, key=lambda x: x['hour_utc']),
        'by_close_reason': by_close,
        'by_side': by_side,
        'mfe_distribution': mfe_dist,
        'recommended_coin_allowlist': recommended_allowlist,
        'discouraged_coins_with_data': discouraged,
        'sample_recent_trades': sorted(engine_rows, key=lambda x: -x['ts'])[:10],
    }
