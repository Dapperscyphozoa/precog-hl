"""Risk auto-ladder. Rolling 100-trade WR gates risk tier.
Tier 0: 4% | Tier 1: 6% (≥60% WR, 100+ trades) | Tier 2: 8% (≥60% WR, 300+ trades, 7d Tier 1)
Downgrade: rolling 50-trade WR <50% → drop one tier.
"""
import time, threading

_LOCK = threading.Lock()
_TRADE_HIST = []  # list of (ts, win_bool)
_STATE = {'tier': 0, 'risk': 0.04, 'last_promoted_ts': 0, 'rolling_wr_100': 0.0, 'rolling_wr_50': 0.0}

TIERS = [
    {'risk': 0.04, 'min_trades': 0,   'min_wr': 0.0,  'hold_days': 0},
    {'risk': 0.06, 'min_trades': 100, 'min_wr': 0.60, 'hold_days': 3},
    {'risk': 0.08, 'min_trades': 300, 'min_wr': 0.60, 'hold_days': 7},
    {'risk': 0.10, 'min_trades': 500, 'min_wr': 0.62, 'hold_days': 14},
]

def record_trade(is_win):
    ts = time.time()
    with _LOCK:
        _TRADE_HIST.append((ts, 1 if is_win else 0))
        if len(_TRADE_HIST) > 500: _TRADE_HIST.pop(0)
        _evaluate_tier()

def _evaluate_tier():
    now = time.time()
    if len(_TRADE_HIST) >= 100:
        last100 = _TRADE_HIST[-100:]
        _STATE['rolling_wr_100'] = sum(w for _, w in last100) / 100
    if len(_TRADE_HIST) >= 50:
        last50 = _TRADE_HIST[-50:]
        _STATE['rolling_wr_50'] = sum(w for _, w in last50) / 50
    # Downgrade check
    if _STATE['tier'] > 0 and len(_TRADE_HIST) >= 50 and _STATE['rolling_wr_50'] < 0.50:
        _STATE['tier'] = max(0, _STATE['tier'] - 1)
        _STATE['risk'] = TIERS[_STATE['tier']]['risk']
        _STATE['last_promoted_ts'] = now
        return
    # Promotion check
    if _STATE['tier'] + 1 < len(TIERS):
        next_t = TIERS[_STATE['tier'] + 1]
        days_since = (now - _STATE['last_promoted_ts']) / 86400
        if (len(_TRADE_HIST) >= next_t['min_trades']
            and _STATE['rolling_wr_100'] >= next_t['min_wr']
            and days_since >= next_t['hold_days']):
            _STATE['tier'] += 1
            _STATE['risk'] = next_t['risk']
            _STATE['last_promoted_ts'] = now

def get_risk():
    with _LOCK: return _STATE['risk']

def get_state():
    with _LOCK: return dict(_STATE) | {'trades_logged': len(_TRADE_HIST)}
