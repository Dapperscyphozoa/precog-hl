"""Reality gap analyzer — post-hoc audit of backtest-to-live drift.

Joins data from existing streams (trades, signal_log, HL state) to compute
correction factors for core assumptions:

A2: entry timing slippage (signal-close vs fill-price)
B4: regime-at-entry vs regime-at-exit (transition drag)
C1: live-vs-OOS WR degradation (backtest overfit tax)
D2: effective-vs-configured leverage (position sizing reality)

Produces an in-memory correction_factors dict that other modules can consult.

No new trading behavior. Telemetry + correction factor exposure only.
Trigger at 50 closes.
"""
import json, os, time, threading
from collections import defaultdict

LOG_PATH = os.environ.get('REALITY_GAP_LOG_PATH', '/app/reality_gap.jsonl')
TRIGGER_THRESHOLD = 50
_LOCK = threading.Lock()
_LOG_PREFIX = '[reality_gap]'
_TRIGGER_FIRED = False

# Correction factors — read by other modules via get_factors()
_CORRECTION_FACTORS = {
    'live_vs_oos_wr_ratio': None,          # None = not yet measured
    'regime_transition_drag_pct': None,     # R lost per transition trade
    'entry_slip_correction_bps': None,      # bps to add to modeled slip
    'effective_leverage_ratio': None,       # actual_lev / configured_lev
    'sample_size': 0,
    'last_update_ts': None,
}


def record_close(coin, engine, regime_at_entry, regime_at_exit,
                 signal_close_price, actual_fill_price, pnl_pct, win,
                 configured_lev, actual_lev, enterprise_oos_wr=None, bar_ts=None):
    """Log a closed trade with all dimensions needed for gap analysis."""
    def _do():
        try:
            rec = {
                'ts': int(time.time()),
                'bar_ts': int(bar_ts) if bar_ts else None,
                'coin': coin,
                'engine': engine,
                'regime_at_entry': regime_at_entry,
                'regime_at_exit': regime_at_exit,
                'regime_transitioned': regime_at_entry != regime_at_exit if regime_at_entry and regime_at_exit else None,
                'signal_close_price': signal_close_price,
                'actual_fill_price': actual_fill_price,
                'entry_slip_bps': ((actual_fill_price - signal_close_price) / signal_close_price * 10000)
                    if (signal_close_price and actual_fill_price) else None,
                'pnl_pct': round(float(pnl_pct), 3),
                'win': bool(win),
                'configured_lev': configured_lev,
                'actual_lev': actual_lev,
                'lev_ratio': (actual_lev / configured_lev) if (configured_lev and actual_lev) else None,
                'enterprise_oos_wr': enterprise_oos_wr,
            }
            with _LOCK:
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
                _check_trigger()
                _recompute_factors()
        except Exception as e:
            print(f"{_LOG_PREFIX} err {coin}: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def _recompute_factors():
    """Aggregate correction factors from log. Called under _LOCK."""
    if not os.path.exists(LOG_PATH): return
    wins = 0
    total = 0
    oos_wr_sum = 0
    oos_wr_count = 0
    transition_wins = 0
    transition_n = 0
    stable_wins = 0
    stable_n = 0
    slip_bps = []
    lev_ratios = []
    try:
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception: continue
                total += 1
                if r.get('win'): wins += 1
                if r.get('enterprise_oos_wr') is not None:
                    oos_wr_sum += r['enterprise_oos_wr']
                    oos_wr_count += 1
                if r.get('regime_transitioned') is True:
                    transition_n += 1
                    if r.get('win'): transition_wins += 1
                elif r.get('regime_transitioned') is False:
                    stable_n += 1
                    if r.get('win'): stable_wins += 1
                if r.get('entry_slip_bps') is not None:
                    slip_bps.append(r['entry_slip_bps'])
                if r.get('lev_ratio') is not None:
                    lev_ratios.append(r['lev_ratio'])
    except Exception:
        return

    if total == 0: return

    live_wr = wins / total if total else 0
    avg_oos_wr = (oos_wr_sum / oos_wr_count) if oos_wr_count else None

    if avg_oos_wr and avg_oos_wr > 0:
        _CORRECTION_FACTORS['live_vs_oos_wr_ratio'] = round(live_wr / avg_oos_wr, 3)
    if transition_n >= 5 and stable_n >= 5:
        transition_wr = transition_wins / transition_n
        stable_wr = stable_wins / stable_n
        _CORRECTION_FACTORS['regime_transition_drag_pct'] = round(stable_wr - transition_wr, 3)
    if slip_bps:
        _CORRECTION_FACTORS['entry_slip_correction_bps'] = round(
            sum(slip_bps) / len(slip_bps), 2)
    if lev_ratios:
        _CORRECTION_FACTORS['effective_leverage_ratio'] = round(
            sum(lev_ratios) / len(lev_ratios), 3)
    _CORRECTION_FACTORS['sample_size'] = total
    _CORRECTION_FACTORS['last_update_ts'] = int(time.time())


def _check_trigger():
    global _TRIGGER_FIRED
    if _TRIGGER_FIRED: return
    try:
        if not os.path.exists(LOG_PATH): return
        with open(LOG_PATH) as f:
            n = sum(1 for _ in f)
        if n >= TRIGGER_THRESHOLD:
            _TRIGGER_FIRED = True
            print(f"{_LOG_PREFIX} ★★★ TRIGGER: {n} closes. "
                  f"Factors: {_CORRECTION_FACTORS} ★★★", flush=True)
    except Exception:
        pass


def get_factors():
    """Return current correction factors. Other modules read this."""
    with _LOCK:
        return dict(_CORRECTION_FACTORS)


def status():
    with _LOCK:
        factors = dict(_CORRECTION_FACTORS)
    n = 0
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH) as f:
                n = sum(1 for _ in f)
        except Exception: pass
    return {
        'correction_factors': factors,
        'trigger_threshold': TRIGGER_THRESHOLD,
        'trigger_fired': _TRIGGER_FIRED,
        'total_records': n,
        'interpretation': {
            'live_vs_oos_wr_ratio': '1.0 = no overfit. <0.85 = backtest overfit. <0.75 = tighten gate.',
            'regime_transition_drag_pct': '0 = no transition penalty. >0.10 = transitions are 10pp+ worse.',
            'entry_slip_correction_bps': '-8 = model was right. >15 = slippage systematically exceeds model.',
            'effective_leverage_ratio': '1.0 = leverage matches config. <0.8 = HL capping below configured.',
        },
    }
