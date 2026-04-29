"""REGIME_LAG_AUDIT — empirical test of the regime-classifier hysteresis hypothesis.

Hypothesis (user, 2026-04-29):
  Regime detector uses 2-bar 1h hysteresis + 30m confirm. In crypto, chop→trend
  transitions happen in minutes, not hours. By the time the classifier confirms
  trend, the move is over. Allowlists block trend signals in chop regime, so we
  miss the move AND keep firing chop-style signals into a now-trending tape.

Test:
  For each losing trade tagged regime='chop' at entry, fetch BTC 1h candles
  around entry. If BTC made a directional move (|move| >= MOVE_THRESHOLD)
  within the ±1h window, the classifier was lagging — actual BTC was trending
  while we were classified as chop.

  Returns counts + per-trade detail so the user can verify.

API:
  audit(threshold_pct=0.01, hours_window=1) -> dict
"""
import os
import csv
import json
import time
import urllib.request
from datetime import datetime, timezone


def _parse_ts(s):
    if not s:
        return None
    try:
        # ISO with or without tz
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _fetch_btc_1h(start_s, end_s):
    """Fetch BTC 1h candles in [start_s, end_s] from HL info API."""
    body = json.dumps({
        'type': 'candleSnapshot',
        'req': {
            'coin': 'BTC',
            'interval': '1h',
            'startTime': int(start_s) * 1000,
            'endTime':   int(end_s) * 1000,
        }
    }).encode()
    req = urllib.request.Request('https://api.hyperliquid.xyz/info',
        data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _btc_close_at(candles_sorted, target_s):
    """Return close of the 1h bar containing target_s, or None if out of range."""
    if not candles_sorted:
        return None
    for c in candles_sorted:
        try:
            t_open = int(c.get('t', 0)) // 1000
            t_close = int(c.get('T', 0)) // 1000 if c.get('T') else t_open + 3600
            if t_open <= target_s <= t_close:
                return float(c.get('c'))
        except Exception:
            continue
    # fallback: nearest bar within 1h
    nearest = None
    nearest_d = 10**9
    for c in candles_sorted:
        try:
            t_open = int(c.get('t', 0)) // 1000
            d = abs(t_open - target_s)
            if d < nearest_d:
                nearest_d = d
                nearest = c
        except Exception:
            continue
    if nearest is not None and nearest_d <= 3600:
        try:
            return float(nearest.get('c'))
        except Exception:
            return None
    return None


def audit(trade_log_path=None, threshold_pct=0.01, hours_window=1, max_rows=2000):
    """Run the audit.

    Args:
      trade_log_path: defaults to env TRADE_LOG_PATH or /var/data/trades.csv
      threshold_pct:  fraction; default 0.01 = 1%
      hours_window:   look ±N hours from entry; default 1
      max_rows:       cap on rows scanned (most recent N closed losses in chop)

    Returns: dict summary.
    """
    path = trade_log_path or os.environ.get('BUCKET_TRADE_LOG_PATH', '/var/data/trades.csv')
    if not os.path.exists(path):
        return {'err': f'trade log not found: {path}'}

    # 1) load all CLOSE rows with regime + pnl + timestamp
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
            regime = (r.get('regime') or '').strip().lower()
            ts = _parse_ts(r.get('timestamp', ''))
            if ts is None:
                continue
            rows.append({
                'ts': ts,
                'iso': r.get('timestamp', ''),
                'coin': r.get('coin', ''),
                'engine': r.get('engine', ''),
                'side': r.get('side', ''),
                'regime': regime,
                'pnl': pnl,
                'mfe_pct': r.get('mfe_pct', ''),
                'mae_pct': r.get('mae_pct', ''),
            })

    rows.sort(key=lambda x: x['ts'])
    rows = rows[-max_rows:]

    chop_losers = [r for r in rows if r['regime'] == 'chop' and r['pnl'] < 0]
    if not chop_losers:
        return {
            'err': None,
            'note': 'no closed losing trades tagged regime=chop',
            'total_rows_scanned': len(rows),
            'threshold_pct': threshold_pct,
            'hours_window': hours_window,
        }

    # 2) one BTC 1h candle fetch covering full span ± buffer
    span_start = chop_losers[0]['ts'] - (hours_window + 2) * 3600
    span_end   = chop_losers[-1]['ts'] + (hours_window + 2) * 3600
    try:
        bars = _fetch_btc_1h(span_start, span_end)
    except Exception as e:
        return {'err': f'btc fetch failed: {type(e).__name__}: {e}'}

    bars.sort(key=lambda b: int(b.get('t', 0)))
    if not bars:
        return {'err': 'btc candles empty'}

    # 3) per-trade: BTC close at entry-1h vs entry+1h
    detail = []
    classifier_wrong = 0
    classifier_ok = 0
    insufficient = 0
    move_dir_match_loss = 0  # losing trade direction was AGAINST btc trend
    for r in chop_losers:
        before = _btc_close_at(bars, r['ts'] - hours_window * 3600)
        after  = _btc_close_at(bars, r['ts'] + hours_window * 3600)
        if before is None or after is None or before <= 0:
            insufficient += 1
            detail.append({**r, 'btc_before': before, 'btc_after': after,
                           'btc_move_pct': None, 'verdict': 'insufficient_btc_data'})
            continue
        move = (after - before) / before
        directional = abs(move) >= threshold_pct
        verdict = 'classifier_lagging' if directional else 'classifier_ok_chop_confirmed'
        if directional:
            classifier_wrong += 1
            # was the loss against the btc move?
            btc_dir = 'BUY' if move > 0 else 'SELL'
            if r['side'] and r['side'] != btc_dir:
                move_dir_match_loss += 1
        else:
            classifier_ok += 1
        detail.append({
            **r,
            'btc_before': round(before, 2),
            'btc_after': round(after, 2),
            'btc_move_pct': round(move * 100, 3),
            'verdict': verdict,
        })

    n = len(chop_losers)
    return {
        'err': None,
        'threshold_pct': threshold_pct,
        'hours_window': hours_window,
        'total_rows_scanned': len(rows),
        'chop_losers': n,
        'classifier_lagging': classifier_wrong,
        'classifier_lagging_pct': round(classifier_wrong / n * 100, 1) if n else None,
        'classifier_ok': classifier_ok,
        'insufficient_btc_data': insufficient,
        'losses_against_btc_trend': move_dir_match_loss,
        'losses_against_btc_trend_pct': round(move_dir_match_loss / n * 100, 1) if n else None,
        'sample_lagging_trades':
            [d for d in detail if d.get('verdict') == 'classifier_lagging'][-25:],
        'sample_ok_trades':
            [d for d in detail if d.get('verdict') == 'classifier_ok_chop_confirmed'][-10:],
    }
