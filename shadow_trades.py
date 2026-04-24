"""Shadow trading — log every REJECTED trade as if it had been taken.

For every BLOCK/SKIP decision in the signal pipeline, this module:
- Records entry price, side, intended TP/SL, rejection reason
- Tracks price over subsequent bars
- Marks outcome when price hits TP or SL (whichever first)
- Aggregates stats per rejection reason: would-have-been WR, expectancy, fee-adjusted PnL

Compare to taken trades: is the system rejecting winners or losers?

USAGE:
    from shadow_trades import record_rejection, resolve_pending, status

    # At every rejection point:
    record_rejection(
        coin='BTC', side='BUY', entry_price=95000.0,
        tp_pct=0.05, sl_pct=0.025,
        reason='conf_score_below_threshold',
        meta={'conf': 28, 'regime': 'chop'}
    )

    # Called periodically (every 60s from main loop or background):
    resolve_pending(get_price_fn)  # advances shadow trades, marks TP/SL hits

    # Read stats:
    status()  # {'by_reason': {reason: {n, wr, expectancy, ...}}, 'pending': N}

Storage: /var/data/shadow_trades.jsonl for persistence across restarts.
"""
import os, json, time, threading
from collections import defaultdict

LOG_PATH = os.environ.get('SHADOW_TRADES_PATH', '/var/data/shadow_trades.jsonl')
MAX_PENDING_AGE_SEC = 6 * 3600  # 6h — give up on trades that haven't hit TP or SL
MAX_PENDING = 2000              # cap in-memory pending list

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


# Friction model — MUST match live execution
FEE_ROUND_TRIP = 0.0007       # 0.07% round-trip (taker entry + taker exit)
SLIPPAGE_ROUND_TRIP = 0.0016  # 0.16% round-trip
MAX_HOLD_SEC = 6 * 3600       # 6h max hold → TIMEOUT


def _apply_friction(gross_pnl_pct):
    """Subtract round-trip friction from gross PnL pct.
    Friction is always a cost regardless of direction."""
    friction_pct = (FEE_ROUND_TRIP + SLIPPAGE_ROUND_TRIP) * 100.0
    return gross_pnl_pct - friction_pct


def resolve_pending(get_candles_fn):
    """Resolve pending shadow trades using DETERMINISTIC candle resolution.

    get_candles_fn: callable (coin, since_ts_ms) -> list of candles
      Each candle is a dict with keys: t (ms), o (open), h (high), l (low), c (close)
      Only candles with t > since_ts_ms should be returned.

    Rules:
    - Walk candles chronologically.
    - If candle high/low reaches TP: mark TP hit
    - If candle high/low reaches SL: mark SL hit
    - If both in same candle: assume SL first (CONSERVATIVE; prevents inflated EV)
    - If age > MAX_HOLD_SEC: mark TIMEOUT (pnl=0)
    - Apply round-trip friction to gross PnL
    - R-multiple = net_pnl_pct / sl_pct_pct (pre-friction sl basis)

    Args:
        get_candles_fn: as described above. May return [] or None if no data.
    """
    now = time.time()
    with _LOCK:
        pending_snapshot = list(_PENDING)

    newly_resolved = []
    still_pending = []

    for rec in pending_snapshot:
        age = now - rec['created_ts']

        # 1. TIMEOUT
        if age > MAX_HOLD_SEC:
            rec['status'] = 'resolved'
            rec['outcome'] = 'timeout'
            rec['resolved_ts'] = now
            rec['pnl_pct'] = 0.0
            rec['pnl_r'] = 0.0
            rec['hold_sec'] = age
            rec['exit_reason'] = 'max_hold_exceeded'
            newly_resolved.append(rec)
            continue

        # 2. Fetch candles since rejection
        since_ts_ms = int(rec['created_ts'] * 1000)
        try:
            candles = get_candles_fn(rec['coin'], since_ts_ms)
        except Exception:
            still_pending.append(rec)
            continue
        if not candles:
            still_pending.append(rec)
            continue

        # 3. Walk candles in chronological order
        resolved = False
        for c in candles:
            c_high = float(c.get('h', c.get('high', 0)) or 0)
            c_low = float(c.get('l', c.get('low', 0)) or 0)
            c_ts = int(c.get('t', c.get('time', 0)) or 0)
            if c_high <= 0 or c_low <= 0:
                continue
            # Only consider candles AFTER the shadow trade was created
            if c_ts <= since_ts_ms:
                continue

            tp_target = rec['tp_target']
            sl_target = rec['sl_target']
            side = rec['side']

            if side == 'BUY':
                hit_tp = c_high >= tp_target
                hit_sl = c_low <= sl_target
            else:  # SELL
                hit_tp = c_low <= tp_target
                hit_sl = c_high >= sl_target

            if hit_tp and hit_sl:
                outcome = 'sl'  # CONSERVATIVE: assume SL first
            elif hit_tp:
                outcome = 'tp'
            elif hit_sl:
                outcome = 'sl'
            else:
                continue  # no resolution this candle

            tp_pct_pct = rec['tp_pct'] * 100.0
            sl_pct_pct = rec['sl_pct'] * 100.0
            if outcome == 'tp':
                gross = tp_pct_pct
            else:
                gross = -sl_pct_pct
            net = _apply_friction(gross)
            pnl_r = net / sl_pct_pct if sl_pct_pct > 0 else 0

            rec['status'] = 'resolved'
            rec['outcome'] = outcome
            rec['resolved_ts'] = c_ts / 1000.0
            rec['gross_pnl_pct'] = round(gross, 4)
            rec['pnl_pct'] = round(net, 4)
            rec['pnl_r'] = round(pnl_r, 4)
            rec['friction_pct'] = round(gross - net, 4)
            rec['hold_sec'] = rec['resolved_ts'] - rec['created_ts']
            rec['exit_reason'] = f'candle_{outcome}'
            rec['exit_candle_ts'] = c_ts
            newly_resolved.append(rec)
            resolved = True
            break

        if not resolved:
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
    """Aggregate shadow outcomes by rejection reason."""
    with _LOCK:
        resolved = list(_RESOLVED)
        pending_count = len(_PENDING)

    by_reason = defaultdict(lambda: {
        'n': 0, 'wins': 0, 'losses': 0, 'timeouts': 0,
        'total_pnl_pct': 0.0, 'pnl_series': []
    })

    for rec in resolved:
        reason = rec.get('reason', 'unknown')
        outcome = rec.get('outcome')
        pnl = rec.get('pnl_pct', 0.0)
        br = by_reason[reason]
        br['n'] += 1
        br['total_pnl_pct'] += pnl
        br['pnl_series'].append(pnl)
        if outcome == 'tp': br['wins'] += 1
        elif outcome == 'sl': br['losses'] += 1
        elif outcome == 'timeout': br['timeouts'] += 1

    # Compute final metrics
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
        }
    return result, pending_count


def compare_live_vs_shadow(live_rmults):
    """Compute expectancy comparison between LIVE taken trades and SHADOW
    resolved rejections.

    Args:
        live_rmults: list of R-multiples from LIVE executed trades
                     (pnl_pct / sl_pct_pct, signed).

    Returns:
        {
          'live': {n, wr, avg_r, std_r},
          'shadow': {n, wr, avg_r, std_r},
          'ev_delta': avg_r(live) - avg_r(shadow),
          'by_reason': {reason: {n, wr, avg_r, class}},
          'by_class':  {class: {n, wr, avg_r}},
        }

    Rules:
      - Shadow trades use pnl_r (already post-friction) for R-mult
      - Timeouts EXCLUDED from expectancy (neutral data)
      - Min 5 samples per reason bucket to be reported
    """
    import statistics as _st

    # Live stats
    live_concluded = [r for r in (live_rmults or []) if r != 0]
    live_wins = sum(1 for r in live_concluded if r > 0)
    live_losses = sum(1 for r in live_concluded if r < 0)
    live_total = len(live_concluded)
    live_wr = live_wins / (live_wins + live_losses) if (live_wins + live_losses) > 0 else None
    live_avg_r = (sum(live_concluded) / live_total) if live_total > 0 else None
    live_std_r = _st.pstdev(live_concluded) if live_total > 1 else 0.0

    # Shadow stats (excludes timeouts)
    with _LOCK:
        resolved = list(_RESOLVED)
    shadow_active = [r for r in resolved if r.get('outcome') in ('tp', 'sl')]
    shadow_rs = [r.get('pnl_r', 0) for r in shadow_active]
    shadow_wins = sum(1 for r in shadow_rs if r > 0)
    shadow_losses = sum(1 for r in shadow_rs if r < 0)
    shadow_total = len(shadow_rs)
    shadow_wr = shadow_wins / (shadow_wins + shadow_losses) if (shadow_wins + shadow_losses) > 0 else None
    shadow_avg_r = (sum(shadow_rs) / shadow_total) if shadow_total > 0 else None
    shadow_std_r = _st.pstdev(shadow_rs) if shadow_total > 1 else 0.0

    # By reason
    by_reason = defaultdict(list)
    by_class = defaultdict(list)
    for rec in shadow_active:
        reason = rec.get('reason', 'unknown')
        cls = rec.get('rejection_class') or classify_reason(reason)
        r = rec.get('pnl_r', 0)
        by_reason[reason].append({'r': r, 'class': cls})
        by_class[cls].append(r)

    reason_out = {}
    for reason, entries in by_reason.items():
        rs = [e['r'] for e in entries]
        n = len(rs)
        if n < 5: continue  # min sample
        wins = sum(1 for r in rs if r > 0)
        losses = sum(1 for r in rs if r < 0)
        concluded = wins + losses
        reason_out[reason] = {
            'n': n,
            'wins': wins,
            'losses': losses,
            'wr': round(wins / concluded, 3) if concluded > 0 else None,
            'avg_r': round(sum(rs) / n, 3) if n > 0 else None,
            'std_r': round(_st.pstdev(rs), 3) if n > 1 else 0.0,
            'class': entries[0]['class'],
            'fvs_r': (round(sum(rs) / n - (live_avg_r or 0), 3)) if live_avg_r is not None and n > 0 else None,
        }

    class_out = {}
    for cls, rs in by_class.items():
        n = len(rs)
        if n == 0: continue
        wins = sum(1 for r in rs if r > 0)
        losses = sum(1 for r in rs if r < 0)
        class_out[cls] = {
            'n': n,
            'wr': round(wins / (wins + losses), 3) if (wins + losses) > 0 else None,
            'avg_r': round(sum(rs) / n, 3) if n > 0 else None,
            'std_r': round(_st.pstdev(rs), 3) if n > 1 else 0.0,
        }

    ev_delta = (live_avg_r - shadow_avg_r) if (live_avg_r is not None and shadow_avg_r is not None) else None

    return {
        'live': {
            'n': live_total,
            'wins': live_wins,
            'losses': live_losses,
            'wr': round(live_wr, 3) if live_wr is not None else None,
            'avg_r': round(live_avg_r, 3) if live_avg_r is not None else None,
            'std_r': round(live_std_r, 3),
        },
        'shadow': {
            'n': shadow_total,
            'wins': shadow_wins,
            'losses': shadow_losses,
            'wr': round(shadow_wr, 3) if shadow_wr is not None else None,
            'avg_r': round(shadow_avg_r, 3) if shadow_avg_r is not None else None,
            'std_r': round(shadow_std_r, 3),
        },
        'ev_delta': round(ev_delta, 3) if ev_delta is not None else None,
        'by_reason': reason_out,
        'by_class': class_out,
        'friction': {
            'fee_round_trip_pct': FEE_ROUND_TRIP * 100,
            'slippage_round_trip_pct': SLIPPAGE_ROUND_TRIP * 100,
        },
        'interpretation': (
            'live > shadow = filters creating edge; '
            'live ≈ shadow = filters neutral; '
            'live < shadow = filters destroying edge'
        ),
        'confidence': (
            'insufficient' if min(live_total, shadow_total) < 30 else
            'moderate' if min(live_total, shadow_total) < 100 else
            'high'
        ),
    }


def status():
    stats, pending = compute_stats()
    # Sort by volume
    sorted_stats = dict(sorted(stats.items(), key=lambda x: -x[1]['n']))
    return {
        'pending_count': pending,
        'resolved_total': sum(s['n'] for s in stats.values()),
        'counters': dict(_STATS_COUNTER),
        'by_reason': sorted_stats,
        'max_pending_age_sec': MAX_PENDING_AGE_SEC,
    }
