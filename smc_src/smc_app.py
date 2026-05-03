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

@app.route('/dash', methods=['GET'])
def dash_compat():
    return jsonify({
        'version': 'smc-1.0',
        'live_trading': bool(int(os.environ.get('LIVE_TRADING', '0'))),
        'equity': smc_pl_compat.get_equity(),
        'positions': _smc_position_count(),
        'armed': len(state.armed),
        'halt': state.halt_flag,
        'btc_trend_up': state.btc_trend_up,
        'universe_size': len(state.universe),
    })

@app.route('/engines', methods=['GET'])
def engines_compat():
    return jsonify({'engines': [{'name': 'SMC v1.0', 'status': 'live', 'live_trading': bool(int(os.environ.get('LIVE_TRADING', '0')))}]})

@app.route('/signals', methods=['GET'])
def signals_compat():
    return jsonify({'signals': smc_trade_log.tail(20)})

@app.route('/news', methods=['GET'])
def news_compat():
    return jsonify({'news': []})

@app.route('/whales', methods=['GET'])
def whales_compat():
    return jsonify({'whales': []})

@app.route('/orderbook/<coin>', methods=['GET'])
def orderbook_compat(coin):
    return jsonify({'coin': coin, 'orderbook': None, 'note': 'not tracked in SMC'})

@app.route('/audit/deep', methods=['GET'])
def audit_compat():
    return jsonify({'rows': smc_trade_log.tail(int(request.args.get('hours', 24)) * 5), 'fmt': request.args.get('format', 'json')})


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


@app.route('/smc/halt', methods=['POST'])
def halt():
    if request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({'status': 'unauthorized'}), 401
    state.halt_flag = True
    state.halt_reason = 'manual'
    smc_state.persist()
    smc_trade_log.log_system('HALT_TRIGGERED', reason='manual')
    return jsonify({'status': 'halted'})


@app.route('/smc/unhalt', methods=['POST'])
def unhalt():
    if request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({'status': 'unauthorized'}), 401
    state.halt_flag = False
    state.halt_reason = None
    smc_state.persist()
    smc_trade_log.log_system('UNHALT')
    return jsonify({'status': 'unhalted'})


# ---------------- Local dev ----------------

if __name__ == '__main__':
    _boot()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
