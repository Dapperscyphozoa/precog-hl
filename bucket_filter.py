"""BUCKET_FILTER — per-(coin, engine, regime) MFE-positive rate veto.

Verified-EV mechanism: cut signals from buckets that historically never
go green. From live data analysis:
  - 4 of 8 trades in recent batch had ZERO MFE — directionally wrong from
    entry, no exit logic recovers them
  - Cutting these 4 turns the batch from -1.7% to +0.77%
  - Tighter SL doesn't help (filler arrives faster, doesn't change direction)
  - The only +EV lever is signal rejection

Logic:
  For each (coin, engine, regime) bucket:
    - n = trades closed (with mfe_pct present)
    - mfe_positive = trades where mfe_pct >= MFE_POSITIVE_THRESHOLD
    - rate = mfe_positive / n
  If n >= MIN_SAMPLES AND rate < MFE_RATE_FLOOR: BLOCK.

Defaults:
  MIN_SAMPLES = 3
  MFE_POSITIVE_THRESHOLD = 0.005 (+0.5%)
  MFE_RATE_FLOOR = 0.40 (40%)

Caching: bucket stats refreshed every CACHE_TTL_S (default 1 hour).
Reads /var/data/trades.csv directly (the trade_ledger CSV).

API:
  block_signal(coin, engine, regime, side) -> (blocked: bool, reason: str)
  status() -> dict for /bucket_filter_status

Tunables (env):
  BUCKET_FILTER_ENABLED         default 1
  BUCKET_MIN_SAMPLES            default 3
  BUCKET_MFE_POS_THRESHOLD      default 0.005
  BUCKET_MFE_RATE_FLOOR         default 0.40
  BUCKET_CACHE_TTL_S            default 3600
  BUCKET_TRADE_LOG_PATH         default /var/data/trades.csv
"""
import os
import csv
import time
import threading
from collections import defaultdict

ENABLED                = os.environ.get('BUCKET_FILTER_ENABLED', '1') == '1'
MIN_SAMPLES            = int(os.environ.get('BUCKET_MIN_SAMPLES', '3'))
MFE_POS_THRESHOLD      = float(os.environ.get('BUCKET_MFE_POS_THRESHOLD', '0.005'))
MFE_RATE_FLOOR         = float(os.environ.get('BUCKET_MFE_RATE_FLOOR', '0.40'))
CACHE_TTL_S            = int(os.environ.get('BUCKET_CACHE_TTL_S', '3600'))
TRADE_LOG_PATH         = os.environ.get('BUCKET_TRADE_LOG_PATH', '/var/data/trades.csv')

_LOCK = threading.Lock()
_BUCKETS = {}      # (coin, engine, regime) -> {'n': int, 'mfe_pos': int}
_LAST_REFRESH = 0
_LAST_REFRESH_ERR = None
_REFRESH_COUNT = 0
_BLOCK_COUNT = defaultdict(int)  # bucket key -> # times blocked


def _log(msg):
    print(f'[bucket_filter] {msg}', flush=True)


def _refresh():
    """Re-read trade log CSV and recompute bucket stats."""
    global _LAST_REFRESH, _LAST_REFRESH_ERR, _REFRESH_COUNT
    if not os.path.exists(TRADE_LOG_PATH):
        _LAST_REFRESH_ERR = f'CSV not found: {TRADE_LOG_PATH}'
        return False
    new_buckets = defaultdict(lambda: {'n': 0, 'mfe_pos': 0})
    try:
        with open(TRADE_LOG_PATH) as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('event_type') != 'CLOSE':
                    continue
                coin = (row.get('coin') or '').strip()
                engine = (row.get('engine') or '').strip()
                regime = (row.get('regime') or '').strip().lower()
                if not coin or not engine:
                    continue
                # Skip noise engines
                if engine in ('RECONCILED', 'untagged_legacy', ''):
                    continue
                mfe_raw = row.get('mfe_pct', '')
                if mfe_raw in (None, ''):
                    continue
                try:
                    mfe = float(mfe_raw)
                except (TypeError, ValueError):
                    continue
                key = (coin, engine, regime)
                new_buckets[key]['n'] += 1
                if mfe >= MFE_POS_THRESHOLD:
                    new_buckets[key]['mfe_pos'] += 1
        with _LOCK:
            _BUCKETS.clear()
            _BUCKETS.update(new_buckets)
            _LAST_REFRESH = time.time()
            _LAST_REFRESH_ERR = None
            _REFRESH_COUNT += 1
        _log(f'refreshed: {len(new_buckets)} buckets across all coins/engines/regimes')
        return True
    except Exception as e:
        _LAST_REFRESH_ERR = f'{type(e).__name__}: {e}'
        _log(f'refresh err: {_LAST_REFRESH_ERR}')
        return False


def _maybe_refresh():
    """Auto-refresh if stale."""
    if time.time() - _LAST_REFRESH > CACHE_TTL_S:
        _refresh()


def block_signal(coin, engine, regime, side=None):
    """Decision for whether to block a signal based on historical MFE rate.

    Returns (blocked: bool, reason: str).
    Fail-soft: insufficient data, refresh err, or disabled → no block.
    """
    if not ENABLED:
        return False, 'disabled'
    _maybe_refresh()
    regime_lc = (regime or '').strip().lower()
    coin_clean = (coin or '').strip()
    engine_clean = (engine or '').strip()
    if not coin_clean or not engine_clean:
        return False, 'missing_metadata'
    key = (coin_clean, engine_clean, regime_lc)
    with _LOCK:
        b = _BUCKETS.get(key)
    if not b or b['n'] < MIN_SAMPLES:
        return False, f'insufficient_samples (n={b["n"] if b else 0}<{MIN_SAMPLES})'
    rate = b['mfe_pos'] / b['n'] if b['n'] > 0 else 0
    if rate < MFE_RATE_FLOOR:
        with _LOCK:
            _BLOCK_COUNT[f'{coin_clean}|{engine_clean}|{regime_lc}'] += 1
        return True, f'mfe_rate_below_floor ({rate*100:.0f}% < {MFE_RATE_FLOOR*100:.0f}%, n={b["n"]})'
    return False, f'allow (mfe_rate={rate*100:.0f}%, n={b["n"]})'


def status():
    """Snapshot for /bucket_filter_status endpoint."""
    _maybe_refresh()
    with _LOCK:
        buckets_snapshot = []
        for (coin, engine, regime), v in _BUCKETS.items():
            n = v['n']
            mfe_pos = v['mfe_pos']
            rate = (mfe_pos / n) if n > 0 else 0
            blocked = (n >= MIN_SAMPLES and rate < MFE_RATE_FLOOR)
            buckets_snapshot.append({
                'coin': coin, 'engine': engine, 'regime': regime,
                'n': n, 'mfe_pos': mfe_pos,
                'mfe_pos_rate_pct': round(rate * 100, 1),
                'blocked': blocked,
            })
        block_count_snapshot = dict(_BLOCK_COUNT)
    # Sort: blocked first, then by sample count desc
    buckets_snapshot.sort(key=lambda r: (-r['blocked'], -r['n']))
    blocked_list = [b for b in buckets_snapshot if b['blocked']]
    return {
        'enabled': ENABLED,
        'min_samples': MIN_SAMPLES,
        'mfe_pos_threshold_pct': MFE_POS_THRESHOLD * 100,
        'mfe_rate_floor_pct': MFE_RATE_FLOOR * 100,
        'cache_ttl_s': CACHE_TTL_S,
        'last_refresh_ts': int(_LAST_REFRESH),
        'last_refresh_age_sec': int(time.time() - _LAST_REFRESH) if _LAST_REFRESH else None,
        'last_refresh_err': _LAST_REFRESH_ERR,
        'refresh_count': _REFRESH_COUNT,
        'total_buckets': len(buckets_snapshot),
        'blocked_buckets': len(blocked_list),
        'block_invocations_per_bucket': block_count_snapshot,
        'top_blocked': blocked_list[:25],
        'top_buckets_by_sample': buckets_snapshot[:25],
    }
