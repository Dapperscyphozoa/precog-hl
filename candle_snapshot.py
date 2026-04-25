"""
candle_snapshot.py — Single-snapshot-per-tick candle pipeline.

Architecture:
  Before: 78 coins × per-coin REST fetch per tick → CloudFront 429 cascade
  After:  1 controlled snapshot build per cycle → reused across all signals
  v2 (2026-04-25): Bucketed builder — spread N coins across K ticks to
       smooth burst pressure. Snapshot stays usable from LKG between buckets.

Design rules:
  - Snapshot layer = single writer (one builder thread per cycle)
  - Signal layer  = pure read (zero network calls in hot path)
  - Failure isolation: per-coin fetch errors fall back to last-known-good
  - Burst shaping: only N/K coins fetched per tick (default K=4 buckets)
  - Backwards compat: signal engine still calls fetch(coin); snapshot is
    populated transparently before the tick scan loop.

Public API:
  build_snapshot(coins, tf, fetch_fn, throttle_fn) → dict
  get_candles(coin, tf='15m') → list  (snapshot or last-known-good)
  snapshot_age_sec() → float
  snapshot_status() → dict (for /health diagnostics)

Tick wiring:
  Right before the per-coin signal scan, call:
    build_snapshot(COINS, '15m', _raw_hl_fetch, _hl_throttle)
  Then process(coin) → calls fetch(coin) → reads from snapshot.
"""

import time
import threading
from collections import defaultdict


# ─── GLOBAL SNAPSHOT STATE ──────────────────────────────────────────────────
_GLOBAL_SNAPSHOT = {
    'ts': 0.0,                 # build completion timestamp
    'tf': defaultdict(dict),   # {tf: {coin: candles_list}}
    'building': False,         # mutex flag
}
_LAST_KNOWN_GOOD = defaultdict(dict)  # {tf: {coin: candles_list}} survives staleness
_SNAPSHOT_LOCK = threading.Lock()

# Bucket scheduler state
_BUCKET_INDEX = {'15m': 0, '1h': 0, '4h': 0}  # rotate which bucket builds this tick

# Default config (overridable via env)
SNAPSHOT_TTL_SEC = 60.0   # one tick cycle = ~60s on this system
SNAPSHOT_BUILD_THROTTLE = 0.20  # 200ms between fetches inside snapshot build
NUM_BUCKETS = 4           # 78 coins / 4 = ~20 coins per tick (smooths burst)

# Stats for diagnostics
_STATS = {
    'snapshots_built': 0,
    'snapshots_reused': 0,
    'fetch_errors': 0,
    'fallback_to_lkg': 0,  # last-known-good
    'last_build_duration_s': 0.0,
    'last_build_coins_ok': 0,
    'last_build_coins_failed': 0,
    'last_bucket_index': 0,
    'bucket_builds': 0,
}


def snapshot_age_sec():
    """Seconds since last successful snapshot build."""
    if _GLOBAL_SNAPSHOT['ts'] == 0:
        return float('inf')
    return time.time() - _GLOBAL_SNAPSHOT['ts']


def snapshot_status():
    """Diagnostic snapshot status for /health endpoint."""
    return {
        'age_sec': round(snapshot_age_sec(), 1),
        'is_fresh': snapshot_age_sec() < SNAPSHOT_TTL_SEC,
        'num_buckets': NUM_BUCKETS,
        'tfs_loaded': list(_GLOBAL_SNAPSHOT['tf'].keys()),
        'coins_per_tf': {tf: len(coins) for tf, coins in _GLOBAL_SNAPSHOT['tf'].items()},
        'lkg_size': sum(len(c) for c in _LAST_KNOWN_GOOD.values()),
        **_STATS,
    }


def get_candles(coin, tf='15m'):
    """Read candles from snapshot. Falls back to last-known-good on miss.
    Returns [] only if no data has ever been fetched for this coin+tf.

    Pure read — no network, no blocking, no retry. This is the API the
    signal layer should use.
    """
    coin_u = coin.upper()
    # Try fresh snapshot first
    with _SNAPSHOT_LOCK:
        snap_tf = _GLOBAL_SNAPSHOT['tf'].get(tf, {})
        if coin_u in snap_tf:
            return snap_tf[coin_u]
        # Fall back to last-known-good (degraded but usable)
        lkg_tf = _LAST_KNOWN_GOOD.get(tf, {})
        if coin_u in lkg_tf:
            _STATS['fallback_to_lkg'] += 1
            return lkg_tf[coin_u]
    return []


def build_snapshot(coins, tf, fetch_fn, throttle_fn=None, n_bars=100, log_fn=None):
    """Bucketed snapshot builder — spreads N coins across K ticks.

    On each call, fetches only one bucket (~N/K coins) instead of all N.
    This caps burst rate at HL/CloudFront-friendly levels even with large
    universes. Snapshot stays usable from LKG between bucket refreshes.

    Args:
      coins: iterable of coin symbols
      tf: timeframe string ('15m', '1h', '4h')
      fetch_fn: callable(coin, tf, n_bars) -> candles_list
      throttle_fn: optional callable() to pace requests
      n_bars: bars per coin to request
      log_fn: optional logger

    Behavior:
      - Splits coins into NUM_BUCKETS deterministic buckets
      - Each call advances bucket index, fetches only that bucket
      - LKG persists across bucket rotations
      - Snapshot ts updates on every bucket completion (always considered "fresh"
        for the purposes of get_candles routing)
      - Per-coin failures fall back to LKG; never blocks tick

    Single-writer: concurrent calls coalesce — only one builder runs at a time.
    """
    # Single-writer guard
    with _SNAPSHOT_LOCK:
        if _GLOBAL_SNAPSHOT.get('building'):
            return _GLOBAL_SNAPSHOT['tf'].get(tf, {})
        _GLOBAL_SNAPSHOT['building'] = True

    try:
        coins_list = list(coins)
        n_total = len(coins_list)
        if n_total == 0:
            return {}

        # Compute bucket for this tick
        bucket_idx = _BUCKET_INDEX.get(tf, 0)
        bucket_size = max(1, (n_total + NUM_BUCKETS - 1) // NUM_BUCKETS)
        start_i = (bucket_idx % NUM_BUCKETS) * bucket_size
        end_i = min(start_i + bucket_size, n_total)
        bucket_coins = coins_list[start_i:end_i]

        # Advance bucket pointer for next tick
        _BUCKET_INDEX[tf] = (bucket_idx + 1) % NUM_BUCKETS

        start = time.time()
        ok_count = 0
        fail_count = 0

        for coin in bucket_coins:
            coin_u = coin.upper()
            try:
                if throttle_fn:
                    throttle_fn()
                candles = fetch_fn(coin_u, tf, n_bars)
                if candles and len(candles) > 0:
                    # Update LKG and current snapshot
                    _LAST_KNOWN_GOOD[tf][coin_u] = candles
                    with _SNAPSHOT_LOCK:
                        _GLOBAL_SNAPSHOT['tf'][tf][coin_u] = candles
                    ok_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                _STATS['fetch_errors'] += 1
                fail_count += 1
                if log_fn:
                    log_fn(f"snapshot fetch err {coin_u} {tf}: {e}")

        # Mark snapshot fresh (timestamp on bucket completion, not full universe)
        with _SNAPSHOT_LOCK:
            _GLOBAL_SNAPSHOT['ts'] = time.time()

        duration = time.time() - start
        _STATS['snapshots_built'] += 1
        _STATS['bucket_builds'] += 1
        _STATS['last_bucket_index'] = bucket_idx
        _STATS['last_build_duration_s'] = round(duration, 2)
        _STATS['last_build_coins_ok'] = ok_count
        _STATS['last_build_coins_failed'] = fail_count

        if log_fn:
            log_fn(f"[snapshot] bucket {bucket_idx+1}/{NUM_BUCKETS} tf={tf} "
                   f"coins={ok_count}/{len(bucket_coins)} failed={fail_count} "
                   f"dur={duration:.1f}s lkg_size={len(_LAST_KNOWN_GOOD.get(tf, {}))}")

        return _GLOBAL_SNAPSHOT['tf'][tf]
    finally:
        with _SNAPSHOT_LOCK:
            _GLOBAL_SNAPSHOT['building'] = False


def invalidate():
    """Force snapshot rebuild on next build_snapshot() call. Test/debug only."""
    with _SNAPSHOT_LOCK:
        _GLOBAL_SNAPSHOT['ts'] = 0.0

