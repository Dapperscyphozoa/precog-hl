"""
candle_snapshot.py — Deterministic single-source candle pipeline (final form).

Architecture:
  v1: 78 per-tick REST fetches → CloudFront 429 cascade
  v2: bucketed (20/tick) — still allowed REST fallback in tick path
  v3: atomic full-universe build with 90% coverage gate, sequential
  v4 (CURRENT, 2026-05-11):
       - PARALLEL fetch with bounded concurrency (ThreadPoolExecutor)
       - PER-TF build lock (allows concurrent adjacent TF builds, e.g.
         15m + 30m + 1h built simultaneously rather than serialized)
       - All other guarantees preserved: atomic commit, 3-tier coverage
         gate, LKG fallback, no REST in tick path

Design rules:
  - Snapshot layer = single writer per TF, atomic commit (all-or-nothing)
  - Different TFs may build concurrently (independent state)
  - Same-TF concurrent rebuilds short-circuit (only one in flight per TF)
  - Coverage gate = soft 85% / monotonic / hard 75% (stale)
  - Tick path NEVER calls REST — only background snapshot build does
  - Signal layer = pure read, O(1) lookup

Public API (unchanged):
  build_snapshot(coins, tf, fetch_fn, throttle_fn=None, n_bars=100,
                 log_fn=None, max_workers=8) → dict
  get_candles(coin, tf='15m') → list
  snapshot_age_sec() → float
  snapshot_status() → dict
"""

import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict


# ─── GLOBAL SNAPSHOT STATE ──────────────────────────────────────────────────
_GLOBAL_SNAPSHOT = {
    'ts_by_tf': {},                # {tf: float} — last commit time per TF
    'tf': defaultdict(dict),       # {tf: {coin: candles}}
}
_LAST_KNOWN_GOOD = defaultdict(dict)
_SNAPSHOT_LOCK = threading.Lock()

# Per-TF build locks: {tf: threading.Lock()} created on demand.
# Allows 15m and 30m to build concurrently, but a second 15m caller waits.
_PER_TF_LOCKS = {}
_PER_TF_LOCK_GUARD = threading.Lock()

# Per-TF in-flight tracking (for /health diagnostics)
_BUILDS_ACTIVE = {}  # {tf: started_at}

# Configuration
SNAPSHOT_TTL_SEC = float(os.environ.get('SNAPSHOT_TTL_SEC', '300'))
MAX_WORKERS_DEFAULT = int(os.environ.get('SNAPSHOT_MAX_WORKERS', '8'))

COVERAGE_SOFT_TARGET = float(os.environ.get('SNAPSHOT_COVERAGE_SOFT', '0.85'))
COVERAGE_HARD_FLOOR  = float(os.environ.get('SNAPSHOT_COVERAGE_HARD', '0.75'))
COVERAGE_THRESHOLD   = COVERAGE_SOFT_TARGET   # legacy alias for /health

_PREV_COVERAGE_RATIO = {}  # {tf: float}

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
    'last_missing_coins': [],
    'commit_reason_soft':     0,
    'commit_reason_monotonic':0,
    'commit_reason_stale':    0,
    'concurrent_builds_skipped': 0,
}


def _get_tf_lock(tf):
    with _PER_TF_LOCK_GUARD:
        lk = _PER_TF_LOCKS.get(tf)
        if lk is None:
            lk = threading.Lock()
            _PER_TF_LOCKS[tf] = lk
        return lk


def snapshot_age_sec(tf=None):
    """Age of the most recent commit. If tf given, age for that TF only."""
    if tf is not None:
        ts = _GLOBAL_SNAPSHOT['ts_by_tf'].get(tf, 0.0)
        return float('inf') if ts == 0 else time.time() - ts
    # No TF: oldest age across all loaded TFs
    if not _GLOBAL_SNAPSHOT['ts_by_tf']:
        return float('inf')
    return time.time() - min(_GLOBAL_SNAPSHOT['ts_by_tf'].values())


def snapshot_status():
    return {
        'active': True,
        'mode': 'parallel_per_tf_v4',
        'age_sec': round(snapshot_age_sec(), 1),
        'age_per_tf': {tf: round(time.time() - ts, 1)
                       for tf, ts in _GLOBAL_SNAPSHOT['ts_by_tf'].items()},
        'is_fresh': snapshot_age_sec() < SNAPSHOT_TTL_SEC,
        'coverage_threshold': COVERAGE_THRESHOLD,
        'coverage_soft_target': COVERAGE_SOFT_TARGET,
        'coverage_hard_floor': COVERAGE_HARD_FLOOR,
        'tfs_loaded': list(_GLOBAL_SNAPSHOT['tf'].keys()),
        'coins_per_tf': {tf: len(coins) for tf, coins in _GLOBAL_SNAPSHOT['tf'].items()},
        'lkg_size': sum(len(c) for c in _LAST_KNOWN_GOOD.values()),
        'building': dict(_BUILDS_ACTIVE),
        'max_workers_default': MAX_WORKERS_DEFAULT,
        **_STATS,
    }


def get_candles(coin, tf='15m'):
    """Case-preserving lookup; falls back to LKG; case-insensitive scan last."""
    with _SNAPSHOT_LOCK:
        snap_tf = _GLOBAL_SNAPSHOT['tf'].get(tf, {})
        if coin in snap_tf:
            return snap_tf[coin]
        lkg_tf = _LAST_KNOWN_GOOD.get(tf, {})
        if coin in lkg_tf:
            _STATS['fallback_to_lkg'] += 1
            return lkg_tf[coin]
        coin_l = coin.lower()
        for k, v in snap_tf.items():
            if k.lower() == coin_l:
                return v
        for k, v in lkg_tf.items():
            if k.lower() == coin_l:
                _STATS['fallback_to_lkg'] += 1
                return v
    return []


def build_snapshot(coins, tf, fetch_fn, throttle_fn=None,
                   n_bars=100, log_fn=None, max_workers=None):
    """Parallel atomic full-universe snapshot build with coverage gate.

    Concurrency model:
      - Workers: ThreadPoolExecutor(max_workers). I/O-bound: fine on GIL.
      - throttle_fn: still called once per coin BEFORE submitting the fetch.
        With concurrent workers, the throttle becomes a rate-limiter rather
        than a strict serial gap — calls are released at the throttle's
        pace, workers pick them up and run fetches in parallel.
      - Per-TF lock prevents double-build of the same TF; different TFs
        can build concurrently.

    Performance:
      - 80 coins, 0.1s OKX throttle, 8 workers, ~150ms per fetch:
        ≈ max(80 × 0.1s = 8s,  ceil(80/8) × 0.15s = 1.5s) = ~10s.
      - vs sequential at 2s gap = 160s. ~16× speedup.
    """
    if max_workers is None:
        max_workers = MAX_WORKERS_DEFAULT

    age = snapshot_age_sec(tf)
    if age < SNAPSHOT_TTL_SEC:
        _STATS['snapshots_reused'] += 1
        n_loaded = len(_GLOBAL_SNAPSHOT['tf'].get(tf, {}))
        return {'ok': n_loaded, 'total': n_loaded, 'coverage': 1.0,
                'committed': False, 'reused': True}

    tf_lock = _get_tf_lock(tf)
    if not tf_lock.acquire(blocking=False):
        # Another worker is already building this exact TF; don't pile on
        _STATS['concurrent_builds_skipped'] += 1
        if log_fn:
            log_fn(f"[snapshot] build skipped — {tf} already in flight")
        return {'ok': 0, 'total': 0, 'coverage': 0.0,
                'committed': False, 'skipped': True}

    _BUILDS_ACTIVE[tf] = time.time()

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

        def _fetch_one(coin):
            try:
                if throttle_fn is not None:
                    throttle_fn()
                candles = fetch_fn(coin, tf, n_bars)
                return (coin, candles, None)
            except Exception as e:
                return (coin, None, e)

        # Bounded concurrency. workers ≤ max_workers; throttle controls
        # the actual fetch rate.
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, n_total)),
                                thread_name_prefix=f'snap-{tf}') as ex:
            futures = [ex.submit(_fetch_one, c) for c in coins_list]
            for fut in as_completed(futures):
                coin, candles, err = fut.result()
                if err is not None:
                    _STATS['fetch_errors'] += 1
                    fail_count += 1
                    missing_coins.append(coin)
                    if log_fn:
                        log_fn(f"[snapshot] fetch err {coin} {tf}: {err}")
                elif candles and len(candles) > 0:
                    new_snap[coin] = candles
                    ok_count += 1
                else:
                    fail_count += 1
                    missing_coins.append(coin)

        duration = time.time() - start
        coverage = ok_count / max(1, n_total)
        _STATS['snapshots_built'] += 1
        _STATS['last_build_duration_s'] = round(duration, 2)
        _STATS['last_coverage_ratio'] = round(coverage, 3)
        _STATS['last_build_coins_ok'] = ok_count
        _STATS['last_build_coins_total'] = n_total
        _STATS['last_missing_coins'] = missing_coins[:25]

        # ─── THREE-TIER COMMIT GATE ──────────────────────────────────────
        prev_ratio = _PREV_COVERAGE_RATIO.get(tf, 0.0)
        is_stale = snapshot_age_sec(tf) >= SNAPSHOT_TTL_SEC
        commit = False
        commit_reason = None

        if coverage >= COVERAGE_SOFT_TARGET:
            commit = True
            commit_reason = 'soft'
        elif coverage >= prev_ratio and prev_ratio > 0:
            commit = True
            commit_reason = 'monotonic'
        elif coverage >= COVERAGE_HARD_FLOOR and is_stale:
            commit = True
            commit_reason = 'stale'

        committed = False
        if commit:
            with _SNAPSHOT_LOCK:
                _GLOBAL_SNAPSHOT['tf'][tf] = new_snap
                _GLOBAL_SNAPSHOT['ts_by_tf'][tf] = time.time()
                for c, candles in new_snap.items():
                    _LAST_KNOWN_GOOD[tf][c] = candles
            _STATS['snapshots_committed'] += 1
            _STATS[f'commit_reason_{commit_reason}'] += 1
            _PREV_COVERAGE_RATIO[tf] = coverage
            committed = True
            if log_fn:
                miss_n = len(missing_coins)
                miss_preview = (','.join(missing_coins[:6]) +
                                (f',+{miss_n-6}' if miss_n > 6 else '')) if miss_n else ''
                log_fn(f"[snapshot] COMMIT tf={tf} coverage={coverage:.1%} "
                       f"reason={commit_reason} ok={ok_count}/{n_total} "
                       f"missing={miss_n}({miss_preview}) dur={duration:.1f}s "
                       f"workers={max_workers}")
        else:
            _STATS['snapshots_rejected'] += 1
            with _SNAPSHOT_LOCK:
                for c, candles in new_snap.items():
                    _LAST_KNOWN_GOOD[tf][c] = candles
            if log_fn:
                log_fn(f"[snapshot] REJECT tf={tf} coverage={coverage:.1%} "
                       f"prev={prev_ratio:.1%} hard={COVERAGE_HARD_FLOOR:.0%} "
                       f"stale={is_stale} — kept previous, LKG expanded "
                       f"({len(missing_coins)} missing)")

        return {
            'ok': ok_count, 'total': n_total,
            'coverage': coverage, 'committed': committed,
            'commit_reason': commit_reason,
            'missing_coins': missing_coins,
            'duration_s': duration,
        }

    finally:
        _BUILDS_ACTIVE.pop(tf, None)
        tf_lock.release()


def invalidate(tf=None):
    """Force next build to actually rebuild rather than reuse cache."""
    with _SNAPSHOT_LOCK:
        if tf is None:
            _GLOBAL_SNAPSHOT['ts_by_tf'].clear()
        else:
            _GLOBAL_SNAPSHOT['ts_by_tf'].pop(tf, None)
