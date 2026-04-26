"""Per-coin killswitch. Rolling 24h WR + PnL tracker per coin.
Auto-disables a single coin when:
- WR drops >20pp below OOS expectation AND n>=10 trades, OR
- 3 consecutive losses, OR
- Manual override
Does NOT disable the tier. Surgical shutoff.
"""
import time, json, os, threading

STATE_PATH = '/var/data/coin_killswitch.json'
ROLLING_WINDOW_SEC = 86400
CONSEC_LOSS_TRIGGER = 3
WR_DROP_PP_TRIGGER = 20  # percentage points below expected
MIN_TRADES_FOR_WR_CHECK = 10
LOCK = threading.RLock()
# 2026-04-26: was threading.Lock(). Functions like record_trade_close()
# acquire LOCK, then call _save() which ALSO does `with LOCK:`. Non-
# reentrant Lock deadlocks the calling thread. RLock allows re-entry by
# the same thread, restoring the intended behavior. (Same fix pattern
# applied to engine_killswitch.py earlier — this was the original bug
# I noticed there, just hadn't been backported here.) Likely cause of
# coin_killswitch never auto-disabling a coin in production despite
# the conditions being hit — calls to _save() inside lock-held branches
# silently hung the calling thread until the next request unblocked it.

# {coin: {'trades': [(ts, win_bool, pnl_pct)], 'disabled': bool, 'disabled_at': ts, 'reason': str, 'consec_losses': int}}
_state = {}

def _load():
    if os.path.exists(STATE_PATH):
        try:
            loaded = json.load(open(STATE_PATH))
            with LOCK:
                for coin, v in loaded.items():
                    _state[coin] = v
        except Exception: pass

def _save():
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with LOCK:
            snap = {c: dict(s) for c, s in _state.items()}
        json.dump(snap, open(STATE_PATH, 'w'))
    except Exception: pass

_load()


# 2026-04-26: per-coin expected_wr lookup from all_grid_results.json.
# Default callers fall back to hardcoded 75 (legacy), but if the grid
# results file is present, it provides per-coin baselines (typically
# 50-75% from honest backtests). README claims the active strategy
# averages 80.2% — using a per-coin baseline instead of one-size-fits-all
# tightens the trigger from 55%-WR-drop to coin-specific.
_EXPECTED_WR_CACHE = None
_EXPECTED_WR_PATH = os.environ.get('GRID_RESULTS_PATH', 'all_grid_results.json')

def _load_expected_wr_map():
    """Load coin -> base_wr from grid results. Cached. Best-effort."""
    global _EXPECTED_WR_CACHE
    if _EXPECTED_WR_CACHE is not None:
        return _EXPECTED_WR_CACHE
    out = {}
    try:
        with open(_EXPECTED_WR_PATH, 'r') as f:
            arr = json.load(f)
        for item in (arr or []):
            t = (item.get('ticker') or '').upper()
            wr = item.get('base_wr')
            if t and wr is not None:
                try:
                    out[t] = float(wr)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    _EXPECTED_WR_CACHE = out
    return out


def get_expected_wr(coin, default=75.0):
    """Per-coin expected WR%. Returns default if not in grid results."""
    if not coin:
        return default
    return _load_expected_wr_map().get(coin.upper(), default)


def _prune(coin):
    cutoff = time.time() - ROLLING_WINDOW_SEC
    _state[coin]['trades'] = [t for t in _state[coin]['trades'] if t[0] >= cutoff]

def _ensure(coin):
    if coin not in _state:
        _state[coin] = {'trades':[], 'disabled':False, 'disabled_at':0, 'reason':'', 'consec_losses':0}

def record_trade_close(coin, pnl_pct, expected_wr_pct=None):
    """Record a close. Evaluates disable conditions. Returns True if just disabled.

    If `expected_wr_pct` is None, looks up per-coin baseline from
    all_grid_results.json (falling back to 75% if coin missing). This makes
    the WR-drop trigger coin-aware: a coin with backtest 60% WR triggers at
    40%, not at the legacy hardcoded 55%.
    """
    if expected_wr_pct is None:
        expected_wr_pct = get_expected_wr(coin, default=75.0)
    now = time.time()
    win = pnl_pct > 0
    with LOCK:
        _ensure(coin)
        _prune(coin)
        _state[coin]['trades'].append((now, win, pnl_pct))
        # Update consecutive losses
        if win:
            _state[coin]['consec_losses'] = 0
        else:
            _state[coin]['consec_losses'] += 1
        # Already disabled? skip further checks
        if _state[coin]['disabled']:
            _save(); return False
        # Trigger 1: consecutive losses
        if _state[coin]['consec_losses'] >= CONSEC_LOSS_TRIGGER:
            _state[coin]['disabled'] = True
            _state[coin]['disabled_at'] = now
            _state[coin]['reason'] = f'{_state[coin]["consec_losses"]} consecutive losses'
            _save(); return True
        # Trigger 2: WR drop vs expectation, if enough trades
        trades = _state[coin]['trades']
        if len(trades) >= MIN_TRADES_FOR_WR_CHECK:
            wins = sum(1 for _, w, _ in trades if w)
            actual_wr = wins / len(trades) * 100
            if actual_wr < expected_wr_pct - WR_DROP_PP_TRIGGER:
                _state[coin]['disabled'] = True
                _state[coin]['disabled_at'] = now
                _state[coin]['reason'] = f'WR {actual_wr:.0f}% vs expected {expected_wr_pct:.0f}% ({WR_DROP_PP_TRIGGER}pp drop)'
                _save(); return True
    _save()
    return False

def is_disabled(coin):
    with LOCK:
        if coin not in _state: return False
        return _state[coin]['disabled']

def manual_disable(coin, reason='manual'):
    with LOCK:
        _ensure(coin)
        _state[coin]['disabled'] = True
        _state[coin]['disabled_at'] = time.time()
        _state[coin]['reason'] = reason
    _save(); return True

def manual_enable(coin):
    with LOCK:
        if coin not in _state: return False
        _state[coin]['disabled'] = False
        _state[coin]['disabled_at'] = 0
        _state[coin]['reason'] = ''
        _state[coin]['consec_losses'] = 0
    _save(); return True

def status():
    with LOCK:
        out = {}
        for coin, s in _state.items():
            _prune(coin)
            n = len(s['trades'])
            wins = sum(1 for _, w, _ in s['trades'] if w)
            wr = wins/n*100 if n else 0
            pnl = sum(p for _, _, p in s['trades'])
            out[coin] = {
                'disabled': s['disabled'],
                'reason': s['reason'],
                'trades_24h': n,
                'wr_24h': round(wr, 1),
                'pnl_24h_sum_pct': round(pnl, 2),
                'consec_losses': s['consec_losses'],
            }
        return out
