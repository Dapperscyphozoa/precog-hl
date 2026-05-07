#!/usr/bin/env python3
"""
unified_state.py — UZT (Unified Zone Trading) bridge between SMC v2 and BRK.

Per Lesson 2: every HTF zone has TWO possible trades — REVERSAL (zone holds) or
CONTINUATION (zone breaks). Never both. Whichever path the tape provides, fire it.

This module maintains shared per-zone state across the two services. Each service
writes its own decisions; both read the unified state before firing to:
  - prevent double-fire (one zone, one trade)
  - enable invalidation flips (smc2 setup invalidated by break → brk takes over)
  - enable reclaim flips (brk setup invalidated by reclaim → smc2 takes over)

State machine:
    IDLE
      ↓ (price tags zone)
    IN_ZONE
      ├─→ SWEPT       (wick + return) ──→ REVERSAL_ARMED ──→ REV_FILLED ──→ CONSUMED
      └─→ BROKEN      (close-thru + disp) ──→ CONTINUATION_ARMED ──→ CON_FILLED ──→ CONSUMED

Invalidation transitions:
    SWEPT / REVERSAL_ARMED  ──(close-thru + disp)──→  BROKEN     [smc2 flushed, brk armed]
    BROKEN / CONTINUATION_ARMED  ──(reclaim + disp)──→  IN_ZONE   [brk flushed, smc2 re-watches]
    Bias flip (HTF close-thru last_swing_h or last_swing_l) ──→  CONSUMED [zone retired]

State file: /var/data/uzt_zones.json (or env UZT_STATE_PATH)
Format: { "<coin>": { "<zone_key>": { ...zone state... } } }
zone_key = "{kind}:{round(top,8)}:{round(bot,8)}:{is_bull}"
"""
import os
import json
import time
import fcntl
from contextlib import contextmanager


STATE_PATH = os.environ.get('UZT_STATE_PATH', '/var/data/uzt_zones.json')
LOCK_PATH = STATE_PATH + '.lock'

VALID_STATES = {
    'IDLE', 'IN_ZONE', 'SWEPT', 'REVERSAL_ARMED', 'REV_FILLED',
    'BROKEN', 'CONTINUATION_ARMED', 'CON_FILLED', 'CONSUMED',
}


def zone_key(zone):
    return f"{zone.get('kind', 'OB')}:{round(zone['top'], 8)}:{round(zone['bot'], 8)}:{int(zone['is_bull'])}"


# ═══════════════════════════════════════════════════════════════════════════
# FILE I/O WITH LOCKING
# ═══════════════════════════════════════════════════════════════════════════

@contextmanager
def _locked_state():
    """Acquire exclusive lock + load state, yield, save on exit."""
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


def load_state():
    """Read-only snapshot."""
    return _load_unlocked()


# ═══════════════════════════════════════════════════════════════════════════
# QUERIES
# ═══════════════════════════════════════════════════════════════════════════

def get_zone_state(coin, zone, state=None):
    """Return current state for a zone, or 'IDLE' if not tracked."""
    s = state if state is not None else _load_unlocked()
    return s.get(coin, {}).get(zone_key(zone), {}).get('state', 'IDLE')


def can_fire_reversal(coin, zone, state=None):
    """SMC v2 may fire if zone is in IN_ZONE or SWEPT (about to arm)."""
    st = get_zone_state(coin, zone, state)
    return st in ('IDLE', 'IN_ZONE', 'SWEPT')


def can_fire_continuation(coin, zone, state=None):
    """BRK may fire if zone is BROKEN (about to arm continuation)."""
    st = get_zone_state(coin, zone, state)
    return st in ('IDLE', 'IN_ZONE', 'BROKEN')


def is_consumed(coin, zone, state=None):
    """True if zone has already filled or been retired."""
    st = get_zone_state(coin, zone, state)
    return st in ('REV_FILLED', 'CON_FILLED', 'CONSUMED')


# ═══════════════════════════════════════════════════════════════════════════
# TRANSITIONS
# ═══════════════════════════════════════════════════════════════════════════

def transition(coin, zone, new_state, meta=None):
    """Atomic transition. Returns (success, prior_state)."""
    if new_state not in VALID_STATES:
        raise ValueError(f"invalid state {new_state}")
    with _locked_state() as s:
        coin_d = s.setdefault(coin, {})
        k = zone_key(zone)
        prev = coin_d.get(k, {}).get('state', 'IDLE')
        if not _valid_transition(prev, new_state):
            return False, prev
        coin_d[k] = {
            'state': new_state,
            'zone': {
                'top': zone['top'],
                'bot': zone['bot'],
                'is_bull': zone['is_bull'],
                'kind': zone.get('kind', 'OB'),
            },
            't_updated': int(time.time() * 1000),
            'meta': meta or {},
        }
    return True, prev


def _valid_transition(prev, new):
    # Forward path A (reversal)
    if (prev, new) in {
        ('IDLE', 'IN_ZONE'),
        ('IN_ZONE', 'SWEPT'),
        ('SWEPT', 'REVERSAL_ARMED'),
        ('REVERSAL_ARMED', 'REV_FILLED'),
    }:
        return True
    # Forward path B (continuation)
    if (prev, new) in {
        ('IDLE', 'IN_ZONE'),
        ('IN_ZONE', 'BROKEN'),
        ('BROKEN', 'CONTINUATION_ARMED'),
        ('CONTINUATION_ARMED', 'CON_FILLED'),
    }:
        return True
    # Invalidation flip A→B (zone broken while smc2 was watching)
    if prev in ('IN_ZONE', 'SWEPT', 'REVERSAL_ARMED') and new == 'BROKEN':
        return True
    # Invalidation flip B→A (zone reclaimed while brk was watching)
    if prev in ('BROKEN', 'CONTINUATION_ARMED') and new == 'IN_ZONE':
        return True
    # Retire from any non-filled state
    if new == 'CONSUMED' and prev not in ('REV_FILLED', 'CON_FILLED'):
        return True
    return False


def mark_in_zone(coin, zone):
    return transition(coin, zone, 'IN_ZONE')


def mark_swept(coin, zone, sweep_wick=None):
    return transition(coin, zone, 'SWEPT', meta={'sweep_wick': sweep_wick})


def mark_reversal_armed(coin, zone, setup):
    return transition(coin, zone, 'REVERSAL_ARMED', meta={'setup': setup})


def mark_reversal_filled(coin, zone, fill_px=None):
    return transition(coin, zone, 'REV_FILLED', meta={'fill_px': fill_px})


def mark_broken(coin, zone, break_idx=None, displacement=None):
    return transition(coin, zone, 'BROKEN',
                      meta={'break_idx': break_idx, 'displacement': displacement})


def mark_continuation_armed(coin, zone, setup):
    return transition(coin, zone, 'CONTINUATION_ARMED', meta={'setup': setup})


def mark_continuation_filled(coin, zone, fill_px=None):
    return transition(coin, zone, 'CON_FILLED', meta={'fill_px': fill_px})


def mark_consumed(coin, zone, reason=None):
    return transition(coin, zone, 'CONSUMED', meta={'reason': reason})


def mark_reclaimed(coin, zone, reclaim_idx=None):
    return transition(coin, zone, 'IN_ZONE',
                      meta={'reclaimed_at': reclaim_idx})


# ═══════════════════════════════════════════════════════════════════════════
# DETECTION HELPERS — used by both engines to detect state changes
# ═══════════════════════════════════════════════════════════════════════════

def detect_break(zone, c15_recent, displace_atr_mult=1.2, atr=None):
    """Did the zone get closed-through with displacement in any of the recent bars?

    Returns (broken, idx, displacement_pct) or (False, None, None).
    """
    if not c15_recent:
        return False, None, None
    for i, b in enumerate(c15_recent):
        body = abs(b['c'] - b['o'])
        thresh = (atr * displace_atr_mult) if atr else (b['h'] - b['l']) * 0.6
        if body < thresh:
            continue
        if zone['is_bull']:
            # Demand broken DOWN
            if b['c'] < zone['bot']:
                disp = (zone['bot'] - b['c']) / max(zone['bot'], 1e-9)
                return True, i, disp
        else:
            # Supply broken UP
            if b['c'] > zone['top']:
                disp = (b['c'] - zone['top']) / max(zone['top'], 1e-9)
                return True, i, disp
    return False, None, None


def detect_reclaim(zone, c15_recent, displace_atr_mult=1.2, atr=None):
    """Did a broken zone get reclaimed (close back inside with displacement)?

    Caller is responsible for confirming the zone was previously BROKEN.
    """
    if not c15_recent:
        return False, None
    for i, b in enumerate(c15_recent):
        body = abs(b['c'] - b['o'])
        thresh = (atr * displace_atr_mult) if atr else (b['h'] - b['l']) * 0.6
        if body < thresh:
            continue
        # Bullish zone reclaim: close back ABOVE bot (price re-entered demand)
        if zone['is_bull'] and b['c'] > zone['bot'] and b['o'] < zone['bot']:
            return True, i
        # Bearish zone reclaim: close back BELOW top
        if (not zone['is_bull']) and b['c'] < zone['top'] and b['o'] > zone['top']:
            return True, i
    return False, None


# ═══════════════════════════════════════════════════════════════════════════
# CLEANUP
# ═══════════════════════════════════════════════════════════════════════════

def gc_consumed(max_age_seconds=7*86400):
    """Drop CONSUMED zones older than max_age. Stops the state file from growing forever."""
    now = int(time.time() * 1000)
    cutoff = now - max_age_seconds * 1000
    with _locked_state() as s:
        for coin in list(s.keys()):
            for k in list(s[coin].keys()):
                z = s[coin][k]
                if z.get('state') in ('REV_FILLED', 'CON_FILLED', 'CONSUMED') \
                   and z.get('t_updated', now) < cutoff:
                    del s[coin][k]
            if not s[coin]:
                del s[coin]


def reset_coin(coin):
    """Wipe all zone state for one coin (e.g. after killswitch)."""
    with _locked_state() as s:
        if coin in s:
            del s[coin]
