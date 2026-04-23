"""Signal state logger — parallel telemetry for future MI analysis.

At every bar close on every scanned coin, records:
- Which engines WOULD have fired (agnostic of actual trade decision)
- Confluence filter state (V3 trend, 1H pullback, 5m mom, OB/FVG, session)
- Regime classification
- Actual trade decision + eventual outcome

Purpose: accumulate parallel signal state to compute mutual information
between signals retrospectively, without altering live trading.

Output: append-only JSONL at /app/signal_states.jsonl.
Capped at ~50k lines (trimmed by background cleanup).

Trigger: at 1000+ logged states AND 500+ closed trades, prints discussion flag.
"""
import json, time, os, threading
from collections import defaultdict

LOG_PATH = os.environ.get('SIGNAL_LOG_PATH', '/app/signal_states.jsonl')
MAX_LINES = 50_000
TRIGGER_STATES = 1000
TRIGGER_CLOSES = 500
_LOCK = threading.Lock()
_LOG_PREFIX = '[signal_log]'
_TRIGGER_FIRED = False


def log_state(coin, regime, engines_fired, confluence_state, actual_fired,
              price, bar_ts, side_if_fired=None, conf_score=None):
    """Log a single bar's signal state.

    engines_fired: dict like {'PV': True, 'BB': False, 'MR': False, ...}
    confluence_state: dict with keys like {'v3_trend': 'up', 'session': 'ny', ...}
    actual_fired: whether an order was actually placed
    """
    def _do():
        try:
            rec = {
                'ts': int(time.time()),
                'bar_ts': int(bar_ts) if bar_ts else None,
                'coin': coin,
                'regime': regime,
                'engines_fired': engines_fired,
                'confluence': confluence_state,
                'actual_fired': actual_fired,
                'side': side_if_fired,
                'conf': conf_score,
                'price': float(price) if price else None,
            }
            with _LOCK:
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
        except Exception as e:
            print(f"{_LOG_PREFIX} write err {coin}: {e}", flush=True)

    t = threading.Thread(target=_do, daemon=True)
    t.start()


def log_outcome(bar_ts, coin, pnl_pct, win):
    """Attach outcome to the originating signal state. Called from record_close.

    Appends an outcome record that joins back to the signal state by (coin, bar_ts).
    """
    def _do():
        try:
            rec = {
                'type': 'outcome',
                'ts': int(time.time()),
                'bar_ts': int(bar_ts) if bar_ts else None,
                'coin': coin,
                'pnl_pct': round(float(pnl_pct), 3),
                'win': bool(win),
            }
            with _LOCK:
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
                _check_trigger()
        except Exception as e:
            print(f"{_LOG_PREFIX} outcome err: {e}", flush=True)

    t = threading.Thread(target=_do, daemon=True)
    t.start()


def _check_trigger():
    """Fire discussion trigger when thresholds met."""
    global _TRIGGER_FIRED
    if _TRIGGER_FIRED: return
    try:
        if not os.path.exists(LOG_PATH): return
        n_states = 0
        n_outcomes = 0
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r.get('type') == 'outcome': n_outcomes += 1
                    else: n_states += 1
                except Exception: continue
        if n_states >= TRIGGER_STATES and n_outcomes >= TRIGGER_CLOSES:
            _TRIGGER_FIRED = True
            print(f"{_LOG_PREFIX} ★★★ TRIGGER: {n_states} states + {n_outcomes} outcomes. "
                  f"Ready for empirical MI calculation. ★★★", flush=True)
    except Exception:
        pass


def get_stats():
    """Return current telemetry counts."""
    if not os.path.exists(LOG_PATH):
        return {'states': 0, 'outcomes': 0, 'trigger_states': TRIGGER_STATES,
                'trigger_closes': TRIGGER_CLOSES, 'trigger_fired': False}
    n_states = 0
    n_outcomes = 0
    engine_counts = defaultdict(int)
    regime_counts = defaultdict(int)
    try:
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get('type') == 'outcome':
                    n_outcomes += 1
                    continue
                n_states += 1
                regime_counts[r.get('regime') or 'unknown'] += 1
                for eng, fired in (r.get('engines_fired') or {}).items():
                    if fired: engine_counts[eng] += 1
    except Exception as e:
        return {'error': str(e)}
    return {
        'states': n_states,
        'outcomes': n_outcomes,
        'trigger_states': TRIGGER_STATES,
        'trigger_closes': TRIGGER_CLOSES,
        'trigger_fired': _TRIGGER_FIRED,
        'by_engine_fired': dict(engine_counts),
        'by_regime': dict(regime_counts),
        'file_size_kb': round(os.path.getsize(LOG_PATH) / 1024, 1) if os.path.exists(LOG_PATH) else 0,
    }


def trim_log():
    """Background trim to stay under MAX_LINES. Runs periodically."""
    try:
        if not os.path.exists(LOG_PATH): return
        with _LOCK:
            with open(LOG_PATH) as f:
                lines = f.readlines()
            if len(lines) <= MAX_LINES: return
            with open(LOG_PATH, 'w') as f:
                f.writelines(lines[-MAX_LINES:])
        print(f"{_LOG_PREFIX} trimmed from {len(lines)} to {MAX_LINES} lines", flush=True)
    except Exception as e:
        print(f"{_LOG_PREFIX} trim err: {e}", flush=True)


def start_trim_daemon():
    """Background thread to trim log every hour."""
    def _loop():
        while True:
            time.sleep(3600)
            trim_log()
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


# ═══════════════════════════════════════════════════════
# CALIBRATION ANALYZER — extends existing signal_logger.
# Reads the jsonl at LOG_PATH and produces reliability curve
# + Brier score + per-bucket expectancy. Zero new infrastructure.
# ═══════════════════════════════════════════════════════

def calibration_report():
    """Bucket trades by conf_score and compute calibration metrics."""
    if not os.path.exists(LOG_PATH):
        return {'n': 0, 'error': 'no log data'}

    # Build signal->outcome join by (coin, bar_ts)
    signals = {}
    outcomes = {}
    try:
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get('type') == 'outcome':
                    key = (r.get('coin'), r.get('bar_ts'))
                    outcomes[key] = r
                else:
                    key = (r.get('coin'), r.get('bar_ts'))
                    signals[key] = r
    except Exception as e:
        return {'error': str(e)}

    # Buckets
    buckets = {
        '0-29':   {'n': 0, 'wins': 0, 'pnl_sum': 0, 'conf_sum': 0},
        '30-49':  {'n': 0, 'wins': 0, 'pnl_sum': 0, 'conf_sum': 0},
        '50-64':  {'n': 0, 'wins': 0, 'pnl_sum': 0, 'conf_sum': 0},
        '65-79':  {'n': 0, 'wins': 0, 'pnl_sum': 0, 'conf_sum': 0},
        '80+':    {'n': 0, 'wins': 0, 'pnl_sum': 0, 'conf_sum': 0},
    }

    brier_sum = 0
    brier_n = 0
    joined = 0

    for key, sig in signals.items():
        out = outcomes.get(key)
        if not out: continue
        joined += 1
        conf = sig.get('conf_score') or sig.get('conf') or 0
        if conf is None: conf = 0
        try: conf = float(conf)
        except Exception: continue
        win = bool(out.get('win'))
        pnl = out.get('pnl_pct', 0)

        # Brier: predicted prob = conf/100
        predicted = conf / 100.0
        actual = 1.0 if win else 0.0
        brier_sum += (predicted - actual) ** 2
        brier_n += 1

        if conf < 30: bkey = '0-29'
        elif conf < 50: bkey = '30-49'
        elif conf < 65: bkey = '50-64'
        elif conf < 80: bkey = '65-79'
        else: bkey = '80+'
        b = buckets[bkey]
        b['n'] += 1
        if win: b['wins'] += 1
        b['pnl_sum'] += pnl
        b['conf_sum'] += conf

    # Compute calibration
    report = {}
    for bkey, b in buckets.items():
        if b['n'] == 0:
            report[bkey] = {'n': 0}
            continue
        observed_wr = b['wins'] / b['n']
        avg_conf = b['conf_sum'] / b['n']
        predicted_wr = avg_conf / 100.0
        delta = observed_wr - predicted_wr
        status = 'calibrated'
        if delta > 0.10: status = 'underconfident'
        elif delta < -0.10: status = 'overconfident'
        avg_pnl = b['pnl_sum'] / b['n']
        # Expectancy in % equity terms (already in pnl_pct)
        report[bkey] = {
            'n': b['n'],
            'observed_wr': round(observed_wr, 3),
            'predicted_wr': round(predicted_wr, 3),
            'delta': round(delta, 3),
            'status': status,
            'avg_pnl_pct': round(avg_pnl, 3),
            'avg_conf': round(avg_conf, 1),
        }

    brier = brier_sum / brier_n if brier_n else None

    # Mispriced zones
    mispriced = []
    for bkey, r in report.items():
        if r.get('n', 0) < 10: continue
        if r.get('status') == 'overconfident':
            mispriced.append({'bucket': bkey, 'issue': 'raise floor — overconfident',
                              'delta': r['delta']})
        elif r.get('status') == 'underconfident':
            mispriced.append({'bucket': bkey, 'issue': 'boost size — underconfident',
                              'delta': r['delta']})

    return {
        'joined_trades': joined,
        'brier_score': round(brier, 4) if brier is not None else None,
        'interpretation': (
            'brier 0.0=perfect, 0.15-0.20=well-calibrated, 0.25=random, >0.22=meaningless'
        ),
        'by_bucket': report,
        'mispriced_zones': mispriced,
    }
