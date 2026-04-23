"""Edge decay monitor — rolling-window drift detector.

Tracks 4 metrics across 20/50/100-trade windows:
- WR drift (rolling win rate change)
- R:R compression (avg_pnl ratio)
- Hold time expansion
- Regime efficacy decay

Half-life calculation flags imminent edge loss BEFORE PnL drops.

UNLIKE other telemetry: this one IS live-active (alerts only, no trading gate).
The math is rolling statistics — no empirical validation needed.

Thresholds:
- decaying_slow: WR drift < -3pp
- decaying_fast: 3+ metrics negative, half-life <50 trades
- broken: recent_20 WR <45%
"""
import json, os, time, threading, math
from collections import deque

LOG_PATH = os.environ.get('DECAY_LOG_PATH', '/app/edge_decay.jsonl')
_LOCK = threading.Lock()
_LOG_PREFIX = '[edge_decay]'
_LAST_ALERT = {'state': 'stable', 'ts': 0}

# In-memory rolling window of closes
_CLOSES = deque(maxlen=200)


def record_close(coin, engine, regime, pnl_pct, hold_seconds, win, exit_reason=None,
                 regime_at_entry=None, config_source=None):
    """Update rolling window and evaluate decay state. Non-blocking."""
    def _do():
        try:
            rec = {
                'ts': int(time.time()),
                'coin': coin,
                'engine': engine,
                'regime': regime,
                'regime_at_entry': regime_at_entry,
                'pnl_pct': round(float(pnl_pct), 3),
                'hold_sec': int(hold_seconds) if hold_seconds else 0,
                'win': bool(win),
                'exit_reason': exit_reason,
                'config_source': config_source,
            }
            with _LOCK:
                _CLOSES.append(rec)
                state = _evaluate()
                rec['decay_state'] = state['trend']
                rec['half_life_trades'] = state.get('half_life_trades')
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
                _maybe_alert(state)
        except Exception as e:
            print(f"{_LOG_PREFIX} record err: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def _window_stats(window_trades):
    if not window_trades:
        return None
    n = len(window_trades)
    wins = sum(1 for t in window_trades if t['win'])
    wr = wins / n
    avg_pnl = sum(t['pnl_pct'] for t in window_trades) / n
    avg_hold = sum(t['hold_sec'] for t in window_trades) / n if any(t['hold_sec'] for t in window_trades) else 0
    # Regime efficacy: did we win when regime_at_entry matched regime_at_close?
    matched_regime = [t for t in window_trades if t.get('regime_at_entry') and t.get('regime')
                      and t['regime_at_entry'] == t['regime']]
    regime_eff = (sum(1 for t in matched_regime if t['win']) / len(matched_regime)) if matched_regime else None
    return {'n': n, 'wr': wr, 'avg_pnl': avg_pnl, 'avg_hold': avg_hold,
            'regime_eff': regime_eff}


def _evaluate():
    """Compute trend state from current closes buffer."""
    closes = list(_CLOSES)
    if len(closes) < 20:
        return {'trend': 'insufficient_data', 'n_total': len(closes)}

    w20 = _window_stats(closes[-20:])
    w50 = _window_stats(closes[-50:]) if len(closes) >= 50 else None
    w100 = _window_stats(closes[-100:]) if len(closes) >= 100 else None

    # Metric A: WR drift
    wr_drift_20_vs_50 = None
    wr_drift_accelerating = False
    if w50:
        wr_drift_20_vs_50 = w20['wr'] - w50['wr']
        if w100:
            drift_50_vs_100 = w50['wr'] - w100['wr']
            if wr_drift_20_vs_50 < 0 and drift_50_vs_100 < 0 and wr_drift_20_vs_50 < drift_50_vs_100:
                wr_drift_accelerating = True

    # Metric B: R:R compression
    rr_compression = None
    if w50 and w50['avg_pnl'] != 0:
        rr_compression = w20['avg_pnl'] / w50['avg_pnl']

    # Metric C: Hold expansion
    hold_expansion = None
    if w50 and w50['avg_hold'] > 0:
        hold_expansion = w20['avg_hold'] / w50['avg_hold']

    # Metric D: Regime efficacy decay
    regime_eff_drop = None
    if w50 and w20['regime_eff'] is not None and w50['regime_eff'] is not None:
        regime_eff_drop = w20['regime_eff'] - w50['regime_eff']

    # Score negative metrics
    negative_count = 0
    if wr_drift_20_vs_50 is not None and wr_drift_20_vs_50 < -0.03: negative_count += 1
    if rr_compression is not None and rr_compression < 0.7: negative_count += 1
    if hold_expansion is not None and hold_expansion > 1.4: negative_count += 1
    if regime_eff_drop is not None and regime_eff_drop < -0.05: negative_count += 1

    # Half-life
    half_life = None
    if wr_drift_20_vs_50 is not None and wr_drift_20_vs_50 < 0:
        loss_per_trade = abs(wr_drift_20_vs_50) / 30  # drift over ~30-trade span
        current_edge = max(w20['wr'] - 0.5, 0.001)
        if loss_per_trade > 0:
            half_life = current_edge / loss_per_trade / 2  # trades to lose half of edge

    # Trend classification
    if w20['wr'] < 0.45:
        trend = 'broken'
    elif negative_count >= 3 or (half_life and half_life < 50):
        trend = 'decaying_fast'
    elif negative_count >= 1 and wr_drift_accelerating:
        trend = 'decaying_slow'
    elif all(x is None or x >= 0 for x in [wr_drift_20_vs_50]) and w20['wr'] > (w50['wr'] if w50 else 0.5):
        trend = 'increasing'
    else:
        trend = 'stable'

    return {
        'trend': trend,
        'n_total': len(closes),
        'window_20': w20,
        'window_50': w50,
        'window_100': w100,
        'metrics': {
            'wr_drift_20_vs_50': round(wr_drift_20_vs_50, 4) if wr_drift_20_vs_50 is not None else None,
            'wr_drift_accelerating': wr_drift_accelerating,
            'rr_compression': round(rr_compression, 3) if rr_compression is not None else None,
            'hold_expansion': round(hold_expansion, 3) if hold_expansion is not None else None,
            'regime_eff_drop': round(regime_eff_drop, 4) if regime_eff_drop is not None else None,
            'negative_count': negative_count,
        },
        'half_life_trades': round(half_life, 1) if half_life else None,
    }


def _maybe_alert(state):
    """Log alert if trend state changed to worse category."""
    global _LAST_ALERT
    severity = {'insufficient_data': 0, 'stable': 0, 'increasing': 0,
                'decaying_slow': 1, 'decaying_fast': 2, 'broken': 3}
    trend = state.get('trend', 'stable')
    prev = _LAST_ALERT['state']
    if severity.get(trend, 0) > severity.get(prev, 0):
        _LAST_ALERT = {'state': trend, 'ts': int(time.time())}
        alert_prefix = '★★★' if trend == 'broken' else '⚠' if trend == 'decaying_fast' else '•'
        print(f"{_LOG_PREFIX} {alert_prefix} EDGE TREND: {prev} → {trend} "
              f"(n={state.get('n_total')}, half_life={state.get('half_life_trades')} trades, "
              f"negatives={state.get('metrics',{}).get('negative_count')})",
              flush=True)


def status():
    """Current decay state for /edge_decay endpoint."""
    with _LOCK:
        state = _evaluate()
    state['last_alert'] = _LAST_ALERT
    state['file_size_kb'] = round(os.path.getsize(LOG_PATH) / 1024, 1) if os.path.exists(LOG_PATH) else 0
    return state


def _rebuild_from_log():
    """On startup, rebuild rolling window from persisted log."""
    if not os.path.exists(LOG_PATH):
        return
    try:
        with open(LOG_PATH) as f:
            for line in f.readlines()[-200:]:
                try:
                    _CLOSES.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass


# Rebuild on import
_rebuild_from_log()
