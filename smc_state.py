"""SMC v1.0 — in-memory + persistent state."""
import json, os, threading

STATE_FILE = '/var/data/smc_state.json'
_lock = threading.Lock()


class State:
    armed = {}
    positions = {}
    recent_alert_ids = {}
    halt_flag = False
    halt_reason = None
    btc_trend_up = None
    btc_trend_updated_ms = 0
    funding_cache = {}
    universe = []
    last_alert_ms = 0


state = State()


def persist():
    with _lock:
        snap = {
            'armed': state.armed,
            'halt_flag': state.halt_flag,
            'halt_reason': state.halt_reason,
            'recent_alert_ids': state.recent_alert_ids,
            'btc_trend_up': state.btc_trend_up,
            'btc_trend_updated_ms': state.btc_trend_updated_ms,
        }
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(snap, f)
        os.replace(tmp, STATE_FILE)


def load():
    if not os.path.exists(STATE_FILE):
        return
    with open(STATE_FILE) as f:
        snap = json.load(f)
    state.armed = snap.get('armed', {})
    state.halt_flag = snap.get('halt_flag', False)
    state.halt_reason = snap.get('halt_reason')
    state.recent_alert_ids = snap.get('recent_alert_ids', {})
    state.btc_trend_up = snap.get('btc_trend_up')
    state.btc_trend_updated_ms = snap.get('btc_trend_updated_ms', 0)
