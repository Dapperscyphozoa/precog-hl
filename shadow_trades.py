"""Shadow trading — log every REJECTED trade as if it had been taken.

For every BLOCK/SKIP decision in the signal pipeline, this module:
- Records entry price, side, intended TP/SL, rejection reason
- Classifies rejection as EDGE (signal-quality filter) or CAPACITY (resource limit)
- Tracks price over subsequent bars
- Marks outcome when price hits TP or SL (whichever first)
- Aggregates stats per reason AND per class

Class semantics:
- EDGE_REJECTION: the system decided the signal wasn't good enough
  (failed filters, regime mismatch, conf too low). If these shadow WR
  is HIGH, the filters are too aggressive.
- CAPACITY_REJECTION: signal WAS acceptable but resource exhausted
  (position cap, margin lock). If these shadow WR is positive, you're
  LEAVING MONEY on the table — raise caps or reduce per-trade risk.
- UNCLASSIFIED: reason not mapped; treated as edge by default.

USAGE:
    record_rejection('BTC', 'BUY', 95000.0, 0.05, 0.025, 'conf_below_floor')
    resolve_pending(get_price_fn)
    status()  # {'by_class': {EDGE: {...}, CAPACITY: {...}}, 'by_reason': {...}}

Storage: /var/data/shadow_trades.jsonl for persistence across restarts.
"""
import os, json, time, threading
from collections import defaultdict

LOG_PATH = os.environ.get('SHADOW_TRADES_PATH', '/var/data/shadow_trades.jsonl')
MAX_PENDING_AGE_SEC = 6 * 3600  # 6h — give up on trades that haven't hit TP or SL
MAX_PENDING = 2000              # cap in-memory pending list

# Classification map: reason → class
# EDGE_REJECTION = edge/quality filter rejected this trade
# CAPACITY_REJECTION = would have taken it but resources exhausted
CLASSIFICATION = {
    # EDGE — signal filters / quality gates
    'conf_below_floor':        'EDGE',
    'v3_trend_block':          'EDGE',
    'atr_min_block':           'EDGE',
    'funding_block':           'EDGE',
    'btc_correlation_block':   'EDGE',
    'not_elite_whitelisted':   'EDGE',
    'htf_opposing_block':      'EDGE',
    'regime_mismatch':         'EDGE',
    'chop_cooldown':           'EDGE',
    # Per-coin filters are also edge (quality gates per coin)
    # Handled by prefix match below.

    # CAPACITY — system-level resource limits
    'max_positions_cap':       'CAPACITY',
    'same_side_cap':           'CAPACITY',
    'margin_tight':            'CAPACITY',
    'drawdown_circuit':        'CAPACITY',
    'tier_bump_blocked':       'CAPACITY',
    'cb_pause':                'CAPACITY',
}


def classify_reason(reason):
    """Return 'EDGE' | 'CAPACITY' | 'UNCLASSIFIED' for a rejection reason."""
    if reason in CLASSIFICATION:
        return CLASSIFICATION[reason]
    # Per-coin filter variants: e.g. 'per_coin_filter_rsi', 'per_coin_filter_adx'
    if reason.startswith('per_coin_filter'):
        return 'EDGE'
    # Default unknown reasons to EDGE (conservative — we ASSUME filter
    # was intentional rather than capacity). Unclassified surfaces in
    # stats so we can spot unmapped reasons.
    return 'UNCLASSIFIED'


_LOCK = threading.Lock()
_PENDING = []   # active shadow trades, awaiting TP/SL resolution
_RESOLVED = []  # historical resolved trades (in-memory; also flushed to jsonl)
_STATS_COUNTER = defaultdict(int)


def _append_log(record):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        print(f'[shadow_trades] log write err: {e}', flush=True)


def record_rejection(coin, side, entry_price, tp_pct, sl_pct, reason, meta=None):
    """Record a rejected trade for shadow tracking.

    Args:
        coin: instrument symbol
        side: 'BUY' or 'SELL'
        entry_price: the price at which the trade would have been entered
        tp_pct: intended TP percentage (e.g., 0.05 for 5%)
        sl_pct: intended SL percentage (e.g., 0.025 for 2.5%)
        reason: rejection reason string (e.g., 'conf_below_threshold', 'htf_block')
        meta: optional dict of additional context (regime, conf_score, etc.)
    """
    if not entry_price or not tp_pct or not sl_pct:
        return  # can't track without these
    if side not in ('BUY', 'SELL'):
        return

    if side == 'BUY':
        tp_target = entry_price * (1 + tp_pct)
        sl_target = entry_price * (1 - sl_pct)
    else:
        tp_target = entry_price * (1 - tp_pct)
        sl_target = entry_price * (1 + sl_pct)

    rec = {
        'coin': coin,
        'side': side,
        'entry_price': entry_price,
        'tp_target': tp_target,
        'sl_target': sl_target,
        'tp_pct': tp_pct,
        'sl_pct': sl_pct,
        'reason': reason,
        'rejection_class': classify_reason(reason),
        'meta': meta or {},
        'created_ts': time.time(),
        'status': 'pending',
    }

    with _LOCK:
        _PENDING.append(rec)
        if len(_PENDING) > MAX_PENDING:
            # Evict oldest
            _PENDING[:] = _PENDING[-MAX_PENDING:]
        _STATS_COUNTER[f'record_{reason}'] += 1


def resolve_pending(get_price_fn):
    """Scan pending shadow trades. For each, fetch current price; if price
    reached TP or SL target, mark resolved.

    Args:
        get_price_fn: callable coin → float (current price). Return None if unavailable.
    """
    now = time.time()
    with _LOCK:
        pending_snapshot = list(_PENDING)

    newly_resolved = []
    still_pending = []

    for rec in pending_snapshot:
        age = now - rec['created_ts']

        # Timeout: if 6h elapsed with neither TP nor SL hit, mark expired
        if age > MAX_PENDING_AGE_SEC:
            rec['status'] = 'expired'
            rec['resolved_ts'] = now
            rec['pnl_pct'] = 0.0
            rec['outcome'] = 'timeout'
            newly_resolved.append(rec)
            continue

        try:
            px = get_price_fn(rec['coin'])
        except Exception:
            px = None

        if px is None or px <= 0:
            still_pending.append(rec)
            continue

        # Check TP/SL
        tp_hit = False; sl_hit = False
        if rec['side'] == 'BUY':
            if px >= rec['tp_target']: tp_hit = True
            elif px <= rec['sl_target']: sl_hit = True
        else:  # SELL
            if px <= rec['tp_target']: tp_hit = True
            elif px >= rec['sl_target']: sl_hit = True

        if tp_hit:
            rec['status'] = 'resolved'
            rec['outcome'] = 'tp'
            rec['resolved_ts'] = now
            rec['pnl_pct'] = rec['tp_pct'] * 100.0  # TP hit = full TP pct profit
            rec['hold_sec'] = age
            newly_resolved.append(rec)
        elif sl_hit:
            rec['status'] = 'resolved'
            rec['outcome'] = 'sl'
            rec['resolved_ts'] = now
            rec['pnl_pct'] = -rec['sl_pct'] * 100.0
            rec['hold_sec'] = age
            newly_resolved.append(rec)
        else:
            still_pending.append(rec)

    if newly_resolved:
        with _LOCK:
            _PENDING[:] = still_pending
            _RESOLVED.extend(newly_resolved)
            if len(_RESOLVED) > 5000:
                _RESOLVED[:] = _RESOLVED[-5000:]
            for rec in newly_resolved:
                _STATS_COUNTER[f'resolved_{rec["outcome"]}'] += 1
        for rec in newly_resolved:
            _append_log(rec)


def compute_stats():
    """Aggregate shadow outcomes by rejection reason AND by rejection class.
    Also aggregates pending (resolved+pending) counts by class for capacity vs edge
    pressure heatmap.
    """
    with _LOCK:
        resolved = list(_RESOLVED)
        pending = list(_PENDING)
    pending_count = len(pending)

    by_reason = defaultdict(lambda: {
        'n': 0, 'wins': 0, 'losses': 0, 'timeouts': 0,
        'total_pnl_pct': 0.0, 'pnl_series': [], 'class': None,
    })
    by_class = defaultdict(lambda: {
        'n': 0, 'wins': 0, 'losses': 0, 'timeouts': 0,
        'total_pnl_pct': 0.0,
        'pending': 0,
        'reasons': set(),
    })

    for rec in resolved:
        reason = rec.get('reason', 'unknown')
        outcome = rec.get('outcome')
        pnl = rec.get('pnl_pct', 0.0)
        cls = rec.get('rejection_class') or classify_reason(reason)

        br = by_reason[reason]
        br['n'] += 1
        br['total_pnl_pct'] += pnl
        br['pnl_series'].append(pnl)
        br['class'] = cls
        if outcome == 'tp': br['wins'] += 1
        elif outcome == 'sl': br['losses'] += 1
        elif outcome == 'timeout': br['timeouts'] += 1

        bc = by_class[cls]
        bc['n'] += 1
        bc['total_pnl_pct'] += pnl
        bc['reasons'].add(reason)
        if outcome == 'tp': bc['wins'] += 1
        elif outcome == 'sl': bc['losses'] += 1
        elif outcome == 'timeout': bc['timeouts'] += 1

    # Include pending in by_class so the UI can show rejection PRESSURE, not just resolved
    for rec in pending:
        reason = rec.get('reason', 'unknown')
        cls = rec.get('rejection_class') or classify_reason(reason)
        by_class[cls]['pending'] += 1
        by_class[cls]['reasons'].add(reason)

    # Compute final per-reason metrics
    result = {}
    for reason, br in by_reason.items():
        n = br['n']
        concluded = br['wins'] + br['losses']
        wr = (br['wins'] / concluded) if concluded > 0 else None
        expectancy = (br['total_pnl_pct'] / n) if n > 0 else 0
        avg_win = 0
        avg_loss = 0
        wins_pnl = [p for p in br['pnl_series'] if p > 0]
        losses_pnl = [p for p in br['pnl_series'] if p < 0]
        if wins_pnl: avg_win = sum(wins_pnl) / len(wins_pnl)
        if losses_pnl: avg_loss = sum(losses_pnl) / len(losses_pnl)
        result[reason] = {
            'n': n,
            'wins': br['wins'],
            'losses': br['losses'],
            'timeouts': br['timeouts'],
            'win_rate': round(wr, 3) if wr is not None else None,
            'expectancy_pct': round(expectancy, 3),
            'avg_win_pct': round(avg_win, 3),
            'avg_loss_pct': round(avg_loss, 3),
            'total_pnl_pct': round(br['total_pnl_pct'], 2),
            'class': br['class'],
        }

    # Per-class metrics
    class_result = {}
    for cls, bc in by_class.items():
        n = bc['n']
        concluded = bc['wins'] + bc['losses']
        wr = (bc['wins'] / concluded) if concluded > 0 else None
        expectancy = (bc['total_pnl_pct'] / n) if n > 0 else 0
        class_result[cls] = {
            'n_resolved': n,
            'pending': bc['pending'],
            'wins': bc['wins'],
            'losses': bc['losses'],
            'timeouts': bc['timeouts'],
            'win_rate': round(wr, 3) if wr is not None else None,
            'expectancy_pct': round(expectancy, 3),
            'total_pnl_pct': round(bc['total_pnl_pct'], 2),
            'reasons': sorted(list(bc['reasons'])),
        }
    return result, class_result, pending_count


def status():
    stats, class_stats, pending = compute_stats()
    sorted_stats = dict(sorted(stats.items(), key=lambda x: -x[1]['n']))
    return {
        'pending_count': pending,
        'resolved_total': sum(s['n'] for s in stats.values()),
        'counters': dict(_STATS_COUNTER),
        'by_reason': sorted_stats,
        'by_class': class_stats,
        'classification_map': CLASSIFICATION,
        'max_pending_age_sec': MAX_PENDING_AGE_SEC,
    }
