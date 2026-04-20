"""Dynamic per-coin size multiplier based on live performance drift vs OOS expectation.
If a coin prints 2x OOS expected trades → increase size 1.3x
If a coin prints 0.3x OOS expected trades → decrease size 0.5x
If actual WR > expected → increase size up to 1.5x
If actual WR < expected - 10pp → decrease size to 0.4x
Applied on top of tier base size. Updates every 4h.
"""
import time, json, os, threading

STATE_PATH = '/var/data/coin_sizing.json'
LOCK = threading.Lock()
REFRESH_SEC = 4 * 3600  # 4h

# Per-coin tracking: rolling trade count + WR vs OOS expected
# {coin: {'trades_7d': [(ts, win, pnl)], 'mult': 1.0, 'updated_at': ts}}
_state = {}

# Bounds
MAX_MULT = 1.5
MIN_MULT = 0.4

def _load():
    if os.path.exists(STATE_PATH):
        try:
            loaded = json.load(open(STATE_PATH))
            with LOCK:
                for c, v in loaded.items(): _state[c] = v
        except: pass

def _save():
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with LOCK:
            snap = {c: dict(s) for c, s in _state.items()}
        json.dump(snap, open(STATE_PATH, 'w'))
    except: pass

_load()

def _ensure(coin):
    if coin not in _state:
        _state[coin] = {'trades_7d':[], 'mult':1.0, 'updated_at':0}

def _prune(coin):
    cutoff = time.time() - 7 * 86400
    _state[coin]['trades_7d'] = [t for t in _state[coin]['trades_7d'] if t[0] >= cutoff]

def record_trade(coin, win, pnl_pct, oos_expected_trades_per_day, oos_expected_wr):
    """Called on every trade close. Recomputes mult if stale."""
    now = time.time()
    with LOCK:
        _ensure(coin)
        _state[coin]['trades_7d'].append((now, win, pnl_pct))
        _prune(coin)
        # Only recompute mult every 4h to avoid thrash
        if now - _state[coin]['updated_at'] < REFRESH_SEC:
            _save()
            return
        # Compute actual metrics
        trades = _state[coin]['trades_7d']
        n = len(trades)
        if n < 5:
            _state[coin]['mult'] = 1.0  # neutral until data
            _state[coin]['updated_at'] = now
            _save()
            return
        days = min(7, (now - trades[0][0]) / 86400)
        if days < 0.5:
            _save()
            return
        actual_tpd = n / max(days, 0.5)
        actual_wr = sum(1 for _, w, _ in trades if w) / n * 100
        # Frequency multiplier: 0.5x at 0.3x expected, 1.3x at 2x expected
        if oos_expected_trades_per_day > 0:
            tpd_ratio = actual_tpd / oos_expected_trades_per_day
            if tpd_ratio < 0.3: freq_mult = 0.5
            elif tpd_ratio < 0.7: freq_mult = 0.8
            elif tpd_ratio < 1.3: freq_mult = 1.0
            elif tpd_ratio < 2.0: freq_mult = 1.15
            else: freq_mult = 1.3
        else:
            freq_mult = 1.0
        # WR multiplier
        wr_delta = actual_wr - oos_expected_wr
        if wr_delta < -15: wr_mult = 0.4
        elif wr_delta < -10: wr_mult = 0.6
        elif wr_delta < -5: wr_mult = 0.8
        elif wr_delta < 5: wr_mult = 1.0
        elif wr_delta < 10: wr_mult = 1.2
        else: wr_mult = 1.5
        combined = min(MAX_MULT, max(MIN_MULT, freq_mult * wr_mult))
        _state[coin]['mult'] = combined
        _state[coin]['updated_at'] = now
        _state[coin]['actual_tpd'] = round(actual_tpd, 2)
        _state[coin]['actual_wr'] = round(actual_wr, 1)
    _save()

def get_mult(coin):
    """Get current size multiplier for coin. Default 1.0."""
    with LOCK:
        if coin not in _state: return 1.0
        return _state[coin]['mult']

def status():
    with LOCK:
        return {c: {
            'mult': s.get('mult', 1.0),
            'actual_tpd': s.get('actual_tpd', 0),
            'actual_wr': s.get('actual_wr', 0),
            'trades_7d': len(s.get('trades_7d', [])),
        } for c, s in _state.items()}
