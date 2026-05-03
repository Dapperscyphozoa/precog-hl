"""
smc_monitors.py — Schedules + per-tick management.

position_tick (15min):    MFE/MAE update, BE move at +2.5R, time-stop at 24h<+1R
refresh_btc_trend (1h):   OKX 4h candle 40-bar comparison
refresh_funding_rates(1h): HL meta_and_asset_ctxs
refresh_universe (daily): rebuild SMC universe from HL meta minus excluded majors
nightly_rollup (23:55):   smc_daily_rollup.generate_rollup
heartbeat (5min):         log status line
"""
import os
import time
import logging
import threading
from datetime import datetime, timezone

import schedule

import okx_fetch
import smc_pl_compat
import smc_trade_log
import smc_skip_log
import smc_daily_rollup
from smc_config import SMC_CONFIG
from smc_state import state, persist as state_persist

log = logging.getLogger(__name__)

_scheduler_thread = None
_stop = False


# ---------------- Position tick ----------------

def position_tick():
    from smc_execution import replace_sl, close_market
    for coin, pos in list(state.positions.items()):
        if not pos.get('trade_id', '').startswith('smc-'):
            continue   # ignore orphans (JUP/STABLE)

        mark = smc_pl_compat.get_mark_price(coin)
        if mark is None:
            continue

        risk = abs(pos['fill_price'] - pos['sl_orig'])
        if risk == 0:
            continue

        cur_r = (mark - pos['fill_price']) / risk
        cur_pct = (mark - pos['fill_price']) / pos['fill_price'] * 100

        pos['best_r'] = max(pos.get('best_r', 0.0), cur_r)
        pos['worst_r'] = min(pos.get('worst_r', 0.0), cur_r)
        pos['mfe_pct'] = max(pos.get('mfe_pct', 0.0), cur_pct)
        pos['mae_pct'] = min(pos.get('mae_pct', 0.0), cur_pct)

        # BE move at +2.5R, lock in +0.2R
        if not pos.get('be_done') and cur_r >= SMC_CONFIG['be_trigger_r']:
            new_sl = pos['fill_price'] + SMC_CONFIG['be_buffer_r'] * risk
            try:
                replace_sl(pos, new_sl)
                pos['be_done'] = True
                pos['sl_current'] = new_sl
                pos['be_at_ms'] = int(time.time() * 1000)
                smc_trade_log.log_be_moved(pos, new_sl)
            except Exception as e:
                log.exception(f"BE move failed for {coin}: {e}")

        # Time-stop: 24h, best_r < 1.0
        hours_held = (time.time() - pos['fill_time_ms'] / 1000) / 3600
        if (hours_held >= SMC_CONFIG['time_stop_hours']
                and pos['best_r'] < SMC_CONFIG['time_stop_progress_r']):
            try:
                close_market(pos, reason='TIME_STOP')
            except Exception as e:
                log.exception(f"time_stop close failed for {coin}: {e}")

    try:
        state_persist()
    except Exception:
        pass


# ---------------- Cache refreshes ----------------

def refresh_btc_trend():
    n = SMC_CONFIG['btc_trend_lookback_4h_bars']
    candles = okx_fetch.fetch_klines('BTC', '4h', max(n + 5, 50))
    if len(candles) < n + 1:
        log.warning(f"btc_trend: insufficient candles ({len(candles)})")
        return
    cur = candles[-1]['c']
    old = candles[-(n + 1)]['c']
    state.btc_trend_up = cur > old
    state.btc_trend_updated_ms = int(time.time() * 1000)
    log.info(f"BTC 4h trend: {'UP' if state.btc_trend_up else 'DOWN'} "
             f"({old:.2f} → {cur:.2f})")
    try:
        state_persist()
    except Exception:
        pass


def refresh_funding_rates():
    try:
        from smc_execution import _ensure_hl, _info
        _ensure_hl()
        from smc_execution import _info as info
        if info is None:
            return
        meta_ctx = info.meta_and_asset_ctxs()
        if not isinstance(meta_ctx, list) or len(meta_ctx) < 2:
            return
        ctxs = meta_ctx[1] or []
        meta = meta_ctx[0] or {}
        universe = meta.get('universe', [])
        for idx, ctx in enumerate(ctxs):
            if idx >= len(universe):
                break
            name = universe[idx].get('name')
            if not name:
                continue
            state.funding_cache[name] = {
                'rate_per_hour': float(ctx.get('funding', 0) or 0),
                'updated_ms': int(time.time() * 1000),
            }
    except Exception as e:
        log.exception(f"refresh_funding_rates failed: {e}")


def refresh_universe():
    try:
        from smc_execution import _ensure_hl
        _ensure_hl()
        from smc_execution import _info as info
        if info is None:
            return
        meta = info.meta()
        excluded = set(SMC_CONFIG['excluded_majors'])
        universe = [
            u['name'] for u in meta.get('universe', [])
            if u.get('name') and u['name'] not in excluded
        ]
        state.universe = universe
        log.info(f"Universe loaded: {len(universe)} coins")
    except Exception as e:
        log.exception(f"refresh_universe failed: {e}")


def nightly_rollup():
    try:
        equity = smc_pl_compat.get_equity()
        smc_daily_rollup.generate_rollup(current_equity=equity)
        log.info("Nightly rollup written")
    except Exception as e:
        log.exception(f"nightly_rollup failed: {e}")


def heartbeat():
    log.info(
        "[hb] armed=%d pos=%d eq=%s btc_trend=%s halt=%s ws_fresh=%s",
        len(state.armed),
        sum(1 for p in state.positions.values() if p.get('trade_id', '').startswith('smc-')),
        smc_pl_compat.get_equity(),
        state.btc_trend_up,
        state.halt_flag,
        smc_pl_compat.ws_is_fresh(),
    )


# ---------------- Scheduler ----------------

def _run_scheduler():
    while not _stop:
        try:
            schedule.run_pending()
        except Exception as e:
            log.exception(f"scheduler tick raised: {e}")
        time.sleep(1)


def start():
    global _scheduler_thread, _stop
    _stop = False

    schedule.every(15).minutes.do(position_tick)
    schedule.every().hour.do(refresh_btc_trend)
    schedule.every().hour.do(refresh_funding_rates)
    schedule.every().day.at("00:05").do(refresh_universe)
    schedule.every().day.at("23:55").do(nightly_rollup)
    schedule.every(5).minutes.do(heartbeat)

    # Run once at startup
    refresh_universe()
    refresh_btc_trend()
    refresh_funding_rates()

    _scheduler_thread = threading.Thread(target=_run_scheduler, daemon=True)
    _scheduler_thread.start()
    log.info("smc_monitors scheduler started")


def stop():
    global _stop
    _stop = True
