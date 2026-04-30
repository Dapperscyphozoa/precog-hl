"""BTCD_BACKTEST — walk full trade ledger, classify each trade by BTCD direction
at entry, return per-engine scenario stats.

Tests whether currently-blocked engines (HL, BTC_WALL+*) and currently-allowed
engines (BB_REJ, PIVOT, CONFLUENCE_DAY+NEWS, etc.) flip from -EV to +EV when
filtered by BTCD direction alignment.

DEFINITIONS
===========

BTCD proxy = BTC_close / ETH_close

State at entry_ts (computed using `lookback_h` and `threshold`):
  pct_change = (btcd_at - btcd_pre) / btcd_pre
  rising  if pct_change > +threshold
  falling if pct_change < -threshold
  flat    otherwise

"Aligned" trade:
  side=SELL when state=rising  (alts weak vs BTC → shorts work)
  side=BUY  when state=falling (alts strong vs BTC → longs work)
  Otherwise: misaligned (or flat).

API
===
audit(engines=None, lookback_h=4, threshold=0.002, days=14) -> dict

  engines: None = all, or list of engine names to filter
  lookback_h: BTCD change window (1, 4, 24)
  threshold: |pct_change| above this = directional state
  days: ledger lookback

Returns per-engine breakdown:
  - baseline: all trades, current realized P&L
  - filter_aligned: keep only BTCD-aligned trades
  - by_state: rows per (state, side)
  - winner_classification: which engines flip +EV under filter

PRICE DATA
==========
Hyperliquid public API: candleSnapshot. 1h candles for BTC + ETH.
Cached at module-level for the lifetime of the import.
"""
import os
import csv
import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from collections import defaultdict


NOISE_ENGINES = {'RECONCILED', 'untagged_legacy', ''}

# Cache for BTC/ETH bars to avoid re-fetching on multiple calls
_BAR_CACHE = {'btc': [], 'eth': [], 'span': (0, 0)}


def _wilson_lcb(wins, n, z=1.645):
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return max(0.0, centre - half)


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


def _fetch_bars_chunk(coin, start_ms, end_ms):
    """Single HL API call. Returns list of (ts_sec, close)."""
    body = json.dumps({
        'type': 'candleSnapshot',
        'req': {'coin': coin, 'interval': '1h',
                'startTime': start_ms, 'endTime': end_ms}
    }).encode()
    req = urllib.request.Request(
        'https://api.hyperliquid.xyz/info',
        data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        return [(int(b['t']) // 1000, float(b['c'])) for b in data]
    except Exception:
        return []


def _ensure_bars(start_ts, end_ts):
    """Populate _BAR_CACHE for the requested span. Paginates if needed."""
    span_lo, span_hi = _BAR_CACHE['span']
    if span_lo <= start_ts and span_hi >= end_ts and _BAR_CACHE['btc']:
        return  # cache covers it

    # HL API caps at ~5000 bars per request. 1h bars × 5000 = ~208 days.
    # In practice we want days=14 → ~336 bars. Single call is fine.
    pad = 6 * 3600  # 6h padding for lookback windows
    s_ms = (start_ts - pad) * 1000
    e_ms = (end_ts + pad) * 1000

    btc_bars = _fetch_bars_chunk('BTC', s_ms, e_ms)
    eth_bars = _fetch_bars_chunk('ETH', s_ms, e_ms)
    btc_bars.sort()
    eth_bars.sort()
    _BAR_CACHE['btc'] = btc_bars
    _BAR_CACHE['eth'] = eth_bars
    _BAR_CACHE['span'] = (start_ts - pad, end_ts + pad)


def _close_at(bars, ts):
    """Latest close at or before ts."""
    valid = [(t, c) for t, c in bars if t <= ts]
    return valid[-1][1] if valid else None


def _classify_btcd(entry_ts, lookback_h, threshold):
    """Returns ('rising'|'falling'|'flat'|'no_data', btcd_pct_change)."""
    btc_at = _close_at(_BAR_CACHE['btc'], entry_ts)
    btc_pre = _close_at(_BAR_CACHE['btc'], entry_ts - lookback_h * 3600)
    eth_at = _close_at(_BAR_CACHE['eth'], entry_ts)
    eth_pre = _close_at(_BAR_CACHE['eth'], entry_ts - lookback_h * 3600)
    if not all([btc_at, btc_pre, eth_at, eth_pre]):
        return 'no_data', 0.0
    btcd_at = btc_at / eth_at
    btcd_pre = btc_pre / eth_pre
    chg = (btcd_at - btcd_pre) / btcd_pre
    if chg > threshold:
        return 'rising', chg
    if chg < -threshold:
        return 'falling', chg
    return 'flat', chg


def _aligned(state, side):
    """side=SELL aligned with rising; side=BUY aligned with falling."""
    side = (side or '').upper()
    if state == 'rising' and side in ('SELL', 'S', 'SHORT'):
        return True
    if state == 'falling' and side in ('BUY', 'B', 'LONG'):
        return True
    return False


def _misaligned(state, side):
    """side=BUY in rising state, or side=SELL in falling state."""
    side = (side or '').upper()
    if state == 'rising' and side in ('BUY', 'B', 'LONG'):
        return True
    if state == 'falling' and side in ('SELL', 'S', 'SHORT'):
        return True
    return False



def _stats(rs):
    n = len(rs)
    w = sum(1 for r in rs if r['pnl_usd'] > 0)
    l = sum(1 for r in rs if r['pnl_usd'] < 0)
    s = sum(r['pnl_usd'] for r in rs)
    wr = (100.0 * w / (w + l)) if (w + l) else 0.0
    wlo = 100.0 * _wilson_lcb(w, w + l) if (w + l) else 0.0
    mean = s / n if n else 0.0
    return {
        'n': n, 'wins': w, 'losses': l,
        'wr_pct': round(wr, 1),
        'wilson_wr_lcb_pct': round(wlo, 1),
        'sum_pnl_usd': round(s, 4),
        'mean_pnl_usd': round(mean, 5),
    }


def _by_regime_breakdown(classified_rows):
    """Per-regime stats split by alignment."""
    from collections import defaultdict
    by_reg = defaultdict(list)
    for r in classified_rows:
        by_reg[r.get('regime') or 'unknown'].append(r)
    out = {}
    for reg, rs in by_reg.items():
        out[reg] = {
            'baseline': _stats(rs),
            'aligned_only': _stats([r for r in rs if r['aligned']]),
            'misaligned_only': _stats([r for r in rs if r['misaligned']]),
            'flat_only': _stats([r for r in rs if not r['aligned'] and not r['misaligned']]),
        }
    return out


def audit(engines=None, lookback_h=4, threshold=0.002, days=14,
          trade_log_path=None):
    """Walk ledger, classify each closed trade by BTCD state at entry.

    engines: None=all, or set/list of engine names to filter (e.g.
             {'HL', 'CONFLUENCE_BTC_WALL+NEWS'}).
    lookback_h: BTCD change window in hours (1, 4, 24).
    threshold: |pct_change| boundary for rising/falling vs flat (e.g. 0.002 = 0.2%).
    days: ledger lookback window.
    """
    path = trade_log_path or os.environ.get(
        'BUCKET_TRADE_LOG_PATH', '/var/data/trades.csv')
    if not os.path.exists(path):
        return {'err': f'trade log not found: {path}'}

    if engines is not None:
        engines = set(engines) if not isinstance(engines, set) else engines

    cutoff = time.time() - days * 86400
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r.get('event_type') != 'CLOSE':
                continue
            eng = (r.get('engine') or '').strip()
            if not eng or eng in NOISE_ENGINES:
                continue
            if engines is not None and eng not in engines:
                continue
            ts = _parse_ts(r.get('timestamp', ''))
            if ts is None or ts < cutoff:
                continue
            try:
                pnl_usd = float(r.get('pnl') or 0)
            except Exception:
                continue
            try:
                pnl_pct = float(r.get('pnl_pct') or 0)
            except Exception:
                pnl_pct = 0.0
            rows.append({
                'ts': ts,
                'engine': eng,
                'coin': (r.get('coin') or '').strip(),
                'side': (r.get('side') or '').strip().upper(),
                'pnl_usd': pnl_usd,
                'pnl_pct': pnl_pct,
            })

    if not rows:
        return {'err': None, 'note': 'no closed trades in window',
                'days': days, 'engines': engines}

    # Fetch BTC + ETH bars covering the rows' time span
    span_lo = min(r['ts'] for r in rows)
    span_hi = max(r['ts'] for r in rows)
    _ensure_bars(span_lo, span_hi)

    btc_n = len(_BAR_CACHE['btc'])
    eth_n = len(_BAR_CACHE['eth'])

    # Classify each trade
    classified = []
    no_data_count = 0
    for r in rows:
        state, chg = _classify_btcd(r['ts'], lookback_h, threshold)
        if state == 'no_data':
            no_data_count += 1
            continue
        r['btcd_state'] = state
        r['btcd_change_pct'] = chg * 100
        r['aligned'] = _aligned(state, r['side'])
        r['misaligned'] = _misaligned(state, r['side'])
        classified.append(r)

    if not classified:
        return {'err': None, 'note': 'no trades classified (BTCD bars missing)',
                'no_data_count': no_data_count,
                'btc_bars_fetched': btc_n, 'eth_bars_fetched': eth_n,
                'window': [
                    datetime.fromtimestamp(span_lo, tz=timezone.utc).isoformat(),
                    datetime.fromtimestamp(span_hi, tz=timezone.utc).isoformat(),
                ]}

    # Group by engine, run scenarios
    by_eng = defaultdict(list)
    for c in classified:
        by_eng[c['engine']].append(c)


    engine_results = []
    for eng, eng_rows in by_eng.items():
        baseline = _stats(eng_rows)
        aligned_only = _stats([r for r in eng_rows if r['aligned']])
        non_misaligned = _stats([r for r in eng_rows if not r['misaligned']])  # aligned + flat
        flat_only = _stats([r for r in eng_rows
                            if not r['aligned'] and not r['misaligned']])
        misaligned = _stats([r for r in eng_rows if r['misaligned']])

        # Scenario delta vs baseline
        delta_aligned = round(aligned_only['sum_pnl_usd']
                              - baseline['sum_pnl_usd'], 4)
        delta_non_misaligned = round(non_misaligned['sum_pnl_usd']
                                     - baseline['sum_pnl_usd'], 4)

        # Recommendation: aligned-only Wilson WR LCB > 50% AND mean > 0
        recommend_filter = (aligned_only['wilson_wr_lcb_pct'] >= 50.0
                            and aligned_only['mean_pnl_usd'] > 0
                            and aligned_only['n'] >= 10)

        engine_results.append({
            'engine': eng,
            'baseline': baseline,
            'aligned_only': aligned_only,
            'non_misaligned': non_misaligned,
            'flat_only': flat_only,
            'misaligned': misaligned,
            'delta_aligned_vs_baseline_usd': delta_aligned,
            'delta_non_misaligned_vs_baseline_usd': delta_non_misaligned,
            'recommend_btcd_filter': recommend_filter,
        })

    # Sort by potential lift
    engine_results.sort(key=lambda e: -e['delta_aligned_vs_baseline_usd'])

    # Overall summary
    total = _stats(classified)
    aligned_total = _stats([c for c in classified if c['aligned']])
    non_mis_total = _stats([c for c in classified if not c['misaligned']])

    return {
        'err': None,
        'config': {
            'lookback_h': lookback_h,
            'threshold_pct': threshold * 100,
            'days': days,
            'engines_filter': sorted(engines) if engines else None,
        },
        'window': {
            'start': datetime.fromtimestamp(span_lo, tz=timezone.utc).isoformat(),
            'end': datetime.fromtimestamp(span_hi, tz=timezone.utc).isoformat(),
        },
        'data_quality': {
            'btc_bars_fetched': btc_n,
            'eth_bars_fetched': eth_n,
            'rows_total': len(rows),
            'rows_classified': len(classified),
            'rows_no_data': no_data_count,
        },
        'overall': {
            'baseline': total,
            'aligned_only': aligned_total,
            'misaligned_only': _stats([c for c in classified if c['misaligned']]),
            'flat_only': _stats([c for c in classified if not c['aligned'] and not c['misaligned']]),
            'non_misaligned': non_mis_total,
            'delta_aligned_vs_baseline_usd': round(
                aligned_total['sum_pnl_usd'] - total['sum_pnl_usd'], 4),
        },
        'by_regime': _by_regime_breakdown(classified),
        'engines': engine_results,
    }


def reset_cache():
    """Clear bar cache. Test helper."""
    _BAR_CACHE['btc'] = []
    _BAR_CACHE['eth'] = []
    _BAR_CACHE['span'] = (0, 0)
