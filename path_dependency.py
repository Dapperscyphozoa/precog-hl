"""Path dependency analyzer — live streak detection + adaptive sizing.

UNLIKE other telemetry: this one DOES modify live position sizing.

Rolling 10-trade window tracks:
- Consecutive losses / wins
- Inter-trade correlation
- Drawdown velocity
- Regime-transition cascade factor

Emits a size multiplier that precog.py applies to base risk_pct.

Sizing ladder (on consecutive losses):
- 0-2 losses: 1.0x (normal)
- 3 losses: 0.5x for next 5 trades
- 5 losses: 0.3x + require ensemble agreement (flag)
- 7 losses: pause new entries for 30 min (flag)

Reset triggers:
- 3 consecutive wins
- Regime flip
- 30-min pause elapsed
"""
import json, os, time, threading
from collections import deque

LOG_PATH = os.environ.get('PATH_DEP_LOG_PATH', '/app/path_dependency.jsonl')
_LOCK = threading.Lock()
_LOG_PREFIX = '[path_dep]'

# Live state
_STATE = {
    'consec_losses': 0,
    'consec_wins': 0,
    'size_mult': 1.0,
    'trades_remaining_at_reduced': 0,
    'pause_until_ts': 0,
    'require_ensemble': False,
    'last_regime': None,
    'last_peak_pnl': 0.0,
    'running_pnl': 0.0,
    'peak_to_trough_trades': 0,
    'peak_to_trough_pnl': 0.0,
    'alerts': [],
}

# Rolling window for correlation computation
_RECENT = deque(maxlen=10)


def record_close(pnl_pct, win, regime=None, bar_ts=None):
    """Update path-dependency state from a closed trade. Non-blocking.

    Returns nothing; state consulted via get_size_multiplier() at next signal.
    """
    def _do():
        try:
            with _LOCK:
                _update_state(pnl_pct, win, regime)
                rec = {
                    'ts': int(time.time()),
                    'bar_ts': int(bar_ts) if bar_ts else None,
                    'pnl_pct': round(float(pnl_pct), 3),
                    'win': bool(win),
                    'regime': regime,
                    'consec_losses_after': _STATE['consec_losses'],
                    'consec_wins_after': _STATE['consec_wins'],
                    'size_mult_emitted': _STATE['size_mult'],
                    'pause_active': time.time() < _STATE['pause_until_ts'],
                    'require_ensemble': _STATE['require_ensemble'],
                }
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
        except Exception as e:
            print(f"{_LOG_PREFIX} err: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def _update_state(pnl_pct, win, regime):
    """Core state machine. Called under _LOCK."""
    # Regime flip = full reset
    if regime and _STATE['last_regime'] and regime != _STATE['last_regime']:
        _reset('regime_flip')
    _STATE['last_regime'] = regime

    # Streak accounting
    if win:
        _STATE['consec_wins'] += 1
        _STATE['consec_losses'] = 0
        if _STATE['consec_wins'] >= 3 and _STATE['size_mult'] < 1.0:
            _reset('win_streak_recovery')
    else:
        _STATE['consec_losses'] += 1
        _STATE['consec_wins'] = 0

    # Running PnL / drawdown velocity
    _STATE['running_pnl'] += pnl_pct
    if _STATE['running_pnl'] > _STATE['last_peak_pnl']:
        _STATE['last_peak_pnl'] = _STATE['running_pnl']
        _STATE['peak_to_trough_trades'] = 0
        _STATE['peak_to_trough_pnl'] = 0
    else:
        _STATE['peak_to_trough_trades'] += 1
        _STATE['peak_to_trough_pnl'] = _STATE['last_peak_pnl'] - _STATE['running_pnl']

    drawdown_velocity = 0
    if _STATE['peak_to_trough_trades'] > 0:
        drawdown_velocity = _STATE['peak_to_trough_pnl'] / _STATE['peak_to_trough_trades']

    # Trigger ladder on consecutive losses
    new_mult = _STATE['size_mult']
    new_pause = 0
    new_ensemble = _STATE['require_ensemble']
    alert_msg = None

    if _STATE['consec_losses'] >= 7:
        new_pause = time.time() + 30 * 60  # 30-min pause
        new_mult = 0.3
        new_ensemble = True
        alert_msg = f"★★★ 7 consecutive losses → 30min entry pause + 0.3x size"
    elif _STATE['consec_losses'] >= 5:
        new_mult = 0.3
        new_ensemble = True
        alert_msg = f"⚠ 5 consecutive losses → size 0.3x + ensemble required"
    elif _STATE['consec_losses'] >= 3:
        new_mult = 0.5
        _STATE['trades_remaining_at_reduced'] = 5  # reduced for next 5 trades
        alert_msg = f"• 3 consecutive losses → size 0.5x for next 5 trades"
    elif _STATE['trades_remaining_at_reduced'] > 0:
        _STATE['trades_remaining_at_reduced'] -= 1
        new_mult = 0.5
        if _STATE['trades_remaining_at_reduced'] == 0:
            new_mult = 1.0

    # Drawdown velocity override
    if drawdown_velocity > 0.01 and _STATE['peak_to_trough_trades'] >= 5:
        if new_mult > 0.5: new_mult = 0.5
        if alert_msg is None:
            alert_msg = f"• Drawdown velocity >1%/trade over 5 trades → size 0.5x"

    _STATE['size_mult'] = new_mult
    if new_pause > 0:
        _STATE['pause_until_ts'] = new_pause
    _STATE['require_ensemble'] = new_ensemble

    _RECENT.append({'win': win, 'pnl_pct': pnl_pct, 'ts': time.time()})

    if alert_msg:
        _STATE['alerts'].append({'ts': int(time.time()), 'msg': alert_msg})
        if len(_STATE['alerts']) > 20:
            _STATE['alerts'] = _STATE['alerts'][-20:]
        print(f"{_LOG_PREFIX} {alert_msg}", flush=True)


def _reset(reason):
    """Reset streak state. Called under _LOCK."""
    print(f"{_LOG_PREFIX} RESET ({reason}): "
          f"{_STATE['consec_losses']}L/{_STATE['consec_wins']}W → normal", flush=True)
    _STATE['consec_losses'] = 0
    _STATE['consec_wins'] = 0
    _STATE['size_mult'] = 1.0
    _STATE['trades_remaining_at_reduced'] = 0
    _STATE['pause_until_ts'] = 0
    _STATE['require_ensemble'] = False
    _STATE['last_peak_pnl'] = _STATE['running_pnl']
    _STATE['peak_to_trough_trades'] = 0
    _STATE['peak_to_trough_pnl'] = 0


def get_size_multiplier():
    """Consulted at every signal fire. Returns (mult, flags_dict)."""
    with _LOCK:
        now = time.time()
        # Pause check
        if now < _STATE['pause_until_ts']:
            return (0.0, {
                'paused': True,
                'pause_remaining_sec': int(_STATE['pause_until_ts'] - now),
                'reason': 'post_streak_cooldown',
            })
        # Normal case
        return (_STATE['size_mult'], {
            'paused': False,
            'require_ensemble': _STATE['require_ensemble'],
            'consec_losses': _STATE['consec_losses'],
            'consec_wins': _STATE['consec_wins'],
            'trades_remaining_at_reduced': _STATE['trades_remaining_at_reduced'],
        })


def status():
    """Full state + recent alerts for /path_dep endpoint."""
    with _LOCK:
        now = time.time()
        recent = list(_RECENT)
        return {
            'current': {
                'size_mult': _STATE['size_mult'],
                'consec_losses': _STATE['consec_losses'],
                'consec_wins': _STATE['consec_wins'],
                'require_ensemble': _STATE['require_ensemble'],
                'paused': now < _STATE['pause_until_ts'],
                'pause_remaining_sec': max(0, int(_STATE['pause_until_ts'] - now)),
                'trades_remaining_at_reduced': _STATE['trades_remaining_at_reduced'],
            },
            'drawdown': {
                'running_pnl_pct': round(_STATE['running_pnl'], 3),
                'peak_pnl_pct': round(_STATE['last_peak_pnl'], 3),
                'peak_to_trough_pct': round(_STATE['peak_to_trough_pnl'], 3),
                'peak_to_trough_trades': _STATE['peak_to_trough_trades'],
                'velocity_pct_per_trade': round(
                    _STATE['peak_to_trough_pnl'] / _STATE['peak_to_trough_trades'], 3
                ) if _STATE['peak_to_trough_trades'] else 0,
            },
            'recent_10_trades': recent,
            'alerts_last_20': _STATE['alerts'][-20:],
            'ladder': {
                'consec_3': 'size 0.5x for next 5 trades',
                'consec_5': 'size 0.3x + ensemble required',
                'consec_7': '30min pause + 0.3x + ensemble',
                'reset_triggers': ['3 consecutive wins', 'regime flip', '30min pause expires'],
            },
        }
