"""Wall-bounce retest entry engine. Third signal source.
Entry: verified wall holds → price pulls away ≥0.3% → returns within ±0.1% → fire aligned with V3.
SL: tight behind wall (wall fails = exit fast). TP: opposite-side wall or 1.5x wall-distance.

Env-tunable for chop regime: in low-vol chop, the default 0.3% pullback in 5min
rarely triggers. Operators can relax via WALL_BNC_PULL_MIN (default 0.003) and
WALL_BNC_PULL_LOOKBACK_SEC (default 300). For chop: 0.0015 + 600 fires more.
"""
import os, time, threading
from collections import Counter
import orderbook_ws

# Env kill-switch: default OFF. Operator flips WALL_BNC_ENABLED=1 to activate.
# Re-enabled 2026-05-11 after snapshot/cache hardening; was hard-disabled
# 2026-04-28 (-$1.16 / 4 trades — latency+notional, not concept).
RETEST_PROXIMITY = float(os.environ.get('WALL_BNC_RETEST_PROXIMITY', '0.001'))   # within 0.1% of wall
PULL_MIN = float(os.environ.get('WALL_BNC_PULL_MIN', '0.003'))                   # ≥0.3% pull-away
PULL_LOOKBACK_SEC = int(os.environ.get('WALL_BNC_PULL_LOOKBACK_SEC', '300'))     # 5min default
MIN_WALL_USD = float(os.environ.get('WALL_BNC_MIN_WALL_USD', '750000'))
COOLDOWN_SEC = int(os.environ.get('WALL_BNC_COOLDOWN_SEC', '600'))               # 10min/coin

_LAST_FIRED = {}           # coin -> ts
_PULL_HISTORY = {}         # coin -> [(ts, px, wall_price, wall_side)]
_LOCK = threading.Lock()

_STATS = {
    'check_calls':       0,
    'fires':             0,
    'errors':            0,
    'disabled_skips':    0,
}
# Gate-fail counters. Operator reads these to see why fires don't happen.
_GATE_FAILS = Counter()           # reason -> count (aggregate)
_GATE_FAILS_BY_COIN = {}          # coin -> Counter(reason -> count)
_LAST_GATE_LOG_T = [0.0]
_GATE_LOG_INTERVAL = int(os.environ.get('WALL_BNC_GATE_LOG_INTERVAL_SEC', '120'))


def is_enabled() -> bool:
    return os.environ.get('WALL_BNC_ENABLED', '0') == '1'


def _record_fail(coin: str, reason: str):
    with _LOCK:
        _GATE_FAILS[reason] += 1
        _GATE_FAILS_BY_COIN.setdefault(coin, Counter())[reason] += 1


def _maybe_log_gates():
    """Emit a one-line summary every WALL_BNC_GATE_LOG_INTERVAL_SEC."""
    now = time.time()
    if now - _LAST_GATE_LOG_T[0] < _GATE_LOG_INTERVAL:
        return
    _LAST_GATE_LOG_T[0] = now
    with _LOCK:
        if not _GATE_FAILS:
            return
        total = sum(_GATE_FAILS.values())
        top = sorted(_GATE_FAILS.items(), key=lambda x: -x[1])[:6]
        summary = ' '.join(f'{r}={n}({100*n/total:.0f}%)' for r, n in top)
    print(f"[wall_bounce] gates: checks={_STATS['check_calls']} fires={_STATS['fires']} fails={total}  {summary}", flush=True)

import sys as _sys
def _log_err(msg):
    print(f"[wall_bounce ERR] {msg}", file=_sys.stderr, flush=True)

def status():
    out = dict(_STATS)
    n = max(1, out['check_calls'])
    out['success_rate_pct'] = round((1 - out['errors']/n) * 100, 2)
    out['enabled'] = is_enabled()
    with _LOCK:
        out['gate_fails'] = dict(_GATE_FAILS)
        # Top 5 coins by gate-fail count (most often blocked)
        top_blocked = sorted(_GATE_FAILS_BY_COIN.items(),
                             key=lambda kv: -sum(kv[1].values()))[:5]
        out['top_blocked_coins'] = {c: dict(reasons) for c, reasons in top_blocked}
    return out

def _record_price(coin, px):
    """Track price for pullback detection."""
    with _LOCK:
        h = _PULL_HISTORY.setdefault(coin, [])
        h.append((time.time(), px))
        # Prune
        cutoff = time.time() - PULL_LOOKBACK_SEC - 60
        _PULL_HISTORY[coin] = [x for x in h if x[0] > cutoff]

def check(coin, current_px, v3_direction):
    """Returns ('BUY'|'SELL', wall_dict) if retest entry fires. v3_direction: +1 up, -1 dn, 0 neutral.
    Kill-switch: WALL_BNC_ENABLED env must be '1'. Default off."""
    if not is_enabled():
        _STATS['disabled_skips'] += 1
        return None, None
    if not current_px or current_px <= 0:
        _record_fail(coin, 'invalid_px')
        return None, None
    _STATS['check_calls'] += 1
    _record_price(coin, current_px)
    _maybe_log_gates()
    # Cooldown
    if time.time() - _LAST_FIRED.get(coin, 0) < COOLDOWN_SEC:
        _record_fail(coin, 'cooldown')
        return None, None
    # Check both sides for eligible walls
    side_outcomes = []
    for wall_side, trade_side, dir_req in [('bid', 'BUY', 1), ('ask', 'SELL', -1)]:
        try:
            wall = orderbook_ws.get_nearest_wall(coin, wall_side)
        except Exception as e:
            _STATS['errors'] += 1
            _log_err(f"get_nearest_wall({coin},{wall_side}): {type(e).__name__}: {e}")
            _record_fail(coin, f'wall_lookup_err_{wall_side}')
            side_outcomes.append('err')
            continue
        if not wall:
            _record_fail(coin, f'no_wall_{wall_side}')
            side_outcomes.append('no_wall')
            continue
        if wall.get('usd', 0) < MIN_WALL_USD:
            _record_fail(coin, f'wall_too_small_{wall_side}')
            side_outcomes.append('too_small')
            continue
        # V3 must align (skip counter-trend retests)
        if v3_direction != 0 and v3_direction != dir_req:
            _record_fail(coin, f'v3_misaligned_{wall_side}')
            side_outcomes.append('v3_misalign')
            continue
        wp = wall['price']
        # Retest proximity
        dist = abs(current_px - wp) / current_px
        if dist > RETEST_PROXIMITY:
            _record_fail(coin, f'not_in_retest_zone_{wall_side}')
            side_outcomes.append('out_of_zone')
            continue
        # Did price pull away ≥PULL_MIN from this wall within lookback?
        with _LOCK:
            hist = list(_PULL_HISTORY.get(coin, []))
        now = time.time()
        pulled = False
        for ts, hpx in hist:
            if now - ts > PULL_LOOKBACK_SEC:
                continue
            away = abs(hpx - wp) / wp
            if away >= PULL_MIN:
                # Direction check: pull must have been AWAY from wall in the right direction
                if wall_side == 'bid' and hpx > wp * (1 + PULL_MIN):
                    pulled = True; break
                if wall_side == 'ask' and hpx < wp * (1 - PULL_MIN):
                    pulled = True; break
        if not pulled:
            _record_fail(coin, f'no_pullback_{wall_side}')
            side_outcomes.append('no_pull')
            continue
        _LAST_FIRED[coin] = now
        _STATS['fires'] += 1
        print(f"[wall_bounce] FIRE {coin} {trade_side} @ wall ${wall['usd']/1000:.0f}k p={wp} dist={dist*100:.3f}%", flush=True)
        return trade_side, wall
    # No side fired
    if all(o == 'no_wall' for o in side_outcomes):
        _record_fail(coin, 'no_eligible_walls_either_side')
    return None, None

def wall_broken(coin, side, original_wall_price, current_px):
    """Check if wall that supported/resisted our trade has been eaten through.
    side: 'BUY' (we held above bid wall) or 'SELL' (we held below ask wall).
    Returns True if wall invalidated → exit immediately.
    """
    if not current_px or not original_wall_price: return False
    try:
        wall = orderbook_ws.get_nearest_wall(coin, 'bid' if side == 'BUY' else 'ask')
    except Exception as e:
        _STATS['errors'] += 1
        _log_err(f"wall_broken get_nearest_wall({coin}): {type(e).__name__}: {e}")
        return False
    # If wall gone from tracked verified list OR moved >0.5% from original = broken
    if not wall: return True
    moved = abs(wall['price'] - original_wall_price) / original_wall_price
    if moved > 0.005: return True
    # Price has crossed through original wall level
    if side == 'BUY' and current_px < original_wall_price * 0.998: return True
    if side == 'SELL' and current_px > original_wall_price * 1.002: return True
    return False
