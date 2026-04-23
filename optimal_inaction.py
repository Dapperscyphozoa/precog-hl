"""Optimal inaction detector — silent telemetry.

At every signal fire, computes abstain_score (0-1) from 4 no-trade zones:

1. post_shock_decay — BTC moved >3% in last 2 bars
2. low_liquidity_hour — UTC 2-6am
3. regime_indeterminacy — 1h/30m regime disagreement
4. consensus_exhaustion — 5+ TPs hit same direction in last 10 bars

Logs score + would_have_abstained flag alongside eventual outcome.
At 100 closes, compare WR for low-abstain-score vs would-abstain-score
trades. Activate live gate only if abstain-bucket WR < 45%.

No live gating until activated.
"""
import json, os, time, threading, urllib.request, math
from collections import defaultdict, deque

LOG_PATH = os.environ.get('ABSTAIN_LOG_PATH', '/app/abstain_scores.jsonl')
MAX_LINES = 15_000
TRIGGER_THRESHOLD = 100
_LOCK = threading.Lock()
_LOG_PREFIX = '[abstain]'
_TRIGGER_FIRED = False

# In-memory recent-TP tracker for consensus exhaustion
_RECENT_TPS = deque(maxlen=50)  # [(ts, coin, side), ...]


def _post_shock_decay_score():
    """1.0 if BTC moved >3% in last 2 bars, decaying over next 6 bars."""
    try:
        body = json.dumps({'type': 'candleSnapshot',
            'req': {'coin': 'BTC', 'interval': '15m',
                    'startTime': int(time.time() * 1000) - 10 * 900_000,
                    'endTime': int(time.time() * 1000)}
        }).encode()
        req = urllib.request.Request('https://api.hyperliquid.xyz/info',
            data=body, method='POST',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=4) as r:
            bars = json.loads(r.read())
        if len(bars) < 4: return 0.0
        closes = [float(b['c']) for b in bars]
        # Rolling 2-bar returns
        recent_moves = []
        for i in range(len(closes) - 2, len(closes)):
            if i >= 2:
                ret = abs((closes[i] - closes[i-2]) / closes[i-2])
                recent_moves.append(ret)
        # Score: 1.0 if any recent 2-bar move > 3%, decaying by bars-since
        max_ret = max(recent_moves) if recent_moves else 0
        if max_ret > 0.03:
            # Find how many bars back the spike was
            for i in range(len(closes) - 1, 1, -1):
                ret_i = abs((closes[i] - closes[i-2]) / closes[i-2])
                if ret_i > 0.03:
                    bars_since = len(closes) - 1 - i
                    # Decay linearly over 6 bars
                    return max(0, 1.0 - bars_since / 6.0)
        return 0.0
    except Exception:
        return 0.0


def _low_liquidity_hour_score():
    """1.0 during UTC 2-6am, 0.3 during 20-24 UTC (pre-London), 0 otherwise."""
    utc_h = time.gmtime().tm_hour
    if 2 <= utc_h < 6: return 1.0
    if 20 <= utc_h < 24: return 0.3
    return 0.0


def _regime_indeterminacy_score():
    """1.0 if 1h and 30m regime classifiers disagree. 0 otherwise."""
    try:
        import regime_detector as rd
        status = rd.status()
        raw_1h = status.get('raw_1h')
        raw_30m = status.get('raw_30m')
        if not raw_1h or not raw_30m: return 0.0
        if raw_1h != raw_30m: return 1.0
        return 0.0
    except Exception:
        return 0.0


def _consensus_exhaustion_score(current_side):
    """1.0 if 5+ same-direction TPs in last 10 bars (~2.5h). Scales with count."""
    now = time.time()
    cutoff = now - 10 * 15 * 60  # 10 bars × 15min
    # Clean old entries
    while _RECENT_TPS and _RECENT_TPS[0][0] < cutoff:
        _RECENT_TPS.popleft()
    same_dir_count = sum(1 for (_, _, side) in _RECENT_TPS if side == current_side)
    if same_dir_count >= 5:
        # Scale: 5 → 1.0, 8+ → 1.0 capped
        return min(1.0, same_dir_count / 5.0)
    elif same_dir_count >= 3:
        return 0.4
    return 0.0


def record_tp_hit(coin, side):
    """Call from record_close when TP is hit. Feeds consensus_exhaustion."""
    _RECENT_TPS.append((time.time(), coin, side))


def compute_abstain(side):
    """Return {score, components, would_abstain, reason}."""
    shock = _post_shock_decay_score()
    liq = _low_liquidity_hour_score()
    regime = _regime_indeterminacy_score()
    consensus = _consensus_exhaustion_score(side)
    score = max(shock, liq, regime, consensus)

    if score >= 0.8:
        action = 'abstain'
    elif score >= 0.5:
        action = 'ensemble_required'
    elif score >= 0.3:
        action = 'reduce_size_0.5x'
    else:
        action = 'trade_normally'

    components = {
        'post_shock_decay': round(shock, 3),
        'low_liquidity_hour': round(liq, 3),
        'regime_indeterminacy': round(regime, 3),
        'consensus_exhaustion': round(consensus, 3),
    }
    reason = max(components, key=components.get) if score > 0 else 'clean'

    return {
        'score': round(score, 3),
        'components': components,
        'would_abstain': score >= 0.8,
        'recommended_action': action,
        'dominant_reason': reason,
    }


def log_signal_abstain(coin, side, engine, regime, bar_ts=None):
    """Called at signal fire. Records abstain telemetry. Non-blocking."""
    def _do():
        try:
            a = compute_abstain(side)
            rec = {
                'type': 'signal',
                'ts': int(time.time()),
                'bar_ts': int(bar_ts) if bar_ts else int(time.time()),
                'coin': coin,
                'side': side,
                'engine': engine,
                'regime': regime,
                **a,
            }
            with _LOCK:
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
        except Exception as e:
            print(f"{_LOG_PREFIX} log err {coin}: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def log_outcome(coin, engine, pnl_pct, win, bar_ts=None, exit_reason=None):
    """Called from record_close. Pairs with signal log for trigger analysis."""
    def _do():
        try:
            # Feed TP hits into consensus tracker
            if exit_reason == 'tp' and win:
                record_tp_hit(coin, 'BUY' if pnl_pct > 0 else 'SELL')  # approximation

            rec = {
                'type': 'outcome',
                'ts': int(time.time()),
                'bar_ts': int(bar_ts) if bar_ts else None,
                'coin': coin,
                'engine': engine,
                'pnl_pct': round(float(pnl_pct), 3),
                'win': bool(win),
                'exit_reason': exit_reason,
            }
            with _LOCK:
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
                _check_trigger()
        except Exception as e:
            print(f"{_LOG_PREFIX} outcome err: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def _check_trigger():
    global _TRIGGER_FIRED
    if _TRIGGER_FIRED: return
    try:
        if not os.path.exists(LOG_PATH): return
        n_outcomes = 0
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    if json.loads(line).get('type') == 'outcome':
                        n_outcomes += 1
                except Exception: continue
        if n_outcomes >= TRIGGER_THRESHOLD:
            _TRIGGER_FIRED = True
            print(f"{_LOG_PREFIX} ★★★ TRIGGER: {n_outcomes} outcomes. "
                  f"Ready for abstain activation decision. ★★★", flush=True)
    except Exception:
        pass


def get_stats():
    if not os.path.exists(LOG_PATH):
        return {'total_signals': 0, 'total_outcomes': 0,
                'trigger_threshold': TRIGGER_THRESHOLD, 'trigger_fired': False}

    buckets = {
        'abstain_0.8+': {'n': 0, 'wins': 0, 'pnl_sum': 0},
        'risky_0.5-0.8': {'n': 0, 'wins': 0, 'pnl_sum': 0},
        'reduce_0.3-0.5': {'n': 0, 'wins': 0, 'pnl_sum': 0},
        'clean_<0.3': {'n': 0, 'wins': 0, 'pnl_sum': 0},
    }

    signals_by_key = {}
    signal_count = 0
    outcome_count = 0

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
                    signals_by_key[key] = r.get('score', 0)
                elif r.get('type') == 'outcome':
                    outcome_count += 1
                    key = (r.get('coin'), r.get('bar_ts'))
                    s = signals_by_key.get(key)
                    if s is None: continue
                    if s >= 0.8: bucket = 'abstain_0.8+'
                    elif s >= 0.5: bucket = 'risky_0.5-0.8'
                    elif s >= 0.3: bucket = 'reduce_0.3-0.5'
                    else: bucket = 'clean_<0.3'
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
        'by_abstain_bucket': buckets,
        'activation_rule': (
            'If abstain_0.8+ bucket WR < 0.45 AND clean bucket WR > 0.55 → ACTIVATE. '
            'If abstain bucket WR >= 0.50 → ABANDON framework (priors wrong).'
        ),
    }


def trim_log():
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
