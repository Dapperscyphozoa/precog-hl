#!/usr/bin/env python3
"""
auto_blacklist.py — Per-coin pause logic shared by smc_v2 and brk.

Rule: 3 consecutive losses on the same coin within a 14-day window pauses the
coin. Re-enable on (a) one win in shadow-tracking mode, (b) 24h cooldown
expiry, or (c) manual unblock command.

State persisted per-engine to /var/data/<engine>_blacklist.json. Each engine
sets UZT_BLACKLIST_PATH explicitly so its loss counters are isolated. A loss on
ENGINE_A for coin X does NOT pause coin X for ENGINE_B.
File-locked for concurrent access by both engine processes.

API:
    is_paused(coin) -> bool
        Pre-fire gate. Returns True if coin is currently blocked.

    record_outcome(coin, won: bool, r_mult: float = None)
        Post-resolve. Updates the consecutive-loss counter and pauses if
        threshold reached. Pass r_mult for telemetry; not required.

    unblock(coin, reason: str = 'manual')
        Manual or programmatic re-enable.

    tick()
        Background sweep — expires 24h cooldowns, garbage-collects stale entries.

    summary() -> dict
        For dashboard / logs. {coin: {state, consec_losses, paused_at, ...}}
"""
import os
import json
import time
import fcntl
from contextlib import contextmanager


STATE_PATH = os.environ.get('UZT_BLACKLIST_PATH')
if not STATE_PATH:
    # Auto-derive from the calling script name when env not set
    import sys as _sys
    _engine = _sys.argv[0].rsplit('/', 1)[-1].replace('.py', '').replace('_service', '')
    STATE_PATH = f'/var/data/{_engine}_blacklist.json'
LOCK_PATH = STATE_PATH + '.lock'

CONSECUTIVE_LOSS_THRESHOLD = int(os.environ.get('UZT_BL_LOSS_THRESHOLD', '3'))
LOSS_WINDOW_DAYS = int(os.environ.get('UZT_BL_WINDOW_DAYS', '14'))
COOLDOWN_HOURS = int(os.environ.get('UZT_BL_COOLDOWN_HOURS', '24'))


@contextmanager
def _locked_state():
    os.makedirs(os.path.dirname(LOCK_PATH) or '.', exist_ok=True)
    f = open(LOCK_PATH, 'a+')
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        state = _load_unlocked()
        yield state
        _save_unlocked(state)
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def _load_unlocked():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_unlocked(state):
    tmp = STATE_PATH + '.tmp'
    os.makedirs(os.path.dirname(STATE_PATH) or '.', exist_ok=True)
    with open(tmp, 'w') as f:
        json.dump(state, f, separators=(',', ':'))
    os.replace(tmp, STATE_PATH)


def _now_ms():
    return int(time.time() * 1000)


def _coin_record(state, coin):
    """Get-or-create the coin's record."""
    if coin not in state:
        state[coin] = {
            'status': 'ACTIVE',           # ACTIVE | PAUSED
            'consec_losses': 0,
            'recent_outcomes': [],         # list of {t, won, r}
            'paused_at': None,
            'pause_reason': None,
            'unpause_at': None,            # 24h cooldown expiry
            'total_pauses': 0,
            'last_outcome_t': 0,
        }
    return state[coin]


def _trim_window(record):
    """Keep only outcomes within LOSS_WINDOW_DAYS."""
    cutoff = _now_ms() - LOSS_WINDOW_DAYS * 86400 * 1000
    record['recent_outcomes'] = [o for o in record['recent_outcomes'] if o['t'] >= cutoff]


# ─────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────

def is_paused(coin):
    """Pre-fire gate. Lightweight read — no lock, eventual consistency is fine."""
    state = _load_unlocked()
    rec = state.get(coin)
    if not rec:
        return False
    if rec.get('status') != 'PAUSED':
        return False
    # Check if 24h cooldown has expired (race-safe — tick() will formally clear)
    unpause_at = rec.get('unpause_at')
    if unpause_at and _now_ms() >= unpause_at:
        return False
    return True


def record_outcome(coin, won, r_mult=None):
    """Post-resolve. Update counters; pause if threshold hit."""
    with _locked_state() as state:
        rec = _coin_record(state, coin)
        now = _now_ms()
        outcome = {'t': now, 'won': bool(won)}
        if r_mult is not None:
            outcome['r'] = float(r_mult)
        rec['recent_outcomes'].append(outcome)
        rec['last_outcome_t'] = now
        _trim_window(rec)

        if won:
            rec['consec_losses'] = 0
            # Win during PAUSED state = re-enable (path A from spec)
            if rec['status'] == 'PAUSED':
                rec['status'] = 'ACTIVE'
                rec['pause_reason'] = None
                rec['unpause_at'] = None
        else:
            rec['consec_losses'] += 1
            # Threshold hit while ACTIVE → pause
            if (rec['status'] == 'ACTIVE'
                and rec['consec_losses'] >= CONSECUTIVE_LOSS_THRESHOLD):
                rec['status'] = 'PAUSED'
                rec['paused_at'] = now
                rec['pause_reason'] = f'{CONSECUTIVE_LOSS_THRESHOLD}_consec_losses'
                rec['unpause_at'] = now + COOLDOWN_HOURS * 3600 * 1000
                rec['total_pauses'] = rec.get('total_pauses', 0) + 1


def unblock(coin, reason='manual'):
    """Force re-enable. For manual override or admin commands."""
    with _locked_state() as state:
        rec = _coin_record(state, coin)
        rec['status'] = 'ACTIVE'
        rec['consec_losses'] = 0
        rec['pause_reason'] = None
        rec['unpause_at'] = None
        rec['unblocked_at'] = _now_ms()
        rec['unblocked_reason'] = reason


def tick():
    """Background sweep. Call from each engine's main loop occasionally.

    - Expires 24h cooldowns: PAUSED coins past unpause_at → ACTIVE
    - Trims recent_outcomes to window
    - Garbage-collects stale entries (no outcome in 30 days)
    """
    cutoff_stale = _now_ms() - 30 * 86400 * 1000
    with _locked_state() as state:
        for coin in list(state.keys()):
            rec = state[coin]
            # Cooldown expiry
            if (rec.get('status') == 'PAUSED'
                and rec.get('unpause_at')
                and _now_ms() >= rec['unpause_at']):
                rec['status'] = 'ACTIVE'
                rec['pause_reason'] = None
                rec['unpause_at'] = None
                rec['cooldown_expired_at'] = _now_ms()
            # Trim window
            _trim_window(rec)
            # GC stale
            if (rec.get('last_outcome_t', 0) < cutoff_stale
                and rec.get('status') == 'ACTIVE'
                and not rec.get('recent_outcomes')):
                del state[coin]


def summary():
    """Snapshot for dashboard / logs."""
    state = _load_unlocked()
    out = {}
    for coin, rec in state.items():
        out[coin] = {
            'status': rec.get('status'),
            'consec_losses': rec.get('consec_losses', 0),
            'paused_at': rec.get('paused_at'),
            'pause_reason': rec.get('pause_reason'),
            'unpause_at': rec.get('unpause_at'),
            'recent_n': len(rec.get('recent_outcomes', [])),
            'recent_wr': (sum(1 for o in rec.get('recent_outcomes', []) if o.get('won'))
                          / max(len(rec.get('recent_outcomes', [])), 1) * 100),
            'total_pauses': rec.get('total_pauses', 0),
        }
    return out


def paused_coins():
    """Quick list of currently-paused coins."""
    state = _load_unlocked()
    return [c for c, rec in state.items() if rec.get('status') == 'PAUSED']


def reset_all():
    """Wipe entire blacklist state. Operator command, use with care."""
    with _locked_state() as state:
        state.clear()


# ─────────────────────────────────────────────────────────────────────────
# CLI for ops
# ─────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('Usage: auto_blacklist.py <command> [args]')
        print('Commands:')
        print('  status              — print summary of all tracked coins')
        print('  paused              — list paused coins')
        print('  unblock <coin>      — manually re-enable a coin')
        print('  tick                — run cooldown expiry sweep')
        print('  reset               — wipe all state (DANGER)')
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == 'status':
        s = summary()
        if not s:
            print('(empty — no coins tracked)')
        for coin, info in sorted(s.items()):
            print(f'{coin:<12} {info["status"]:<8} consec_loss={info["consec_losses"]} '
                  f'recent_n={info["recent_n"]} wr={info["recent_wr"]:.0f}% '
                  f'pauses={info["total_pauses"]}')
    elif cmd == 'paused':
        for c in paused_coins():
            print(c)
    elif cmd == 'unblock':
        if len(sys.argv) < 3:
            print('Need coin arg')
            sys.exit(1)
        unblock(sys.argv[2], reason='cli')
        print(f'Unblocked {sys.argv[2]}')
    elif cmd == 'tick':
        tick()
        print('Tick complete')
    elif cmd == 'reset':
        reset_all()
        print('All blacklist state cleared')
    else:
        print(f'Unknown command: {cmd}')
        sys.exit(1)
