"""
smc_app.py — Flask routes + boot wiring for SMC v1.0.

Reuses precog-hl/main:
  - hl_user_ws.init(info_unused, wallet)        — singleton WS
  - position_ledger                              — state machine + on_fill/on_webdata2
  - atomic_reconciler                            — daemon for SL/TP size reconciliation
  - flight_guard                                 — write spacer

Adds SMC layer:
  - smc_fill_hook.install()                      — wraps position_ledger.on_fill
  - smc_monitors.start()                         — schedules

Procfile:
  web: gunicorn smc_app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT
"""
import os
import time
import logging

from flask import Flask, request, jsonify

import smc_trade_log
import smc_skip_log
import smc_daily_rollup
import smc_pl_compat
import position_ledger
import hl_user_ws
import smc_fill_hook
import smc_monitors
import smc_state
from smc_config import SMC_CONFIG
from smc_engine import handle_smc_alert, WEBHOOK_SECRET
from smc_state import state

log = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO'),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)

app = Flask(__name__)


# ---------------- Boot ----------------

def _boot():
    """Wire WS + reconciler + scheduler. Idempotent — safe under gunicorn."""
    if getattr(app, '_smc_booted', False):
        return
    app._smc_booted = True

    # Force ALO TIF for SMC (maker-only spec)
    os.environ.setdefault('ENTRY_TIF', 'Alo')

    smc_state.load()

    # 1. Install fill hook BEFORE WS starts so first fills are captured
    smc_fill_hook.install()

    # 2. Start hl_user_ws (creates own Info instance, subscribes 3 channels)
    wallet = os.environ.get('HL_ADDRESS', '')
    if wallet:
        try:
            hl_user_ws.init(None, wallet)
            log.info(f"hl_user_ws started for wallet {wallet}")
        except Exception as e:
            log.exception(f"hl_user_ws.init failed: {e}")
    else:
        log.warning("HL_ADDRESS not set; WS disabled")

    # 3. Start atomic_reconciler daemon (handles SL/TP size on partial fills)
    try:
        import atomic_reconciler
        import smc_execution
        atomic_reconciler.init(
            cancel_order_fn=smc_execution.reconciler_cancel,
            place_sl_fn=smc_execution.reconciler_place_sl,
            place_tp_fn=smc_execution.reconciler_place_tp,
            emergency_close_fn=smc_execution.reconciler_emergency_close,
            log_fn=lambda m: log.info(f"reconciler: {m}"),
        )
        atomic_reconciler.start()
        log.info("atomic_reconciler started")
    except ImportError:
        log.warning("atomic_reconciler not present; skipping")
    except Exception as e:
        log.exception(f"atomic_reconciler init/start failed: {e}")

    # 4. Start SMC scheduler (15min position_tick + hourly + daily)
    smc_monitors.start()

    # 5. Native SMC engine — bypass Pine, generate signals from HL WS candles
    if os.environ.get('SMC_NATIVE', '0') == '1':
        try:
            import smc_native_runner
            smc_native_runner.init_native(
                on_setup_callback=handle_smc_alert,
                on_log=lambda m: log.info(f"native: {m}"),
            )
            log.info("SMC native runner initialised")
        except Exception as e:
            log.exception(f"smc_native_runner init failed: {e}")

    log.info("SMC v1.0 boot complete")


@app.before_request
def _before():
    _boot()


# ---------------- Helpers ----------------

def _smc_position_count():
    return sum(
        1 for p in state.positions.values()
        if p.get('trade_id', '').startswith('smc-')
    )


def _ws_fresh():
    return smc_pl_compat.ws_is_fresh()


# ---------------- Routes ----------------

SA_BASE = os.environ.get('SA_BASE', 'https://trading-signals-aggn.onrender.com')

@app.route('/', methods=['GET'])
def landing():
    """Serve the precog-hl landing page from this repo."""
    try:
        with open('landing.html', 'r') as f:
            return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}
    except FileNotFoundError:
        return jsonify({'service': 'SMC v1.0', 'note': 'landing.html missing'}), 200


@app.route('/stats', methods=['GET'])
def stats_proxy():
    """Proxy SA's /stats so the landing's fetch(`${origin}/stats`) works."""
    try:
        import requests as _req
        r = _req.get(f"{SA_BASE}/stats", timeout=5)
        return r.content, r.status_code, dict(r.headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/trades', methods=['GET'])
def trades_proxy():
    """Proxy SA's /trades; fall back to SMC trade log if SA missing."""
    try:
        import requests as _req
        r = _req.get(f"{SA_BASE}/trades", timeout=5)
        if r.status_code == 200:
            return r.content, 200, {'Content-Type': r.headers.get('Content-Type', 'application/json')}
    except Exception:
        pass
    return jsonify(smc_trade_log.tail(50))


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'ok': True,
        'ws_fresh': _ws_fresh(),
    })


# ---------------- Landing page compatibility shims ----------------
# The landing page hits old PRECOG endpoints (/dash, /engines, /signals,
# /news, /whales, /orderbook/BTC, /audit/deep). These don't exist in SMC.
# We stub them to return 200 with empty/SMC-equivalent data so the page
# renders without console-error storms.

def _smc_session_name():
    """Map current UTC hour to session label."""
    h = __import__('datetime').datetime.utcnow().hour
    if 0 <= h < 5:    return 'asian-skip'
    if 5 <= h < 8:    return 'asian-late'
    if 8 <= h < 13:   return 'london'
    if 13 <= h < 17:  return 'overlap'
    if 17 <= h < 22:  return 'ny'
    return 'after-hours'


def _smc_positions_for_dash():
    """Shape SMC + orphan positions for /dash."""
    out = []
    for coin, p in state.positions.items():
        is_smc = p.get('trade_id', '').startswith('smc-')
        mark = smc_pl_compat.get_mark_price(coin) or p.get('fill_price') or 0
        entry = p.get('fill_price') or p.get('ob_top') or 0
        size = p.get('fill_size') or p.get('size') or 0
        upnl = (mark - entry) * size if entry and size else 0
        out.append({
            'coin': coin,
            'side': 'LONG' if p.get('side') == 'BUY' else (p.get('side') or 'LONG'),
            'entry': entry,
            'mark': mark,
            'size': size,
            'upnl': upnl,
            'lev': '10x',
            'tp': p.get('tp2') or p.get('tp1'),
            'sl': p.get('sl_current') or p.get('sl_orig') or p.get('sl_price'),
            'engine': 'SMC' if is_smc else 'ORPHAN',
            'tp_pct': None,
            'sl_pct': None,
        })
    return out


@app.route('/dash', methods=['GET'])
def dash_compat():
    return jsonify({
        'version': 'smc-1.0',
        'live_trading': bool(int(os.environ.get('LIVE_TRADING', '0'))),
        'equity': smc_pl_compat.get_equity(),
        'positions': _smc_positions_for_dash(),
        'armed': len(state.armed),
        'halt': state.halt_flag,
        'btc_trend_up': state.btc_trend_up,
        'universe_size': len(state.universe),
        'session': {'name': _smc_session_name()},
        'orderbook': {'verified_coins': len(state.universe)},
        'whale': {'total_whales': 0},
        'funding_cached': len(state.funding_cache),
        'risk_ladder': {'risk': 0.10},
    })


@app.route('/engines', methods=['GET'])
def engines_compat():
    """Shape engine state for the landing's grid renderer."""
    smc_live = bool(int(os.environ.get('LIVE_TRADING', '0')))
    ws_fresh = _ws_fresh()
    return jsonify({
        'signal_engines': {
            'SMC_v1':       smc_live,
            'pine_webhook': True,
            'btc_trend':    state.btc_trend_up is not None,
            'funding':      len(state.funding_cache) > 0,
        },
        'guards': {
            'webhook_secret': True,
            'short_halt':     True,
            'rr_min':         True,
            'session_filter': True,
            'flight_guard':   True,
            'atomic_entry':   True,
            'reconciler':     True,
            'halt_flag':      not state.halt_flag,
        },
        'venues': {
            'HYPERLIQUID': ws_fresh,
            'OKX':         True,
            'BINANCE':     False,
            'BYBIT':       False,
            'COINBASE':    False,
            'BITGET':      False,
            'KRAKEN':      False,
        },
        'sizing': {
            'fixed_50_usd':    True,
            'long_only':       True,
            'max_20_pos':      True,
            'maker_only_alo':  True,
        },
        'venue_ages': {
            'hl': 0 if ws_fresh else 999,
            'okx': 0,
            'by': 999, 'bn': 999, 'cb': 999, 'bg': 999, 'kr': 999,
        },
    })


@app.route('/signals', methods=['GET'])
def signals_compat():
    """Recent SMC trade events as signal feed: ARMED/FILLED/CLOSED."""
    rows = smc_trade_log.tail(50)
    items = []
    KIND_MAP = {
        'ARMED':         'OPEN',
        'FILLED':        'OPEN',
        'CLOSED_TP':     'CLOSED',
        'CLOSED_SL':     'CLOSED',
        'CLOSED_BE':     'CLOSED',
        'CLOSED_MARKET': 'CLOSED',
        'GATE_PASS':     'PASS',
        'GATE_FAIL':     'SKIP',
        'REJECTED':      'REJECTED',
    }
    for r in reversed(rows):
        ev = r.get('event', '')
        kind = KIND_MAP.get(ev)
        if not kind:
            continue
        ts_ms = r.get('event_ts_ms')
        try:
            ts_str = __import__('datetime').datetime.utcfromtimestamp(int(ts_ms) / 1000).strftime('%H:%M:%S')
        except Exception:
            ts_str = ''
        items.append({
            'coin': r.get('coin') or '—',
            'kind': kind,
            'side': r.get('side') or '',
            'ts':   ts_str,
            'event': ev,
            'reason': r.get('reason') or r.get('gate_reason') or '',
        })
        if len(items) >= 20:
            break
    return jsonify({'items': items})


@app.route('/news', methods=['GET'])
def news_compat():
    return jsonify({'items': []})


@app.route('/whales', methods=['GET'])
def whales_compat():
    return jsonify({'items': []})


@app.route('/orderbook/<coin>', methods=['GET'])
def orderbook_compat(coin):
    """Lightweight orderbook stub — landing degrades gracefully when mid is null."""
    mark = smc_pl_compat.get_mark_price(coin)
    return jsonify({
        'coin': coin,
        'mid': mark,
        'bids': [],
        'asks': [],
        'note': 'SMC does not track orderbooks',
    })


def _aggregate_smc_window(hours: int = 12):
    """Compute landing-panel-shaped aggregates from SMC trade log over `hours`."""
    cutoff_ms = int(time.time() * 1000) - hours * 3600 * 1000
    rows = smc_trade_log.tail(5000)
    rows_window = [r for r in rows if int(r.get('event_ts_ms', 0) or 0) >= cutoff_ms]

    CLOSE_EVENTS = {'CLOSED_TP', 'CLOSED_SL', 'CLOSED_BE', 'CLOSED_MARKET'}
    closes = [r for r in rows_window if r.get('event') in CLOSE_EVENTS]

    def _f(v, dflt=0.0):
        try: return float(v)
        except (ValueError, TypeError): return dflt

    wins = [c for c in closes if _f(c.get('pnl_r')) > 0]
    losses = [c for c in closes if _f(c.get('pnl_r')) < 0]
    breakevens = [c for c in closes if _f(c.get('pnl_r')) == 0]

    total_pnl = sum(_f(c.get('pnl_usd')) for c in closes)
    avg_win = (sum(_f(c.get('pnl_usd')) for c in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(_f(c.get('pnl_usd')) for c in losses) / len(losses)) if losses else 0.0
    closed_count = len(closes)
    wr_pct = (len(wins) / closed_count * 100) if closed_count else 0.0

    # Per-engine breakdown — for SMC this is just one engine
    by_engine = {
        'SMC_v1': {
            'n': closed_count,
            'w': len(wins),
            'l': len(losses),
            'b': len(breakevens),
            'wr_pct': round(wr_pct, 1),
            'pnl_usd': round(total_pnl, 2),
        },
    }

    # Counters from event types in window
    alerts_recv = sum(1 for r in rows_window if r.get('event') == 'ALERT_RECV')
    armed_count = sum(1 for r in rows_window if r.get('event') == 'ARMED')
    rejected = sum(1 for r in rows_window if r.get('event') == 'REJECTED')
    filled = sum(1 for r in rows_window if r.get('event') == 'FILLED')
    expired = sum(1 for r in rows_window if r.get('event') == 'EXPIRED')

    # Gate-fail breakdown for rejects display
    gate_fails = [r for r in rows_window if r.get('event') == 'GATE_FAIL']
    rejects = {}
    for r in gate_fails:
        reason = r.get('gate_reason') or 'unknown'
        rejects[reason] = rejects.get(reason, 0) + 1

    return {
        'total_pnl_usd': round(total_pnl, 2),
        'wr_pct_window': round(wr_pct, 1),
        'wins_window': len(wins),
        'losses_window': len(losses),
        'breakevens_window': len(breakevens),
        'closed_count': closed_count,
        'avg_win_usd': round(avg_win, 4),
        'avg_loss_usd': round(avg_loss, 4),
        'by_engine': by_engine,
        'alerts_recv_window': alerts_recv,
        'armed_window': armed_count,
        'rejected_window': rejected,
        'filled_window': filled,
        'expired_window': expired,
        'rejects_last_scan': rejects,
    }


@app.route('/precog_status', methods=['GET'])
def precog_status():
    """Shape SMC + SA data for the landing's SA panel (renderSystemA)."""
    hours = int(request.args.get('hours', 12))
    agg = _aggregate_smc_window(hours)
    return jsonify({
        **agg,
        'enabled': bool(int(os.environ.get('LIVE_TRADING', '0'))),
        'disabled_engines': '',
        'engine_auto_pause': {},
        'risk_pct': SMC_CONFIG['force_notional_usd'] / max(smc_pl_compat.get_equity() or 525, 1),
    })


@app.route('/confluence', methods=['GET'])
def confluence():
    """Shape SMC data for the landing's SMC panel (renderSystemB, was confluence)."""
    hours = int(request.args.get('hours', 12))
    agg = _aggregate_smc_window(hours)

    # Open positions in confluence panel shape
    open_positions = {}
    for coin, p in state.positions.items():
        if not p.get('trade_id', '').startswith('smc-'):
            continue
        risk = abs((p.get('fill_price') or 0) - (p.get('sl_orig') or 0))
        entry = p.get('fill_price') or p.get('ob_top') or 0
        tp_pct = ((p.get('tp2') or 0) - entry) / entry * 100 if entry else 0
        sl_pct = ((p.get('sl_orig') or 0) - entry) / entry * 100 if entry else 0
        open_positions[coin] = {
            'side': p.get('side') or 'BUY',
            'entry': entry,
            'systems': ['SMC'],
            'tp_pct': round(tp_pct, 2),
            'sl_pct': round(sl_pct, 2),
            'sl_at_be': bool(p.get('be_done')),
            'mfe_pct': p.get('mfe_pct'),
            'mae_pct': p.get('mae_pct'),
            'ts': p.get('fill_time_ms'),
        }

    return jsonify({
        **agg,
        'dry_run': not bool(int(os.environ.get('LIVE_TRADING', '0'))),
        'enabled': not state.halt_flag,
        'open_positions': open_positions,
        'max_positions': SMC_CONFIG['max_concurrent_positions'],
        'engine_stats': {'signals_yielded': agg['armed_window']},
        'last_scan_signals': agg['alerts_recv_window'],
        'last_scan_fires': agg['armed_window'],
        'total_fires': agg['armed_window'],
        'place_attempts': agg['armed_window'],
        'place_filled': agg['filled_window'],
        'place_no_fill': agg['expired_window'],
        'place_error': agg['rejected_window'],
        'killed_coins': [],
    })


@app.route('/audit/deep', methods=['GET'])
def audit_compat():
    """Aggregate closed trades per coin for the heatmap."""
    rows = smc_trade_log.tail(2000)
    per_coin = {}
    CLOSE_EVENTS = {'CLOSED_TP', 'CLOSED_SL', 'CLOSED_BE', 'CLOSED_MARKET'}
    for r in rows:
        if r.get('event') not in CLOSE_EVENTS:
            continue
        coin = r.get('coin')
        if not coin:
            continue
        try:
            pnl_r = float(r.get('pnl_r') or 0)
        except (ValueError, TypeError):
            pnl_r = 0
        try:
            pnl_usd = float(r.get('pnl_usd') or 0)
        except (ValueError, TypeError):
            pnl_usd = 0
        c = per_coin.setdefault(coin, {'coin': coin, 'n': 0, 'w': 0, 'l': 0, 'pnl': 0.0})
        c['n'] += 1
        if pnl_r > 0:   c['w'] += 1
        elif pnl_r < 0: c['l'] += 1
        c['pnl'] += pnl_usd
    return jsonify({'per_coin': list(per_coin.values()), 'rows': len(rows)})


@app.route('/smc/alert', methods=['POST'])
def smc_alert():
    payload = request.get_json(force=True, silent=True) or {}
    body, status = handle_smc_alert(payload)
    return jsonify(body), status


@app.route('/smc/status', methods=['GET'])
def status():
    smc_pos_count = _smc_position_count()
    orphans = [
        c for c, p in state.positions.items()
        if not p.get('trade_id', '').startswith('smc-')
    ]
    age_min = (
        (time.time() - state.btc_trend_updated_ms / 1000) / 60
        if state.btc_trend_updated_ms else None
    )
    equity = smc_pl_compat.get_equity()

    return jsonify({
        'version': 'smc-1.0',
        'live_trading': bool(int(os.environ.get('LIVE_TRADING', '0'))),
        'long_only': bool(int(os.environ.get('LONG_ONLY', '1'))),
        'halt_flag': state.halt_flag,
        'halt_reason': state.halt_reason,
        'armed_count': len(state.armed),
        'positions_count': smc_pos_count,
        'orphan_positions': orphans,
        'btc_trend_up': state.btc_trend_up,
        'btc_trend_age_min': age_min,
        'universe_size': len(state.universe),
        'ws_fresh': _ws_fresh(),
        'last_alert_ms': state.last_alert_ms,
        'equity': equity,
    })


@app.route('/smc/positions', methods=['GET'])
def positions():
    smc_pos = {
        c: p for c, p in state.positions.items()
        if p.get('trade_id', '').startswith('smc-')
    }
    return jsonify(smc_pos)


@app.route('/smc/armed', methods=['GET'])
def armed():
    return jsonify(state.armed)


@app.route('/smc/trades', methods=['GET'])
def trades():
    n = int(request.args.get('n', 100))
    return jsonify(smc_trade_log.tail(n))


@app.route('/smc/skips', methods=['GET'])
def skips():
    n = int(request.args.get('n', 100))
    return jsonify({
        'tail': smc_skip_log.tail(n),
        'gate_breakdown_24h': smc_skip_log.gate_breakdown(
            since_ms=int(time.time() * 1000) - 86_400_000
        ),
        'coin_breakdown_24h': smc_skip_log.coin_skip_breakdown(
            since_ms=int(time.time() * 1000) - 86_400_000
        ),
    })


@app.route('/smc/daily', methods=['GET'])
def daily():
    n = int(request.args.get('n', 30))
    return jsonify(smc_daily_rollup.tail(n))


@app.route('/smc/weekly', methods=['GET'])
def weekly():
    weeks = int(request.args.get('weeks', 4))
    return jsonify(smc_daily_rollup.weekly_summary(weeks))


@app.route('/smc/halt', methods=['POST', 'GET'])
def halt():
    if request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({'status': 'unauthorized', 'usage': 'POST /smc/halt?secret=...'}), 401
    state.halt_flag = True
    state.halt_reason = 'manual'
    smc_state.persist()
    smc_trade_log.log_system('HALT_TRIGGERED', reason='manual')
    return jsonify({'status': 'halted'})


@app.route('/smc/unhalt', methods=['POST', 'GET'])
def unhalt():
    if request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({'status': 'unauthorized', 'usage': 'POST /smc/unhalt?secret=...'}), 401
    state.halt_flag = False
    state.halt_reason = None
    smc_state.persist()
    smc_trade_log.log_system('UNHALT')
    return jsonify({'status': 'unhalted'})


@app.route('/smc/native/status', methods=['GET'])
def smc_native_status():
    """Native engine telemetry."""
    try:
        import smc_native_runner
        runner = smc_native_runner.get_runner()
        if runner:
            return jsonify(runner.status())
        return jsonify({'enabled': False, 'reason': 'not_initialized'})
    except Exception as e:
        return jsonify({'enabled': False, 'error': str(e)}), 500


# Catch-all: any unknown path falls back to landing page (no more 404/405)
@app.route('/<path:_anything>', methods=['GET'])
def _catchall(_anything):
    try:
        with open('landing.html', 'r') as f:
            return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}
    except FileNotFoundError:
        return jsonify({'service': 'SMC v1.0', 'path': _anything}), 200


# ---------------- Local dev ----------------

if __name__ == '__main__':
    _boot()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
