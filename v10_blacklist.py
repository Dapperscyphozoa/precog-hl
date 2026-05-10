"""
V10 coin blacklist + consecutive-loss tracker.

Persistent JSON-backed counter per coin:
  - increments on losing close (pnl_pct <= 0)
  - resets to 0 on winning close (pnl_pct > 0)
  - blacklisted when counter >= BLACKLIST_LOSS_THRESHOLD (default 5)
  - blacklist is sticky (survives restarts; manual reset only)

V10 is a background_worker so this module is consumed directly by
pole_runner_v10.py — no HTTP layer. State persists at
/var/data/v10_blacklist.json (same disk as the rest of V10's state).
"""
import os
import json
import threading
import time

STATE_DIR = os.environ.get('STATE_DIR', '/var/data')
BL_PATH = os.path.join(STATE_DIR, 'v10_blacklist.json')
THRESHOLD = int(os.environ.get('BLACKLIST_LOSS_THRESHOLD', '5'))

_lock = threading.RLock()
_state = {}  # coin -> {consec_losses, blacklisted, blacklist_ts, last_outcome_ts, last_pnl_pct, last_outcome}


def _load():
    global _state
    if os.path.exists(BL_PATH):
        try:
            with open(BL_PATH, 'r') as f:
                _state = json.load(f)
        except Exception:
            _state = {}


def _save():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with _lock:
            snap = {c: dict(s) for c, s in _state.items()}
        with open(BL_PATH, 'w') as f:
            json.dump(snap, f)
    except Exception as e:
        print(f"[v10_blacklist] save failed: {e}", flush=True)


_load()


def record_outcome(coin: str, pnl_pct: float, outcome: str = None):
    """Update consec-loss counter for `coin` based on pnl_pct.
    pnl_pct > 0  → reset to 0
    pnl_pct <= 0 → increment by 1; blacklist if >= THRESHOLD.
    `outcome` is informational (TP1_TP2 / TP1_BE / SL / EXPIRED / OPEN_EOW).
    """
    if not coin:
        return
    is_loss = pnl_pct <= 0
    with _lock:
        if coin not in _state:
            _state[coin] = {
                'consec_losses': 0,
                'blacklisted': False,
                'blacklist_ts': None,
                'last_outcome_ts': None,
                'last_pnl_pct': None,
                'last_outcome': None,
            }
        row = _state[coin]
        if is_loss:
            row['consec_losses'] = int(row.get('consec_losses', 0)) + 1
            if row['consec_losses'] >= THRESHOLD and not row.get('blacklisted'):
                row['blacklisted'] = True
                row['blacklist_ts'] = int(time.time() * 1000)
                print(f"[v10_blacklist] {coin} BLACKLISTED after "
                      f"{row['consec_losses']} consec losses", flush=True)
        else:
            row['consec_losses'] = 0
            # sticky: blacklisted stays True even after a win
        row['last_outcome_ts'] = int(time.time() * 1000)
        row['last_pnl_pct'] = pnl_pct
        row['last_outcome'] = outcome
    _save()


def is_blacklisted(coin: str) -> bool:
    with _lock:
        return bool(_state.get(coin, {}).get('blacklisted'))


def get_blacklisted() -> list:
    with _lock:
        return [c for c, s in _state.items() if s.get('blacklisted')]


def get_consec_losses() -> dict:
    with _lock:
        return {c: int(s.get('consec_losses', 0))
                for c, s in _state.items() if s.get('consec_losses', 0) > 0}


def get_state_snapshot() -> dict:
    with _lock:
        blacklisted = [c for c, s in _state.items() if s.get('blacklisted')]
        return {
            'threshold': THRESHOLD,
            'blacklisted': blacklisted,
            'blacklisted_count': len(blacklisted),
            'consec_losses': {c: int(s.get('consec_losses', 0))
                              for c, s in _state.items() if s.get('consec_losses', 0) > 0},
            'tracked_coins': len(_state),
        }


def reset_coin(coin: str):
    with _lock:
        if coin in _state:
            _state[coin]['consec_losses'] = 0
            _state[coin]['blacklisted'] = False
            _state[coin]['blacklist_ts'] = None
    _save()


def reset_all():
    with _lock:
        for coin in list(_state.keys()):
            _state[coin]['consec_losses'] = 0
            _state[coin]['blacklisted'] = False
            _state[coin]['blacklist_ts'] = None
    _save()


def filter_universe(coins) -> list:
    """Return `coins` minus all currently-blacklisted coins."""
    bl = set(get_blacklisted())
    return [c for c in coins if c not in bl]
