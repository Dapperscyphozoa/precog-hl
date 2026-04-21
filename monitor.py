"""Live monitoring agent. Tracks WR, PnL, position health, alerts on divergence from OOS."""
import time, datetime, json
from collections import deque
import threading

# Rolling stats — last 50 trades
_RECENT_CLOSES = deque(maxlen=50)
_RECENT_OPENS = deque(maxlen=50)
_ALERTS = deque(maxlen=100)
_STATE_HISTORY = deque(maxlen=200)
_LAST_ALERT_TS = {}  # alert_type -> timestamp (anti-spam)

ALERT_COOLDOWN_SEC = 600  # 10min between same-type alerts

# Thresholds — tunable
THRESHOLDS = {
    'wr_below': 0.40,           # alert if last-50 WR < 40%
    'expectancy_below': -0.005, # alert if expectancy < -0.5% (negative)
    'consecutive_losses': 8,    # alert on 8+ losses in a row
    'equity_drop_pct': 0.05,    # alert if equity drops 5%+ from peak
    'unrealized_drawdown': 0.10,# alert if unrealized PnL < -10% of equity
    'cross_margin_count': 5,    # alert if 5+ positions are cross-margin
}

def record_open(coin, side, entry, size, lev, margin_type):
    _RECENT_OPENS.append({
        'ts': time.time(), 'coin': coin, 'side': side, 'entry': entry,
        'size': size, 'lev': lev, 'margin_type': margin_type
    })

def record_close(coin, pnl_pct, pnl_usd, hold_min, exit_type='?'):
    _RECENT_CLOSES.append({
        'ts': time.time(), 'coin': coin, 'pnl_pct': pnl_pct, 'pnl_usd': pnl_usd,
        'hold_min': hold_min, 'exit_type': exit_type
    })

def _alert(alert_type, severity, message, data=None):
    """Add alert if not in cooldown."""
    now = time.time()
    last = _LAST_ALERT_TS.get(alert_type, 0)
    if (now - last) < ALERT_COOLDOWN_SEC: return
    _LAST_ALERT_TS[alert_type] = now
    _ALERTS.append({
        'ts': now, 'iso': datetime.datetime.utcnow().isoformat(),
        'type': alert_type, 'severity': severity, 'message': message, 'data': data
    })

def get_stats():
    """Compute rolling stats from recent closes."""
    if not _RECENT_CLOSES:
        return {'count': 0, 'wr': None, 'expectancy_pct': None}
    
    closes = list(_RECENT_CLOSES)
    pnls = [c['pnl_pct'] for c in closes]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    
    wr = len(wins) / len(pnls) if pnls else 0
    avg_win = sum(wins) / max(len(wins), 1) if wins else 0
    avg_loss = sum(losses) / max(len(losses), 1) if losses else 0
    expectancy = sum(pnls) / len(pnls) if pnls else 0
    
    avg_win_usd = sum(c['pnl_usd'] for c in closes if c['pnl_usd'] > 0) / max(len(wins), 1)
    avg_loss_usd = sum(c['pnl_usd'] for c in closes if c['pnl_usd'] < 0) / max(len(losses), 1)
    
    # Consecutive losses
    consec = 0
    for c in reversed(closes):
        if c['pnl_pct'] < 0: consec += 1
        else: break
    
    return {
        'count': len(closes), 'wr': round(wr * 100, 1),
        'avg_win_pct': round(avg_win * 100, 3), 'avg_loss_pct': round(avg_loss * 100, 3),
        'avg_win_usd': round(avg_win_usd, 2), 'avg_loss_usd': round(avg_loss_usd, 2),
        'expectancy_pct': round(expectancy * 100, 3),
        'consec_losses': consec,
        'last_close_ts': closes[-1]['ts'] if closes else None,
    }

def check_health(equity, peak_equity, positions):
    """Run all health checks. Generate alerts."""
    stats = get_stats()
    
    # Stat-based alerts
    if stats['count'] >= 10:
        if stats['wr'] is not None and stats['wr'] < THRESHOLDS['wr_below'] * 100:
            _alert('wr_low', 'WARN', f"Rolling WR {stats['wr']}% < {THRESHOLDS['wr_below']*100}% threshold (n={stats['count']})", stats)
        if stats['expectancy_pct'] is not None and stats['expectancy_pct'] < THRESHOLDS['expectancy_below']*100:
            _alert('expectancy_neg', 'CRITICAL', f"Negative expectancy: {stats['expectancy_pct']}% per trade. System bleeding.", stats)
    
    if stats['consec_losses'] >= THRESHOLDS['consecutive_losses']:
        _alert('losing_streak', 'CRITICAL', f"{stats['consec_losses']} consecutive losses. Possible regime mismatch.", stats)
    
    # Equity-based alerts
    if peak_equity > 0:
        drop_pct = (peak_equity - equity) / peak_equity
        if drop_pct >= THRESHOLDS['equity_drop_pct']:
            _alert('equity_drop', 'WARN', f"Equity ${equity:.2f} is {drop_pct*100:.1f}% below peak ${peak_equity:.2f}", {'equity': equity, 'peak': peak_equity})
    
    # Unrealized drawdown
    if positions:
        unreal = sum(p.get('pnl', 0) for p in positions.values() if p)
        if equity > 0 and unreal / equity < -THRESHOLDS['unrealized_drawdown']:
            _alert('unreal_dd', 'WARN', f"Unrealized PnL ${unreal:+.2f} = {unreal/equity*100:.1f}% of equity", {'unreal': unreal, 'equity': equity})
    
    # Cross-margin warning
    if positions:
        cross_count = sum(1 for p in positions.values() if p and p.get('margin_type') == 'cross')
        if cross_count >= THRESHOLDS['cross_margin_count']:
            _alert('cross_margin', 'WARN', f"{cross_count} positions on cross margin (should be isolated)", None)
    
    # State history snapshot
    _STATE_HISTORY.append({
        'ts': time.time(), 'equity': equity, 'peak': peak_equity,
        'n_pos': len(positions), 'wr': stats.get('wr'), 'expectancy': stats.get('expectancy_pct')
    })

def status():
    """Return monitoring status for /monitor endpoint."""
    return {
        'rolling_stats': get_stats(),
        'recent_alerts': list(_ALERTS)[-20:],
        'alert_count': len(_ALERTS),
        'state_samples': len(_STATE_HISTORY),
        'thresholds': THRESHOLDS,
    }

def get_alerts(severity_filter=None):
    """Return all alerts, optionally filtered by severity."""
    alerts = list(_ALERTS)
    if severity_filter:
        alerts = [a for a in alerts if a['severity'] == severity_filter]
    return alerts
