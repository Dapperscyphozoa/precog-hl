"""Convexity scorer — silent telemetry.

At every signal fire, computes a convexity score (0-1) capturing payoff asymmetry.
Logs the score alongside eventual outcome. After 100 closed trades, comparing
high-convexity vs low-convexity WR+PnL tells us whether size should scale with
convexity in Stage 4.

NO LIVE SIZING IMPACT until explicitly activated.

Convexity components:
1. R:R ratio (TP/SL) — baseline asymmetry
2. Tail win fraction — historical fraction of wins that exceeded TP by 20%+
3. Variance cost — standard deviation of trade P&L
4. Fee drag — fixed 0.23% cost floor

Score formula:
  convexity = (R:R × tail_win_pct) / max(variance_cost + fee_drag, 0.1)
  normalized to 0-1 via sigmoid

Size multiplier (WHEN ACTIVATED):
  0.80-1.00  → 1.5x  (strong asymmetry)
  0.50-0.80  → 1.0x  (normal +EV)
  0.20-0.50  → 0.7x  (linear)
  <0.20      → 0.3x  (concave / skip)
"""
import json, os, time, threading, math
from collections import defaultdict

LOG_PATH = os.environ.get('CONVEX_LOG_PATH', '/app/convex_scores.jsonl')
MAX_LINES = 20_000
TRIGGER_THRESHOLD = 100
_LOCK = threading.Lock()
_LOG_PREFIX = '[convex]'
_TRIGGER_FIRED = False

# In-memory cache of tail-win statistics per (coin, engine). Rebuilt from log on startup.
_TAIL_STATS = defaultdict(lambda: {'wins': 0, 'tail_wins': 0})


def _compute_tail_win_pct(coin, engine, default=0.25):
    """Historical fraction of wins that exceeded TP by >=20%.

    Defaults to 0.25 when insufficient samples (< 5 wins).
    """
    s = _TAIL_STATS.get((coin, engine)) or {}
    wins = s.get('wins', 0)
    if wins < 5:
        return default
    return s['tail_wins'] / wins


def _compute_variance_cost(coin, engine, default=0.025):
    """Standard deviation of trade outcomes for this (coin, engine) pair.

    Rebuilt from log. Defaults to 2.5% when insufficient data.
    """
    # Lightweight: scan last 500 log entries for matching coin/engine outcomes
    if not os.path.exists(LOG_PATH):
        return default
    try:
        pnls = []
        with open(LOG_PATH) as f:
            for line in f.readlines()[-500:]:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get('type') == 'outcome' and r.get('coin') == coin and r.get('engine') == engine:
                    pnls.append(r.get('pnl_pct', 0))
        if len(pnls) < 5:
            return default
        mean = sum(pnls) / len(pnls)
        var = sum((p - mean) ** 2 for p in pnls) / len(pnls)
        return max(math.sqrt(var) / 100, 0.005)  # convert % to decimal, floor 0.5%
    except Exception:
        return default


def score(coin, engine, side, tp_pct, sl_pct, wilson_lb=None):
    """Compute convexity score for this signal.

    Returns dict with score, multiplier (for future activation), and components.
    """
    if tp_pct <= 0 or sl_pct <= 0:
        return {'score': 0, 'reason': 'invalid_tp_sl'}
    rr = tp_pct / sl_pct
    tail_win_pct = _compute_tail_win_pct(coin, engine)
    variance_cost = _compute_variance_cost(coin, engine)
    fee_drag = 0.0023  # 0.23% baseline
    denom = max(variance_cost + fee_drag, 0.01)

    raw = (rr * tail_win_pct) / denom
    # Sigmoid-normalize to 0-1. With priors (rr=2.5, tail_win=0.25, var=0.025+0.0023),
    # raw typically 22-28. Recenter so default-state raw lands ~0.5 and genuine
    # convexity (higher rr + higher tail) maps to 0.7+.
    # Tuned: raw=25 → 0.50, raw=35 → 0.78, raw=15 → 0.22
    normalized = 1 / (1 + math.exp(-(raw - 25) / 4))

    # What size multiplier WOULD apply if activated
    if normalized >= 0.80: mult = 1.5
    elif normalized >= 0.50: mult = 1.0
    elif normalized >= 0.20: mult = 0.7
    else: mult = 0.3

    return {
        'score': round(normalized, 3),
        'size_multiplier_if_activated': mult,
        'rr': round(rr, 2),
        'tail_win_pct': round(tail_win_pct, 3),
        'variance_cost': round(variance_cost, 4),
        'raw_score': round(raw, 2),
        'wilson_lb': wilson_lb,
    }


def log_signal_score(coin, side, engine, tp_pct, sl_pct, wilson_lb=None,
                     bar_ts=None, actual_size=None):
    """Called at signal fire. Records convexity telemetry."""
    def _do():
        try:
            s = score(coin, engine, side, tp_pct, sl_pct, wilson_lb)
            rec = {
                'type': 'signal',
                'ts': int(time.time()),
                'bar_ts': int(bar_ts) if bar_ts else int(time.time()),
                'coin': coin,
                'side': side,
                'engine': engine,
                'tp_pct': tp_pct,
                'sl_pct': sl_pct,
                'actual_size': actual_size,
                **s,
            }
            with _LOCK:
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
        except Exception as e:
            print(f"{_LOG_PREFIX} signal log err {coin}: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def log_outcome(coin, engine, pnl_pct, win, max_favorable_excursion_pct=None,
                tp_hit_pct=None, bar_ts=None):
    """Called from record_close. Updates tail-win stats and logs outcome.

    max_favorable_excursion_pct: peak unrealized % during the hold (if tracked)
    tp_hit_pct: 1.0 if TP hit, else partial fraction (for outcome typing)
    """
    def _do():
        try:
            # Update tail-win stats
            tail_win = False
            if win and max_favorable_excursion_pct is not None and tp_hit_pct is not None:
                # Tail = MFE exceeded TP by >=20%
                if max_favorable_excursion_pct >= tp_hit_pct * 1.2:
                    tail_win = True
            elif win and max_favorable_excursion_pct is None:
                # Fallback: assume tail if PnL itself > 1.5× TP target (partial exit beyond TP)
                tail_win = pnl_pct > 0  # placeholder; real tracking needs MFE

            key = (coin, engine)
            s = _TAIL_STATS[key]
            if win:
                s['wins'] += 1
                if tail_win: s['tail_wins'] += 1

            rec = {
                'type': 'outcome',
                'ts': int(time.time()),
                'bar_ts': int(bar_ts) if bar_ts else None,
                'coin': coin,
                'engine': engine,
                'pnl_pct': round(float(pnl_pct), 3),
                'win': bool(win),
                'tail_win': bool(tail_win),
                'mfe_pct': max_favorable_excursion_pct,
            }
            with _LOCK:
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
                _check_trigger()
        except Exception as e:
            print(f"{_LOG_PREFIX} outcome err: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def _check_trigger():
    """Fire discussion flag at 100 outcomes."""
    global _TRIGGER_FIRED
    if _TRIGGER_FIRED: return
    try:
        if not os.path.exists(LOG_PATH): return
        n_outcomes = 0
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r.get('type') == 'outcome': n_outcomes += 1
                except Exception: continue
        if n_outcomes >= TRIGGER_THRESHOLD:
            _TRIGGER_FIRED = True
            print(f"{_LOG_PREFIX} ★★★ TRIGGER: {n_outcomes} outcomes accumulated. "
                  f"Ready for convexity analysis + activation decision. ★★★",
                  flush=True)
    except Exception:
        pass


def _rebuild_tail_stats():
    """On startup, rebuild tail-win cache from log."""
    if not os.path.exists(LOG_PATH):
        return
    try:
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get('type') != 'outcome': continue
                if not r.get('win'): continue
                key = (r.get('coin'), r.get('engine'))
                _TAIL_STATS[key]['wins'] += 1
                if r.get('tail_win'): _TAIL_STATS[key]['tail_wins'] += 1
    except Exception:
        pass


def get_stats():
    """Summary: convexity distribution of signals, WR by score bucket."""
    if not os.path.exists(LOG_PATH):
        return {'total_signals': 0, 'total_outcomes': 0, 'trigger_threshold': TRIGGER_THRESHOLD,
                'trigger_fired': False}
    buckets = {'convex_0.80+': {'n':0,'wins':0,'pnl_sum':0},
               'mild_0.50-0.80': {'n':0,'wins':0,'pnl_sum':0},
               'linear_0.20-0.50': {'n':0,'wins':0,'pnl_sum':0},
               'concave_<0.20': {'n':0,'wins':0,'pnl_sum':0}}
    signal_count = 0
    outcome_count = 0
    # Build signal_ts -> score map, then pair with outcomes
    signals = {}
    try:
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get('type') == 'signal':
                    signal_count += 1
                    key = (r.get('coin'), r.get('bar_ts'))
                    signals[key] = r.get('score', 0)
                elif r.get('type') == 'outcome':
                    outcome_count += 1
                    key = (r.get('coin'), r.get('bar_ts'))
                    s = signals.get(key)
                    if s is None: continue
                    if s >= 0.80: bucket = 'convex_0.80+'
                    elif s >= 0.50: bucket = 'mild_0.50-0.80'
                    elif s >= 0.20: bucket = 'linear_0.20-0.50'
                    else: bucket = 'concave_<0.20'
                    b = buckets[bucket]
                    b['n'] += 1
                    if r.get('win'): b['wins'] += 1
                    b['pnl_sum'] += r.get('pnl_pct', 0)
    except Exception as e:
        return {'error': str(e)}
    for name, b in buckets.items():
        if b['n']:
            b['wr'] = round(b['wins'] / b['n'], 3)
            b['avg_pnl'] = round(b['pnl_sum'] / b['n'], 3)
    return {
        'total_signals': signal_count,
        'total_outcomes': outcome_count,
        'trigger_threshold': TRIGGER_THRESHOLD,
        'trigger_fired': _TRIGGER_FIRED,
        'by_convexity_bucket': buckets,
        'tail_stats_cache': {f'{k[0]}/{k[1]}': v for k, v in _TAIL_STATS.items() if v['wins'] >= 3},
        'file_size_kb': round(os.path.getsize(LOG_PATH) / 1024, 1),
    }


def trim_log():
    """Background trim."""
    try:
        if not os.path.exists(LOG_PATH): return
        with _LOCK:
            with open(LOG_PATH) as f:
                lines = f.readlines()
            if len(lines) <= MAX_LINES: return
            with open(LOG_PATH, 'w') as f:
                f.writelines(lines[-MAX_LINES:])
    except Exception:
        pass


def start_trim_daemon():
    def _loop():
        while True:
            time.sleep(3600)
            trim_log()
    threading.Thread(target=_loop, daemon=True).start()


# Initialize on import
_rebuild_tail_stats()
