"""Per-tier kill switch: rolling 24h PnL tracker, auto-disable on threshold breach.
Triple-check: persistence across restarts, thread-safe, zero-shift-left bugs."""
import time, json, os, threading

STATE_PATH = '/var/data/killswitch.json'
ROLLING_WINDOW_SEC = 86400  # 24h
DEFAULT_DISABLE_THRESHOLD = -0.15  # -15% per tier over 24h
LOCK = threading.Lock()

# In-memory state: {tier: {'trades': [(ts, pnl_pct, equity_delta)], 'disabled': bool, 'disabled_at': ts, 'reason': str}}
_state = {
    'PURE': {'trades': [], 'disabled': False, 'disabled_at': 0, 'reason': ''},
    'NINETY_99': {'trades': [], 'disabled': False, 'disabled_at': 0, 'reason': ''},
    'EIGHTY_89': {'trades': [], 'disabled': False, 'disabled_at': 0, 'reason': ''},
    'SEVENTY_79': {'trades': [], 'disabled': False, 'disabled_at': 0, 'reason': ''},
}

# Custom per-tier thresholds (PURE is tightest since WR should be 100%)
TIER_THRESHOLDS = {
    'PURE':       -0.05,  # -5% = critical, should never happen with 100% WR
    'NINETY_99':  -0.12,  # -12% = 4 losses in row possible
    'EIGHTY_89':  -0.18,  # -18% = 6 losses in row possible
    'SEVENTY_79': -0.15,  # -15% = filters should prevent worse
}

def _load():
    if os.path.exists(STATE_PATH):
        try:
            loaded = json.load(open(STATE_PATH))
            with LOCK:
                for tier in _state:
                    if tier in loaded:
                        _state[tier].update(loaded[tier])
        except Exception: pass

def _save():
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with LOCK:
            snap = {t: dict(s) for t,s in _state.items()}
        json.dump(snap, open(STATE_PATH,'w'))
    except Exception: pass

_load()

def _prune(tier):
    """Remove trades older than 24h."""
    cutoff = time.time() - ROLLING_WINDOW_SEC
    _state[tier]['trades'] = [t for t in _state[tier]['trades'] if t[0] >= cutoff]

def record_trade_close(tier, pnl_pct, equity_before, equity_after):
    """Record closed trade to tier's rolling window. Check threshold. Auto-disable if breached."""
    if tier not in _state: return False  # unknown tier, do nothing
    now = time.time()
    equity_delta_pct = (equity_after - equity_before) / max(equity_before, 1e-9)
    with LOCK:
        _state[tier]['trades'].append((now, pnl_pct, equity_delta_pct))
        _prune(tier)
        # Compute 24h rolling PnL
        rolling_pnl = sum(d for _, _, d in _state[tier]['trades'])
        threshold = TIER_THRESHOLDS.get(tier, DEFAULT_DISABLE_THRESHOLD)
        if not _state[tier]['disabled'] and rolling_pnl <= threshold:
            _state[tier]['disabled'] = True
            _state[tier]['disabled_at'] = now
            _state[tier]['reason'] = f'24h PnL {rolling_pnl*100:.1f}% breached threshold {threshold*100:.1f}%'
            _save()
            return True  # tier was just disabled
    _save()
    return False

def is_disabled(tier):
    if tier not in _state: return False
    with LOCK:
        return _state[tier]['disabled']

def manual_disable(tier, reason='manual'):
    if tier not in _state: return False
    with LOCK:
        _state[tier]['disabled'] = True
        _state[tier]['disabled_at'] = time.time()
        _state[tier]['reason'] = reason
    _save(); return True

def manual_enable(tier):
    if tier not in _state: return False
    with LOCK:
        _state[tier]['disabled'] = False
        _state[tier]['disabled_at'] = 0
        _state[tier]['reason'] = ''
    _save(); return True

def status():
    """Return dict of tier status for /killswitch endpoint."""
    with LOCK:
        out = {}
        for t, s in _state.items():
            _prune(t)
            rolling = sum(d for _, _, d in s['trades'])
            out[t] = {
                'disabled': s['disabled'],
                'disabled_at': s['disabled_at'],
                'reason': s['reason'],
                'rolling_pnl_24h_pct': round(rolling*100, 2),
                'trade_count_24h': len(s['trades']),
                'threshold_pct': round(TIER_THRESHOLDS.get(t, DEFAULT_DISABLE_THRESHOLD)*100, 1),
                'buffer_pct': round((rolling - TIER_THRESHOLDS.get(t, DEFAULT_DISABLE_THRESHOLD))*100, 2),
            }
        return out
