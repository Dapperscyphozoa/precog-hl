"""Promotion Engine — controlled leak of EDGE_REJECTED trades into live execution.

Randomly promotes LEAK_RATE% of `not_elite_whitelisted` rejections to live
trades with reduced size. Strict caps prevent runaway experimental exposure.

DESIGN PRINCIPLES:
- Not all edge rejections are candidates: ONLY `not_elite_whitelisted`
- All OTHER filters still apply at entry time (regime, funding, BTC-corr,
  conf, HTF alignment). Whitelist is the only bypass.
- Every experimental trade is tagged so it tracks separately from baseline
- Kill switch: -3R cumulative on experimental bucket → 24h pause
- Per-coin 6h cooldown to prevent a single coin dominating the experiment
- Equity floor: if account < EQUITY_FLOOR, kill switch

USAGE:
    import promotion_engine as pe

    # When signal is blocked by `not_elite_whitelisted`:
    decision = pe.maybe_promote(coin, side, conf_score, account_equity)
    # decision = {'promote': bool, 'size_mult': float, 'tag': {...}, 'reason': str}
    if decision['promote']:
        # Execute trade with decision['size_mult'] applied to normal size
        # Tag position metadata with decision['tag']
        pe.record_promotion(coin, side, tag=decision['tag'])

    # After trade closes:
    pe.record_outcome(coin, pnl_usd, pnl_r)  # updates experimental bucket

    # For dashboard/experiment endpoint:
    pe.status()

STORAGE:
    /var/data/experiment.jsonl — per-promotion log (append-only)
    in-memory: bucket state, caps tracking, recent promotions
"""
import os
import json
import time
import random
import threading
from collections import defaultdict

# ─── CONFIGURATION (env-tunable) ─────────────────────────────────
LEAK_RATE = float(os.environ.get('EXPERIMENT_LEAK_RATE', '0.20'))
SIZE_MULT = float(os.environ.get('EXPERIMENT_SIZE_MULT', '0.50'))
KILL_R_THRESHOLD = float(os.environ.get('EXPERIMENT_KILL_R', '-3.0'))
MAX_CONCURRENT = int(os.environ.get('EXPERIMENT_MAX_CONCURRENT', '1'))
PER_COIN_COOLDOWN_SEC = int(os.environ.get('EXPERIMENT_COIN_COOLDOWN', str(6*3600)))
KILL_PAUSE_SEC = int(os.environ.get('EXPERIMENT_KILL_PAUSE', str(24*3600)))
EQUITY_FLOOR = float(os.environ.get('EXPERIMENT_EQUITY_FLOOR', '550.0'))
LOG_PATH = os.environ.get('EXPERIMENT_LOG_PATH', '/var/data/experiment.jsonl')
ENABLED = os.environ.get('EXPERIMENT_ENABLED', '0') == '1'  # default OFF

# ─── STATE ──────────────────────────────────────────────────────
_LOCK = threading.Lock()
_STATE = {
    'bucket_r': 0.0,           # cumulative R on experimental bucket (realized)
    'bucket_pnl_usd': 0.0,     # cumulative USD
    'kill_active': False,
    'kill_until': 0,           # unix ts when pause ends
    'kill_reason': None,
    'concurrent': 0,           # active experimental positions
    'total_promoted': 0,
    'total_resolved': 0,
    'wins': 0,
    'losses': 0,
}
_PER_COIN_LAST_PROMOTION = {}  # coin -> ts
_ACTIVE = {}  # coin -> {side, promoted_ts, tag}
_RESOLVED = []  # historical trade outcomes


def _append_log(rec):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception as e:
        print(f'[promotion_engine] log err: {e}', flush=True)


def is_enabled():
    """Master switch. Returns False if disabled globally OR in kill pause."""
    if not ENABLED:
        return False, 'disabled_by_config'
    now = time.time()
    with _LOCK:
        if _STATE['kill_active']:
            if now < _STATE['kill_until']:
                return False, f"kill_active (until {int(_STATE['kill_until'])}, reason: {_STATE['kill_reason']})"
            # Kill pause expired, reset state
            _STATE['kill_active'] = False
            _STATE['bucket_r'] = 0.0  # reset R bucket for fresh window
            _STATE['bucket_pnl_usd'] = 0.0
            _STATE['kill_reason'] = None
    return True, None


def _caps_ok(coin, account_equity):
    """Check all non-rate caps. Returns (ok: bool, reason: str or None)."""
    now = time.time()
    # Equity floor
    if account_equity is not None and account_equity < EQUITY_FLOOR:
        return False, f'equity_below_floor (${account_equity:.2f} < ${EQUITY_FLOOR})'

    with _LOCK:
        # Concurrent
        if _STATE['concurrent'] >= MAX_CONCURRENT:
            return False, f'max_concurrent ({_STATE["concurrent"]}/{MAX_CONCURRENT})'

        # Per-coin cooldown
        last = _PER_COIN_LAST_PROMOTION.get(coin, 0)
        if now - last < PER_COIN_COOLDOWN_SEC:
            remaining = int(PER_COIN_COOLDOWN_SEC - (now - last))
            return False, f'per_coin_cooldown (coin={coin}, {remaining}s left)'

    return True, None


def maybe_promote(coin, side, conf_score=None, account_equity=None):
    """Called when a signal is about to be rejected with `not_elite_whitelisted`.

    Returns dict:
      - promote: bool
      - size_mult: float (SIZE_MULT if promoting, else None)
      - tag: dict to attach to position metadata
      - reason: why not promoted (if promote=False)

    Caller is responsible for actually executing the trade and calling
    record_promotion() after the order fills.
    """
    ok, disable_reason = is_enabled()
    if not ok:
        return {'promote': False, 'size_mult': None, 'tag': None, 'reason': disable_reason}

    caps_ok, cap_reason = _caps_ok(coin, account_equity)
    if not caps_ok:
        return {'promote': False, 'size_mult': None, 'tag': None, 'reason': cap_reason}

    # Random leak decision (after all gates passed)
    roll = random.random()
    if roll >= LEAK_RATE:
        return {'promote': False, 'size_mult': None, 'tag': None, 'reason': f'leak_miss (roll={roll:.3f} >= {LEAK_RATE})'}

    # Promote
    tag = {
        'source': 'EDGE_REJECTED_not_elite_whitelist',
        'group': 'EXPERIMENT',
        'baseline_group': 'CONTROL',
        'promotion_reason': 'random_leak_test',
        'risk_bucket': 'experimental',
        'leak_rate': LEAK_RATE,
        'size_mult': SIZE_MULT,
        'conf_at_promotion': conf_score,
        'timestamp': time.time(),
    }
    return {
        'promote': True,
        'size_mult': SIZE_MULT,
        'tag': tag,
        'reason': f'promoted (roll={roll:.3f} < {LEAK_RATE})',
    }


def record_promotion(coin, side, tag):
    """Called AFTER experimental trade has been successfully placed."""
    now = time.time()
    with _LOCK:
        _PER_COIN_LAST_PROMOTION[coin] = now
        _ACTIVE[coin] = {'side': side, 'promoted_ts': now, 'tag': tag}
        _STATE['concurrent'] += 1
        _STATE['total_promoted'] += 1
    _append_log({
        'event': 'promotion',
        'coin': coin,
        'side': side,
        'tag': tag,
        'ts': now,
    })
    print(f'[EXPERIMENT] promoted {coin} {side} · leak_rate={LEAK_RATE} · concurrent={_STATE["concurrent"]}', flush=True)


def record_outcome(coin, pnl_usd, pnl_r=None, outcome='close'):
    """Called when an experimental position closes.

    pnl_r: signed R-multiple (pnl_pct / sl_pct). If not provided, bucket tracks
    only USD — less precise but still functional.
    """
    now = time.time()
    with _LOCK:
        active = _ACTIVE.pop(coin, None)
        if active is None:
            # Wasn't tracked as experimental — skip
            return
        _STATE['concurrent'] = max(0, _STATE['concurrent'] - 1)
        _STATE['total_resolved'] += 1
        _STATE['bucket_pnl_usd'] += pnl_usd
        if pnl_r is not None:
            _STATE['bucket_r'] += pnl_r
            if pnl_r > 0: _STATE['wins'] += 1
            elif pnl_r < 0: _STATE['losses'] += 1
        else:
            if pnl_usd > 0: _STATE['wins'] += 1
            elif pnl_usd < 0: _STATE['losses'] += 1

        # Kill switch check
        if _STATE['bucket_r'] <= KILL_R_THRESHOLD and not _STATE['kill_active']:
            _STATE['kill_active'] = True
            _STATE['kill_until'] = now + KILL_PAUSE_SEC
            _STATE['kill_reason'] = f'bucket_r={_STATE["bucket_r"]:.2f}R hit kill threshold {KILL_R_THRESHOLD}R'
            print(f'[EXPERIMENT] ❌ KILL SWITCH ACTIVATED: {_STATE["kill_reason"]}', flush=True)
            _append_log({
                'event': 'kill_switch',
                'bucket_r': _STATE['bucket_r'],
                'bucket_pnl_usd': _STATE['bucket_pnl_usd'],
                'n_resolved': _STATE['total_resolved'],
                'ts': now,
            })

    rec = {
        'event': 'outcome',
        'coin': coin,
        'pnl_usd': pnl_usd,
        'pnl_r': pnl_r,
        'outcome': outcome,
        'held_sec': int(now - active['promoted_ts']) if active else None,
        'side': active['side'] if active else None,
        'tag': active['tag'] if active else None,
        'ts': now,
    }
    _RESOLVED.append(rec)
    if len(_RESOLVED) > 5000:
        _RESOLVED[:] = _RESOLVED[-5000:]
    _append_log(rec)
    print(f'[EXPERIMENT] outcome {coin} pnl=${pnl_usd:+.2f} r={pnl_r} | bucket=${_STATE["bucket_pnl_usd"]:+.2f} ({_STATE["bucket_r"]:+.2f}R)', flush=True)


def is_experimental_coin(coin):
    """Check if a given coin currently has an active experimental position."""
    with _LOCK:
        return coin in _ACTIVE


def status():
    """Full status payload for dashboard / /experiment endpoint."""
    with _LOCK:
        state_copy = dict(_STATE)
        active_copy = {c: dict(d) for c, d in _ACTIVE.items()}
        recent = list(_RESOLVED[-50:])

    n = state_copy['total_resolved']
    wr = (state_copy['wins'] / (state_copy['wins'] + state_copy['losses'])) if (state_copy['wins'] + state_copy['losses']) > 0 else None
    avg_r = (state_copy['bucket_r'] / n) if n > 0 else None

    return {
        'enabled': ENABLED,
        'leak_rate': LEAK_RATE,
        'size_mult': SIZE_MULT,
        'kill_r_threshold': KILL_R_THRESHOLD,
        'kill_active': state_copy['kill_active'],
        'kill_until': state_copy['kill_until'],
        'kill_reason': state_copy['kill_reason'],
        'kill_paused_remaining_sec': max(0, int(state_copy['kill_until'] - time.time())) if state_copy['kill_active'] else 0,
        'total_promoted': state_copy['total_promoted'],
        'total_resolved': state_copy['total_resolved'],
        'wins': state_copy['wins'],
        'losses': state_copy['losses'],
        'win_rate': round(wr, 3) if wr is not None else None,
        'bucket_r': round(state_copy['bucket_r'], 3),
        'bucket_pnl_usd': round(state_copy['bucket_pnl_usd'], 2),
        'avg_r_per_trade': round(avg_r, 3) if avg_r is not None else None,
        'concurrent': state_copy['concurrent'],
        'max_concurrent': MAX_CONCURRENT,
        'per_coin_cooldown_sec': PER_COIN_COOLDOWN_SEC,
        'equity_floor': EQUITY_FLOOR,
        'active_positions': active_copy,
        'recent_resolved': recent,
    }


def reset_kill_switch():
    """Manual override to clear kill pause. Should be used carefully."""
    with _LOCK:
        _STATE['kill_active'] = False
        _STATE['kill_until'] = 0
        _STATE['bucket_r'] = 0.0
        _STATE['bucket_pnl_usd'] = 0.0
        _STATE['kill_reason'] = None
    print('[EXPERIMENT] kill switch manually reset', flush=True)
