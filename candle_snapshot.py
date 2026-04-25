"""
candle_snapshot.py — Deterministic single-source candle pipeline (final form).

Architecture:
  v1: 78 per-tick REST fetches → CloudFront 429 cascade
  v2: bucketed (20/tick) — still allowed REST fallback in tick path
  v3 (FINAL): atomic full-universe build with 90% coverage gate
       NO REST escape during tick. fetch() = snapshot → LKG → empty.

Design rules:
  - Snapshot layer = single writer, atomic commit (all-or-nothing per tick)
  - Signal layer  = pure read, O(1) lookup
  - Coverage gate = ≥90% of universe must succeed before commit
  - LKG persists across failed/partial builds
  - Tick path NEVER calls REST — only background snapshot build does
  - Backwards compat: signal engine still calls fetch(coin); zero changes
    needed to signal logic

Public API:
  build_snapshot(coins, tf, fetch_fn, throttle_fn) → dict with 'ok'/'total'
  get_candles(coin, tf='15m') → list  (snapshot or LKG)
  snapshot_age_sec() → float
  snapshot_status() → dict (for /health diagnostics)

Trade-off (intentional):
  Deterministic stale data > real-time partial data.
  Worst case: stale by ~1 tick window (~60s) on a 15m timeframe = ~6% drift.
"""

import time
import threading
from collections import defaultdict


# ─── GLOBAL SNAPSHOT STATE ──────────────────────────────────────────────────
_GLOBAL_SNAPSHOT = {
    'ts': 0.0,
    'tf': defaultdict(dict),
}
_LAST_KNOWN_GOOD = defaultdict(dict)
_SNAPSHOT_LOCK = threading.Lock()

_BUILD_LOCK = {
    'active': False,
    'tf': None,
    'started_at': 0.0,
}

SNAPSHOT_TTL_SEC = 60.0
COVERAGE_THRESHOLD = 0.90

_STATS = {
    'snapshots_built': 0,
    'snapshots_committed': 0,
    'snapshots_rejected': 0,
    'snapshots_reused': 0,
    'fetch_errors': 0,
    'fallback_to_lkg': 0,
    'rest_calls_during_tick': 0,
    'last_build_duration_s': 0.0,
    'last_coverage_ratio': 0.0,
    'last_build_coins_ok': 0,
    'last_build_coins_total': 0,
}


def snapshot_age_sec():
    if _GLOBAL_SNAPSHOT['ts'] == 0:
        return float('inf')
    return time.time() - _GLOBAL_SNAPSHOT['ts']


def snapshot_status():
    return {
        'active': True,
        'mode': 'atomic_with_coverage_gate',
        'age_sec': round(snapshot_age_sec(), 1),
        'is_fresh': snapshot_age_sec() < SNAPSHOT_TTL_SEC,
        'coverage_threshold': COVERAGE_THRESHOLD,
        'tfs_loaded': list(_GLOBAL_SNAPSHOT['tf'].keys()),
        'coins_per_tf': {tf: len(coins) for tf, coins in _GLOBAL_SNAPSHOT['tf'].items()},
        'lkg_size': sum(len(c) for c in _LAST_KNOWN_GOOD.values()),
        'building': _BUILD_LOCK.get('active', False),
        **_STATS,
    }


def get_candles(coin, tf='15m'):
    coin_u = coin.upper()
    with _SNAPSHOT_LOCK:
        snap_tf = _GLOBAL_SNAPSHOT['tf'].get(tf, {})
        if coin_u in snap_tf:
            return snap_tf[coin_u]
        lkg_tf = _LAST_KNOWN_GOOD.get(tf, {})
        if coin_u in lkg_tf:
            _STATS['fallback_to_lkg'] += 1
            return lkg_tf[coin_u]
    return []


def build_snapshot(coins, tf, fetch_fn, throttle_fn=None, n_bars=100, log_fn=None):
    """Atomic full-universe snapshot build with coverage gate."""
    age = snapshot_age_sec()
    if age < SNAPSHOT_TTL_SEC:
        _STATS['snapshots_reused'] += 1
        n_loaded = len(_GLOBAL_SNAPSHOT['tf'].get(tf, {}))
        return {'ok': n_loaded, 'total': n_loaded, 'coverage': 1.0,
                'committed': False, 'reused': True}

    if _BUILD_LOCK.get('active'):
        if log_fn:
            log_fn(f"[snapshot] build skipped — another builder active for tf={_BUILD_LOCK.get('tf')}")
        return {'ok': 0, 'total': 0, 'coverage': 0.0,
                'committed': False, 'skipped': True}

    _BUILD_LOCK.update({'active': True, 'tf': tf, 'started_at': time.time()})

    try:
        coins_list = list(coins)
        n_total = len(coins_list)
        if n_total == 0:
            return {'ok': 0, 'total': 0, 'coverage': 0.0, 'committed': False}

        start = time.time()
        new_snap = {}
        ok_count = 0
        fail_count = 0

        for coin in coins_list:
            coin_u = coin.upper()
            try:
                if throttle_fn:
                    throttle_fn()
                candles = fetch_fn(coin_u, tf, n_bars)
                if candles and len(candles) > 0:
                    new_snap[coin_u] = candles
                    ok_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                _STATS['fetch_errors'] += 1
                fail_count += 1
                if log_fn:
                    log_fn(f"[snapshot] fetch err {coin_u} {tf}: {e}")

        duration = time.time() - start
        coverage = ok_count / max(1, n_total)
        _STATS['snapshots_built'] += 1
        _STATS['last_build_duration_s'] = round(duration, 2)
        _STATS['last_coverage_ratio'] = round(coverage, 3)
        _STATS['last_build_coins_ok'] = ok_count
        _STATS['last_build_coins_total'] = n_total

        committed = False
        if coverage >= COVERAGE_THRESHOLD:
            with _SNAPSHOT_LOCK:
                _GLOBAL_SNAPSHOT['tf'][tf] = new_snap
                _GLOBAL_SNAPSHOT['ts'] = time.time()
                for c, candles in new_snap.items():
                    _LAST_KNOWN_GOOD[tf][c] = candles
            _STATS['snapshots_committed'] += 1
            committed = True
            if log_fn:
                log_fn(f"[snapshot] COMMIT tf={tf} coverage={coverage:.1%} "
                       f"ok={ok_count}/{n_total} dur={duration:.1f}s")
        else:
            _STATS['snapshots_rejected'] += 1
            with _SNAPSHOT_LOCK:
                for c, candles in new_snap.items():
                    _LAST_KNOWN_GOOD[tf][c] = candles
            if log_fn:
                log_fn(f"[snapshot] REJECT tf={tf} coverage={coverage:.1%} "
                       f"< {COVERAGE_THRESHOLD:.0%} — kept previous snapshot, LKG expanded")

        return {
            'ok': ok_count, 'total': n_total,
            'coverage': coverage, 'committed': committed,
            'duration_s': duration,
        }

    finally:
        _BUILD_LOCK.update({'active': False, 'tf': None, 'started_at': 0.0})


def invalidate():
    with _SNAPSHOT_LOCK:
        _GLOBAL_SNAPSHOT['ts'] = 0.0
