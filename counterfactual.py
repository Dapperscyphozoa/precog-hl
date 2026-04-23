"""Counterfactual engine — silent telemetry.

For every closed trade, replays 4 alternatives on the actual 15m bars that
occurred during the hold:

1. DELAYED ENTRY: entry shifted +1, +3 bars into the future
2. SKIPPED: no trade at all (regret if market moved favorably)
3. RESIZED: 0.5×, 1.5×, 2.0× actual position size
4. REMOVED SIGNAL: would the outcome survive if one confluence input was stripped?

Computes regret metrics per trade. Aggregates at /counterfactual endpoint.

Trigger: 50 closed outcomes → first meaningful delay/skip analysis.
Activation is never automatic — telemetry only.
"""
import json, os, time, threading, urllib.request, math
from collections import defaultdict

LOG_PATH = os.environ.get('COUNTERFACTUAL_LOG_PATH', '/app/counterfactual.jsonl')
MAX_LINES = 10_000
TRIGGER_THRESHOLD = 50
_LOCK = threading.Lock()
_LOG_PREFIX = '[counterfactual]'
_TRIGGER_FIRED = False


def _fetch_bars_around(coin, entry_ts_ms, bars_before=4, bars_after=40, interval='15m'):
    """Pull 15m bars spanning well past typical max hold."""
    ms_per_bar = {'15m': 900_000}[interval]
    start = entry_ts_ms - bars_before * ms_per_bar
    end = entry_ts_ms + bars_after * ms_per_bar
    body = json.dumps({
        'type': 'candleSnapshot',
        'req': {'coin': coin, 'interval': interval, 'startTime': start, 'endTime': end}
    }).encode()
    req = urllib.request.Request('https://api.hyperliquid.xyz/info',
        data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        return [(int(b['t']), float(b['o']), float(b['h']), float(b['l']),
                 float(b['c']), float(b['v'])) for b in data]
    except Exception:
        return []


def _simulate(bars, entry_idx, side, entry_price, tp_pct, sl_pct,
              fee=0.00035, slip=0.0008, max_hold=96):
    """Run TP/SL simulation from entry_idx forward. Returns (pnl_decimal, exit_reason)."""
    if entry_idx < 0 or entry_idx >= len(bars):
        return (0, 'no_bars')
    adj_entry = entry_price * (1 + slip if side == 'BUY' else 1 - slip)
    for j in range(entry_idx + 1, min(entry_idx + 1 + max_hold, len(bars))):
        hi, lo = bars[j][2], bars[j][3]
        if side == 'BUY':
            tp_px = adj_entry * (1 + tp_pct)
            sl_px = adj_entry * (1 - sl_pct)
            if lo <= sl_px: return (-sl_pct - slip*2 - fee*2, 'sl')
            if hi >= tp_px: return (tp_pct - slip*2 - fee*2, 'tp')
        else:
            tp_px = adj_entry * (1 - tp_pct)
            sl_px = adj_entry * (1 + sl_pct)
            if hi >= sl_px: return (-sl_pct - slip*2 - fee*2, 'sl')
            if lo <= tp_px: return (tp_pct - slip*2 - fee*2, 'tp')
    # Timeout
    final = bars[min(entry_idx + max_hold, len(bars) - 1)][4]
    raw = (final - adj_entry) / adj_entry if side == 'BUY' else (adj_entry - final) / adj_entry
    return (raw - slip*2 - fee*2, 'timeout')


def analyze_close(coin, side, entry_price, tp_pct, sl_pct, entry_ts, pnl_pct,
                  actual_size_pct=0.005, engine=None, regime=None):
    """Run counterfactuals on a closed trade. Non-blocking."""
    def _do():
        try:
            ts_ms = int(entry_ts * 1000) if entry_ts < 1e12 else int(entry_ts)
            bars = _fetch_bars_around(coin, ts_ms)
            if len(bars) < 10:
                return
            # Find entry bar index (first bar >= entry_ts)
            entry_idx = next((i for i, b in enumerate(bars) if b[0] >= ts_ms), None)
            if entry_idx is None or entry_idx >= len(bars) - 3:
                return

            # ─── COUNTERFACTUAL 1: Delayed entry ───
            delays = {}
            for n_bars in (1, 3):
                delayed_idx = entry_idx + n_bars
                if delayed_idx < len(bars):
                    delayed_entry = bars[delayed_idx][4]  # close of delayed bar
                    pnl_d, reason_d = _simulate(bars, delayed_idx, side, delayed_entry,
                                                 tp_pct, sl_pct)
                    delays[f'delay_{n_bars}'] = {
                        'pnl_pct': round(pnl_d * 100, 3),
                        'reason': reason_d,
                        'delta_vs_actual': round((pnl_d * 100) - pnl_pct, 3),
                    }

            # ─── COUNTERFACTUAL 2: Skipped ───
            # What did the market do over typical hold window? Compute the
            # opportunity cost / avoided loss.
            skip_pnl = 0  # Skipping = 0 P&L
            skip_delta = skip_pnl - pnl_pct
            skipped = {
                'pnl_pct': 0,
                'delta_vs_actual': round(-pnl_pct, 3),
                'avoided_loss': pnl_pct < 0,
                'missed_profit': pnl_pct > 0,
            }

            # ─── COUNTERFACTUAL 3: Resized ───
            sizes = {}
            for mult in (0.5, 1.5, 2.0):
                # Scaled P&L in % of equity (linear scaling — liq constraint ignored at telemetry layer)
                scaled_pnl = pnl_pct * mult
                sizes[f'size_{mult}x'] = {
                    'pnl_pct': round(scaled_pnl, 3),
                    'delta_vs_actual': round(scaled_pnl - pnl_pct, 3),
                }

            # ─── COUNTERFACTUAL 4: Signal removed (abstract) ───
            # We can't replay the full signal stack without bar data per filter.
            # Approximation: if regime was "correct" bucket vs transition, note it.
            # Full implementation requires parallel signal-state from signal_logger.jsonl.
            signal_removed = {
                'note': 'requires signal_logger join — deferred to Stage 5 analysis',
                'placeholder': True,
            }

            # ─── Regret metrics ───
            all_deltas = []
            for d in delays.values(): all_deltas.append(d['delta_vs_actual'])
            all_deltas.append(skipped['delta_vs_actual'])
            for s in sizes.values(): all_deltas.append(s['delta_vs_actual'])
            best_delta = max(all_deltas) if all_deltas else 0
            worst_delta = min(all_deltas) if all_deltas else 0

            rec = {
                'ts': int(time.time()),
                'bar_ts': ts_ms,
                'coin': coin,
                'side': side,
                'engine': engine,
                'regime': regime,
                'entry_price': entry_price,
                'tp_pct': tp_pct,
                'sl_pct': sl_pct,
                'actual_pnl_pct': round(float(pnl_pct), 3),
                'actual_win': pnl_pct > 0,
                'counterfactuals': {
                    'delayed': delays,
                    'skipped': skipped,
                    'sized': sizes,
                    'signal_removed': signal_removed,
                },
                'max_regret': round(best_delta, 3),  # positive = better alternative existed
                'worst_alternative': round(worst_delta, 3),
                'actual_was_best': best_delta <= 0,
            }

            with _LOCK:
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
                _check_trigger()

            print(f"{_LOG_PREFIX} {coin} {side} actual={pnl_pct:+.2f}% "
                  f"max_regret={best_delta:+.2f}% "
                  f"best_was={'actual' if best_delta <= 0 else 'alternative'}",
                  flush=True)
        except Exception as e:
            print(f"{_LOG_PREFIX} err {coin}: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def _check_trigger():
    global _TRIGGER_FIRED
    if _TRIGGER_FIRED: return
    try:
        if not os.path.exists(LOG_PATH): return
        with open(LOG_PATH) as f:
            n = sum(1 for _ in f)
        if n >= TRIGGER_THRESHOLD:
            _TRIGGER_FIRED = True
            print(f"{_LOG_PREFIX} ★★★ TRIGGER: {n} counterfactual analyses. "
                  f"Ready for alternative-decision evaluation. ★★★", flush=True)
    except Exception:
        pass


def get_stats():
    if not os.path.exists(LOG_PATH):
        return {'total': 0, 'trigger_threshold': TRIGGER_THRESHOLD, 'trigger_fired': False}

    stats = {
        'total': 0,
        'actual_was_best_count': 0,
        'avg_regret': 0,
        'delay_1_better_count': 0,
        'delay_3_better_count': 0,
        'skip_would_have_saved_count': 0,
        'size_2x_better_count': 0,
        'by_regime': defaultdict(lambda: {'n':0,'avg_regret':0,'actual_best':0}),
    }
    total_regret = 0
    regime_regret = defaultdict(float)
    regime_n = defaultdict(int)
    regime_actual_best = defaultdict(int)

    try:
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                stats['total'] += 1
                total_regret += r.get('max_regret', 0)
                if r.get('actual_was_best'):
                    stats['actual_was_best_count'] += 1
                cf = r.get('counterfactuals', {})
                if cf.get('delayed', {}).get('delay_1', {}).get('delta_vs_actual', 0) > 0:
                    stats['delay_1_better_count'] += 1
                if cf.get('delayed', {}).get('delay_3', {}).get('delta_vs_actual', 0) > 0:
                    stats['delay_3_better_count'] += 1
                if cf.get('skipped', {}).get('avoided_loss'):
                    stats['skip_would_have_saved_count'] += 1
                if cf.get('sized', {}).get('size_2.0x', {}).get('delta_vs_actual', 0) > 0:
                    stats['size_2x_better_count'] += 1
                reg = r.get('regime') or 'unknown'
                regime_regret[reg] += r.get('max_regret', 0)
                regime_n[reg] += 1
                if r.get('actual_was_best'):
                    regime_actual_best[reg] += 1
    except Exception as e:
        return {'error': str(e)}

    if stats['total'] > 0:
        stats['avg_regret'] = round(total_regret / stats['total'], 3)
        stats['actual_was_best_pct'] = round(stats['actual_was_best_count'] / stats['total'], 3)
        stats['delay_1_better_pct'] = round(stats['delay_1_better_count'] / stats['total'], 3)
        stats['delay_3_better_pct'] = round(stats['delay_3_better_count'] / stats['total'], 3)
        stats['skip_better_pct'] = round(stats['skip_would_have_saved_count'] / stats['total'], 3)
        stats['size_2x_better_pct'] = round(stats['size_2x_better_count'] / stats['total'], 3)

    for r in regime_n:
        stats['by_regime'][r] = {
            'n': regime_n[r],
            'avg_regret': round(regime_regret[r] / regime_n[r], 3),
            'actual_was_best_pct': round(regime_actual_best[r] / regime_n[r], 3),
        }
    stats['by_regime'] = dict(stats['by_regime'])
    stats['trigger_threshold'] = TRIGGER_THRESHOLD
    stats['trigger_fired'] = _TRIGGER_FIRED
    return stats


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
