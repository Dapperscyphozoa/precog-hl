"""Shadow thresholds — silent evaluation of relaxed-threshold variants.

For every bar close on every coin, evaluates 3 variants alongside production:
- production: current RSI thresholds, current pivot lookback
- relaxed_A: RSI +/-3 (e.g., 75/25 → 72/28)
- relaxed_B: RSI +/-5
- relaxed_C: pivot lookback -1 bar

Logs which variant "would have fired" per bar. Joins to outcome post-close.
At 200 bar-evals, reports WR per variant. Promote to production only if
variant's WR is within 3pp of production AND trade count is >15% higher.

Pure local computation — no LLM, no API calls beyond the bar already fetched.
No live trading impact.
"""
import json, os, time, threading
import numpy as np

LOG_PATH = os.environ.get('SHADOW_LOG_PATH', '/app/shadow_thresholds.jsonl')
_LOCK = threading.Lock()
_LOG_PREFIX = '[shadow]'
_TRIGGER_FIRED = False
TRIGGER_THRESHOLD = 200


def _rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    d = np.diff(closes)
    g = np.where(d > 0, d, 0); l = np.where(d < 0, -d, 0)
    ag = g[:period].mean(); al = l[:period].mean()
    for i in range(period, len(g)):
        ag = (ag * (period-1) + g[i]) / period
        al = (al * (period-1) + l[i]) / period
    if al == 0: return 100.0
    return 100 - 100 / (1 + ag / al)


def evaluate_shadow(coin, bars, production_rsi_hi, production_rsi_lo,
                    production_pivot_lb=5, engine='BB', actual_fired=None, actual_side=None):
    """Evaluate 4 variants for a single bar-close. Non-blocking logging.

    bars: list of [t,o,h,l,c,v] — last ~30 bars minimum
    """
    def _do():
        try:
            if len(bars) < 30:
                return
            closes = [float(b[4]) for b in bars]
            highs = [float(b[2]) for b in bars]
            lows = [float(b[3]) for b in bars]
            rsi_now = _rsi(closes)
            last_price = closes[-1]

            variants = {
                'production': {'rh': production_rsi_hi, 'rl': production_rsi_lo,
                               'pivot_lb': production_pivot_lb},
                'relaxed_A':  {'rh': production_rsi_hi - 3, 'rl': production_rsi_lo + 3,
                               'pivot_lb': production_pivot_lb},
                'relaxed_B':  {'rh': production_rsi_hi - 5, 'rl': production_rsi_lo + 5,
                               'pivot_lb': production_pivot_lb},
                'relaxed_C':  {'rh': production_rsi_hi, 'rl': production_rsi_lo,
                               'pivot_lb': max(3, production_pivot_lb - 1)},
            }

            results = {}
            for name, v in variants.items():
                # Simplified would-fire check:
                # BB-style: close below lower band + RSI below threshold = would BUY
                # (or equivalent for SELL)
                lookback = v['pivot_lb'] * 4  # enough for band calc
                if len(closes) < lookback: continue
                window = closes[-lookback:]
                mean = sum(window) / len(window)
                std = float(np.std(window))
                lower = mean - 2 * std
                upper = mean + 2 * std

                would_buy = last_price < lower and rsi_now < v['rl']
                would_sell = last_price > upper and rsi_now > v['rh']

                if would_buy: side = 'BUY'
                elif would_sell: side = 'SELL'
                else: side = None

                results[name] = {
                    'would_fire': side is not None,
                    'side': side,
                    'rsi': round(rsi_now, 2),
                    'distance_from_lower': round((last_price - lower) / last_price * 100, 3),
                    'distance_from_upper': round((upper - last_price) / last_price * 100, 3),
                }

            rec = {
                'type': 'shadow_eval',
                'ts': int(time.time()),
                'bar_ts': int(bars[-1][0]),
                'coin': coin,
                'engine': engine,
                'actual_fired': actual_fired,
                'actual_side': actual_side,
                'variants': results,
            }
            with _LOCK:
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
                _check_trigger()
        except Exception as e:
            print(f"{_LOG_PREFIX} eval err {coin}: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def log_outcome(coin, bar_ts, pnl_pct, win):
    """Join post-close outcome with earlier shadow eval."""
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

    threading.Thread(target=_do, daemon=True).start()


def _check_trigger():
    global _TRIGGER_FIRED
    if _TRIGGER_FIRED: return
    try:
        if not os.path.exists(LOG_PATH): return
        n = 0
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    if json.loads(line).get('type') == 'shadow_eval': n += 1
                except Exception: continue
        if n >= TRIGGER_THRESHOLD:
            _TRIGGER_FIRED = True
            print(f"{_LOG_PREFIX} ★★★ TRIGGER: {n} shadow evals. "
                  f"Ready for variant-promotion analysis. ★★★", flush=True)
    except Exception:
        pass


def get_stats():
    if not os.path.exists(LOG_PATH):
        return {'n_evals': 0, 'trigger_fired': False}
    variant_counts = {'production': 0, 'relaxed_A': 0, 'relaxed_B': 0, 'relaxed_C': 0}
    variant_wins = {'production': 0, 'relaxed_A': 0, 'relaxed_B': 0, 'relaxed_C': 0}
    n_evals = 0
    n_outcomes = 0
    # Build bar_ts → variants map, then join with outcomes
    evals = {}
    try:
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception: continue
                if r.get('type') == 'shadow_eval':
                    n_evals += 1
                    key = (r.get('coin'), r.get('bar_ts'))
                    evals[key] = r.get('variants', {})
                    for name, v in r.get('variants', {}).items():
                        if v.get('would_fire'): variant_counts[name] = variant_counts.get(name, 0) + 1
                elif r.get('type') == 'outcome':
                    n_outcomes += 1
                    key = (r.get('coin'), r.get('bar_ts'))
                    vs = evals.get(key)
                    if not vs: continue
                    for name, v in vs.items():
                        if v.get('would_fire') and r.get('win'):
                            variant_wins[name] = variant_wins.get(name, 0) + 1
    except Exception as e:
        return {'error': str(e)}

    summary = {}
    for name in variant_counts:
        n = variant_counts.get(name, 0)
        summary[name] = {
            'would_fire_count': n,
            'wins_joined': variant_wins.get(name, 0),
            'wr_joined': round(variant_wins[name] / n, 3) if n else None,
        }
    return {
        'n_evals': n_evals,
        'n_outcomes': n_outcomes,
        'trigger_threshold': TRIGGER_THRESHOLD,
        'trigger_fired': _TRIGGER_FIRED,
        'variant_summary': summary,
        'promotion_rule': (
            'Promote variant if (wr >= production_wr - 0.03) AND '
            '(fire_count >= production_fire_count × 1.15)'
        ),
    }
