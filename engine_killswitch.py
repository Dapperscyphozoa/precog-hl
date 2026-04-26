"""Per-engine killswitch. Rolling 30-trade window per engine.

Auto-disables an engine when (and ONLY when min sample size is met) any of:
  - Cumulative PnL pct sum <= PNL_DROP_PCT (default -3%)
  - Win rate <= WR_FLOOR_PCT (default 35%)
  - Consecutive losses >= CONSEC_LOSS_TRIGGER (default 5)

Sample-size gate (MIN_TRADES) is NON-NEGOTIABLE — disabling an engine on 3
trades of bad luck is exactly the noise-driven mistake the audit warned against.

This module is feature-flagged OFF by default. Set ENGINE_KILLSWITCH_ENABLED=1
to activate. With the flag off, record_trade_close() still updates state (so
when you flip the flag you have history), but is_disabled() always returns
False.

Mirrors the API of coin_killswitch.py so the integration pattern is identical.
"""
import os
import time
import json
import threading

STATE_PATH = os.environ.get('ENGINE_KILLSWITCH_PATH', '/var/data/engine_killswitch.json')

# Tuning — env-overridable so the operator can tighten/loosen without redeploy.
ROLLING_WINDOW_SEC = int(os.environ.get('ENGINE_KS_WINDOW_SEC', 86400 * 3))   # 3 days
MIN_TRADES = int(os.environ.get('ENGINE_KS_MIN_TRADES', 15))                  # noise floor
PNL_DROP_PCT = float(os.environ.get('ENGINE_KS_PNL_DROP_PCT', -0.03))         # -3% sum
WR_FLOOR_PCT = float(os.environ.get('ENGINE_KS_WR_FLOOR_PCT', 35.0))          # %
CONSEC_LOSS_TRIGGER = int(os.environ.get('ENGINE_KS_CONSEC_LOSS', 5))

# Master flag. 'off' = pure observation. 'on' = is_disabled() can return True.
ENABLED = os.environ.get('ENGINE_KILLSWITCH_ENABLED', '0').lower() in ('1', 'true', 'on', 'yes')

LOCK = threading.RLock()  # reentrant: _save() under outer with-lock won't deadlock

# {engine: {'trades': [(ts, win_bool, pnl_pct)], 'disabled': bool,
#           'disabled_at': ts, 'reason': str, 'consec_losses': int}}
_state = {}


def _load():
    if not os.path.exists(STATE_PATH):
        return
    try:
        loaded = json.load(open(STATE_PATH))
        with LOCK:
            for engine, v in loaded.items():
                _state[engine] = v
    except Exception:
        pass


def _save():
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with LOCK:
            snap = {e: dict(s) for e, s in _state.items()}
        with open(STATE_PATH, 'w') as f:
            json.dump(snap, f)
    except Exception:
        pass


_load()


def _prune(engine):
    cutoff = time.time() - ROLLING_WINDOW_SEC
    _state[engine]['trades'] = [t for t in _state[engine]['trades'] if t[0] >= cutoff]


def _ensure(engine):
    if engine not in _state:
        _state[engine] = {
            'trades': [],
            'disabled': False,
            'disabled_at': 0,
            'reason': '',
            'consec_losses': 0,
        }


def record_trade_close(engine, pnl_pct):
    """Record a closed trade. Evaluate disable conditions. Returns True if just disabled.

    Always updates state, regardless of the master flag — that way when the
    operator flips ENABLED on, history is already populated.
    """
    if not engine:
        return False
    now = time.time()
    win = pnl_pct is not None and pnl_pct > 0
    pnl = float(pnl_pct) if pnl_pct is not None else 0.0
    with LOCK:
        _ensure(engine)
        _prune(engine)
        _state[engine]['trades'].append((now, win, pnl))
        if win:
            _state[engine]['consec_losses'] = 0
        else:
            _state[engine]['consec_losses'] += 1

        if _state[engine]['disabled']:
            _save()
            return False

        trades = _state[engine]['trades']
        n = len(trades)
        if n < MIN_TRADES:
            _save()
            return False

        wins = sum(1 for _, w, _ in trades if w)
        wr_pct = wins / n * 100.0
        pnl_sum = sum(p for _, _, p in trades)
        consec = _state[engine]['consec_losses']

        triggers = []
        if pnl_sum <= PNL_DROP_PCT * 100.0:
            triggers.append(f'pnl_sum {pnl_sum:.2f}% <= {PNL_DROP_PCT*100:.2f}%')
        if wr_pct <= WR_FLOOR_PCT:
            triggers.append(f'wr {wr_pct:.1f}% <= {WR_FLOOR_PCT:.1f}%')
        if consec >= CONSEC_LOSS_TRIGGER:
            triggers.append(f'consec_losses {consec} >= {CONSEC_LOSS_TRIGGER}')

        if triggers:
            _state[engine]['disabled'] = True
            _state[engine]['disabled_at'] = now
            _state[engine]['reason'] = '; '.join(triggers)
            _save()
            return True

    _save()
    return False


def is_disabled(engine):
    """Return True iff master flag is on AND engine has been auto-disabled.

    With ENABLED=False this always returns False, so wiring this into the
    entry path is safe with the flag off — no behavior change.
    """
    if not ENABLED:
        return False
    if not engine:
        return False
    with LOCK:
        s = _state.get(engine)
        return bool(s and s.get('disabled'))


def manual_disable(engine, reason='manual'):
    if not engine:
        return False
    with LOCK:
        _ensure(engine)
        _state[engine]['disabled'] = True
        _state[engine]['disabled_at'] = time.time()
        _state[engine]['reason'] = reason
    _save()
    return True


def manual_enable(engine):
    if not engine:
        return False
    with LOCK:
        if engine not in _state:
            return False
        _state[engine]['disabled'] = False
        _state[engine]['disabled_at'] = 0
        _state[engine]['reason'] = ''
        _state[engine]['consec_losses'] = 0
    _save()
    return True


def status():
    """Snapshot per-engine state. Suitable for /engines or /killswitch endpoint."""
    with LOCK:
        out = {
            '_meta': {
                'enabled': ENABLED,
                'window_sec': ROLLING_WINDOW_SEC,
                'min_trades': MIN_TRADES,
                'pnl_drop_pct': PNL_DROP_PCT,
                'wr_floor_pct': WR_FLOOR_PCT,
                'consec_loss_trigger': CONSEC_LOSS_TRIGGER,
            },
        }
        for engine, s in _state.items():
            _prune(engine)
            n = len(s['trades'])
            wins = sum(1 for _, w, _ in s['trades'] if w)
            wr = (wins / n * 100.0) if n else 0.0
            pnl_sum = sum(p for _, _, p in s['trades'])
            out[engine] = {
                'disabled': s['disabled'],
                'reason': s['reason'],
                'trades_in_window': n,
                'wr_pct': round(wr, 1),
                'pnl_sum_pct': round(pnl_sum, 2),
                'consec_losses': s['consec_losses'],
                'sample_meets_min': n >= MIN_TRADES,
            }
        return out
