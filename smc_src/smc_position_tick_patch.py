"""
SMC v1.0 — position_tick MFE/MAE sampling patch.
Replaces the body of smc_monitors.position_tick() so every 15min mark sample
updates per-trade best_r, worst_r, mfe_pct, mae_pct.

Fold this into smc_monitors.py.
"""
import time
import smc_trade_log
from smc_config import SMC_CONFIG


def position_tick(state, position_ledger, replace_sl, close_market):
    """Run every 15min. Updates MFE/MAE, triggers BE move, fires time-stop."""
    for coin, pos in list(state.positions.items()):
        if not pos.get('trade_id', '').startswith('smc-'):
            continue  # ignore orphans (JUP/STABLE)

        mark = position_ledger.get_mark_price(coin)
        if mark is None:
            continue

        risk = abs(pos['fill_price'] - pos['sl_orig'])
        if risk == 0:
            continue

        # Long-only: r is positive when above entry
        cur_r = (mark - pos['fill_price']) / risk
        cur_pct = (mark - pos['fill_price']) / pos['fill_price'] * 100

        # ---- MFE / MAE tracking ----
        pos['best_r'] = max(pos.get('best_r', 0.0), cur_r)
        pos['worst_r'] = min(pos.get('worst_r', 0.0), cur_r)
        pos['mfe_pct'] = max(pos.get('mfe_pct', 0.0), cur_pct)
        pos['mae_pct'] = min(pos.get('mae_pct', 0.0), cur_pct)

        # ---- BE move at +2.5R, lock +0.2R ----
        if not pos.get('be_done') and cur_r >= SMC_CONFIG['be_trigger_r']:
            new_sl = pos['fill_price'] + SMC_CONFIG['be_buffer_r'] * risk
            replace_sl(pos, new_sl)
            pos['be_done'] = True
            pos['sl_current'] = new_sl
            pos['be_at_ms'] = int(time.time() * 1000)
            smc_trade_log.log_be_moved(pos, new_sl)

        # ---- Time-stop: 24h, best_r < 1.0R ----
        hours_held = (time.time() - pos['fill_time_ms'] / 1000) / 3600
        if (hours_held >= SMC_CONFIG['time_stop_hours']
                and pos['best_r'] < SMC_CONFIG['time_stop_progress_r']):
            close_market(pos, reason='TIME_STOP')
