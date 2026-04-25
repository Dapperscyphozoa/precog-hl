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

SNAPSHOT_TTL_SEC = 180.0   # 2026-04-25: 60→180. With throttle raised to 0.7s
                           # for CloudFront friendliness, build takes ~55s for
                           # 78 coins. 60s TTL caused stale flips immediately
                           # after each commit. 180s gives 3 builds of headroom.
# Three-tier commit gate (2026-04-25):
#   v3 used hard 0.90 — too strict, caused snapshot starvation when 84-87%
#   coverage was operationally healthy. Replaced with monotonic-improvement
#   model: accept if soft target met OR not-worse-than-previous OR above hard
#   floor when stale. Bounded degradation, never regresses hard.
COVERAGE_SOFT_TARGET = 0.85    # always commit at or above this
COVERAGE_HARD_FLOOR  = 0.75    # absolute minimum (only when stale)
COVERAGE_THRESHOLD   = COVERAGE_SOFT_TARGET   # legacy alias for /health field

_PREV_COVERAGE_RATIO = {}  # {tf: float} — for monotonic-improvement rule

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
    'last_missing_coins': [],   # observability
    'commit_reason_soft':     0,  # ratio >= 0.85
    'commit_reason_monotonic':0,  # ratio >= prev
    'commit_reason_stale':    0,  # ratio >= 0.75 AND snapshot stale
}


def snapshot_age_sec():
    if _GLOBAL_SNAPSHOT['ts'] == 0:
        return float('inf')
    return time.time() - _GLOBAL_SNAPSHOT['ts']


def snapshot_status():
    return {
        'active': True,
        'mode': 'three_tier_commit_gate',
        'age_sec': round(snapshot_age_sec(), 1),
        'is_fresh': snapshot_age_sec() < SNAPSHOT_TTL_SEC,
        'coverage_threshold': COVERAGE_THRESHOLD,
        'coverage_soft_target': COVERAGE_SOFT_TARGET,
        'coverage_hard_floor': COVERAGE_HARD_FLOOR,
        'tfs_loaded': list(_GLOBAL_SNAPSHOT['tf'].keys()),
        'coins_per_tf': {tf: len(coins) for tf, coins in _GLOBAL_SNAPSHOT['tf'].items()},
        'lkg_size': sum(len(c) for c in _LAST_KNOWN_GOOD.values()),
        'building': _BUILD_LOCK.get('active', False),
        **_STATS,
    }


def get_candles(coin, tf='15m'):
    # 2026-04-25: case-preserving lookup. HL uses lowercase 'k' prefix for
    # 1000x multiplier symbols (kFLOKI, kSHIB, kPEPE). Uppercasing breaks them.
    # Try exact match first (fast), then case-insensitive scan as fallback
    # for legacy callers that may use a different case.
    with _SNAPSHOT_LOCK:
        snap_tf = _GLOBAL_SNAPSHOT['tf'].get(tf, {})
        # Exact match — fast path
        if coin in snap_tf:
            return snap_tf[coin]
        lkg_tf = _LAST_KNOWN_GOOD.get(tf, {})
        if coin in lkg_tf:
            _STATS['fallback_to_lkg'] += 1
            return lkg_tf[coin]
        # Case-insensitive scan — slow fallback for callers using different case
        coin_l = coin.lower()
        for k, v in snap_tf.items():
            if k.lower() == coin_l:
                return v
        for k, v in lkg_tf.items():
            if k.lower() == coin_l:
                _STATS['fallback_to_lkg'] += 1
                return v
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
        missing_coins = []

        for coin in coins_list:
            # 2026-04-25: preserve original case (HL needs 'kFLOKI' not 'KFLOKI')
            try:
                if throttle_fn:
                    throttle_fn()
                candles = fetch_fn(coin, tf, n_bars)
                if candles and len(candles) > 0:
                    new_snap[coin] = candles
                    ok_count += 1
                else:
                    fail_count += 1
                    missing_coins.append(coin)
            except Exception as e:
                _STATS['fetch_errors'] += 1
                fail_count += 1
                missing_coins.append(coin)
                if log_fn:
                    log_fn(f"[snapshot] fetch err {coin} {tf}: {e}")

        duration = time.time() - start
        coverage = ok_count / max(1, n_total)
        _STATS['snapshots_built'] += 1
        _STATS['last_build_duration_s'] = round(duration, 2)
        _STATS['last_coverage_ratio'] = round(coverage, 3)
        _STATS['last_build_coins_ok'] = ok_count
        _STATS['last_build_coins_total'] = n_total
        # Cap missing list for /health (avoid bloat with degenerate cases)
        _STATS['last_missing_coins'] = missing_coins[:25]

        # ─── THREE-TIER COMMIT GATE ──────────────────────────────────────
        # Rule 1: soft target — always commit at or above 0.85
        # Rule 2: monotonic — accept if not worse than previous snapshot
        # Rule 3: stale rescue — accept if above hard floor AND snapshot stale
        # Else: reject (kept previous, LKG expanded with what we got)
        prev_ratio = _PREV_COVERAGE_RATIO.get(tf, 0.0)
        is_stale = snapshot_age_sec() >= SNAPSHOT_TTL_SEC
        commit = False
        commit_reason = None

        if coverage >= COVERAGE_SOFT_TARGET:
            commit = True
            commit_reason = 'soft'
        elif coverage >= prev_ratio and prev_ratio > 0:
            # Not worse than previous → safe forward step
            commit = True
            commit_reason = 'monotonic'
        elif coverage >= COVERAGE_HARD_FLOOR and is_stale:
            # Hard floor + stale rescue → better than starvation
            commit = True
            commit_reason = 'stale'
        # else: reject

        committed = False
        if commit:
            with _SNAPSHOT_LOCK:
                _GLOBAL_SNAPSHOT['tf'][tf] = new_snap
                _GLOBAL_SNAPSHOT['ts'] = time.time()
                for c, candles in new_snap.items():
                    _LAST_KNOWN_GOOD[tf][c] = candles
            _STATS['snapshots_committed'] += 1
            _STATS[f'commit_reason_{commit_reason}'] += 1
            _PREV_COVERAGE_RATIO[tf] = coverage
            committed = True
            if log_fn:
                miss_n = len(missing_coins)
                miss_preview = (',' .join(missing_coins[:6]) +
                                (f',+{miss_n-6}' if miss_n > 6 else '')) if miss_n else ''
                log_fn(f"[snapshot] COMMIT tf={tf} coverage={coverage:.1%} "
                       f"reason={commit_reason} ok={ok_count}/{n_total} "
                       f"missing={miss_n}({miss_preview}) dur={duration:.1f}s")
        else:
            _STATS['snapshots_rejected'] += 1
            with _SNAPSHOT_LOCK:
                for c, candles in new_snap.items():
                    _LAST_KNOWN_GOOD[tf][c] = candles
            if log_fn:
                log_fn(f"[snapshot] REJECT tf={tf} coverage={coverage:.1%} "
                       f"prev={prev_ratio:.1%} hard_floor={COVERAGE_HARD_FLOOR:.0%} "
                       f"stale={is_stale} — kept previous, LKG expanded "
                       f"({len(missing_coins)} coins missing)")

        return {
            'ok': ok_count, 'total': n_total,
            'coverage': coverage, 'committed': committed,
            'commit_reason': commit_reason,
            'missing_coins': missing_coins,
            'duration_s': duration,
        }

    finally:
        _BUILD_LOCK.update({'active': False, 'tf': None, 'started_at': 0.0})


def invalidate():
    with _SNAPSHOT_LOCK:
        _GLOBAL_SNAPSHOT['ts'] = 0.0
