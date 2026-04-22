"""Flask endpoint registration.

Usage from precog.py (after `app = Flask(__name__)`):

    from postmortem.endpoints import register_endpoints
    register_endpoints(app)

All endpoints are read-only EXCEPT /postmortem/reset/<coin> and
/postmortem/veto/<coin>/<component> which require the WEBHOOK_SECRET
header for safety.
"""
import os
import time
from flask import jsonify, request

from . import db, bounds, params_api, runner, tuner


def _auth_ok(req):
    secret = os.environ.get('WEBHOOK_SECRET')
    if not secret:
        return True  # no secret configured → dev mode
    return req.headers.get('X-Webhook-Secret') == secret


def register_endpoints(app):
    """Register all /postmortem/* routes. Idempotent if called twice."""

    @app.route('/postmortem/status', methods=['GET'])
    def pm_status():
        try:
            db.init_db()
            recent = db.list_log(limit=10)
            return jsonify({
                'ok': True,
                'enabled': runner.ENABLED,
                'dry_run': getattr(tuner, 'DRY_RUN', os.environ.get('POSTMORTEM_DRY_RUN', '0') == '1'),
                'recent_runs': recent,
                'total_components': len(bounds.components_list()),
                'has_api_key': bool(os.environ.get('ANTHROPIC_API_KEY')),
            })
        except Exception as e:
            return jsonify({'ok': False, 'err': str(e)}), 500

    @app.route('/postmortem/params', methods=['GET'])
    def pm_params():
        coin = request.args.get('coin')
        return jsonify({'params': params_api.params_summary(coin)})

    @app.route('/postmortem/vetos', methods=['GET'])
    def pm_vetos():
        active_only = request.args.get('all') != '1'
        return jsonify({'vetos': db.list_vetos(active_only=active_only)})

    @app.route('/postmortem/log', methods=['GET'])
    def pm_log():
        limit = min(int(request.args.get('limit', 50)), 500)
        return jsonify({'log': db.list_log(limit=limit)})

    @app.route('/postmortem/findings/<int:log_id>', methods=['GET'])
    def pm_findings(log_id):
        return jsonify({'log_id': log_id, 'findings': db.list_findings(log_id)})

    @app.route('/postmortem/history', methods=['GET'])
    def pm_history():
        coin = request.args.get('coin')
        component = request.args.get('component')
        limit = min(int(request.args.get('limit', 100)), 500)
        return jsonify({'history': db.list_history(coin=coin, component=component, limit=limit)})

    @app.route('/postmortem/bounds', methods=['GET'])
    def pm_bounds():
        out = {}
        for comp in bounds.components_list():
            out[comp] = {p: bounds.get_bounds(comp, p) for p in bounds.params_for(comp)}
        return jsonify({'bounds': out})

    @app.route('/postmortem/reset/<coin>', methods=['POST'])
    def pm_reset(coin):
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        try:
            db.reset_coin_params(coin)
            params_api.invalidate(coin=coin)
            return jsonify({'ok': True, 'coin': coin, 'reset_at': time.time()})
        except Exception as e:
            return jsonify({'ok': False, 'err': str(e)}), 500

    @app.route('/postmortem/veto/<coin>/<component>', methods=['POST'])
    def pm_veto(coin, component):
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        data = request.get_json(silent=True) or {}
        reason = data.get('reason', 'manual veto via API')
        expires_in = data.get('expires_in_sec')
        try:
            db.set_veto(coin, component, reason, expires_in_sec=expires_in)
            params_api.invalidate(coin=coin)
            return jsonify({'ok': True, 'coin': coin, 'component': component})
        except Exception as e:
            return jsonify({'ok': False, 'err': str(e)}), 500

    @app.route('/postmortem/veto/<coin>/<component>/clear', methods=['POST'])
    def pm_veto_clear(coin, component):
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        try:
            db.clear_veto(coin, component)
            params_api.invalidate(coin=coin)
            return jsonify({'ok': True, 'coin': coin, 'component': component})
        except Exception as e:
            return jsonify({'ok': False, 'err': str(e)}), 500

    # Ping route for smoke test
    @app.route('/postmortem/ping', methods=['GET'])
    def pm_ping():
        return jsonify({'ok': True, 'ts': time.time(), 'module': 'postmortem'})

    # ─────────────────────────────────────────────────────
    # Knowledge base endpoints
    # ─────────────────────────────────────────────────────
    from . import kb, entry_gate

    @app.route('/postmortem/kb', methods=['GET'])
    def pm_kb():
        coin = request.args.get('coin')
        side = request.args.get('side')
        limit = min(int(request.args.get('limit', 100)), 500)
        return jsonify({'entries': kb.list_entries(coin=coin, side=side, limit=limit)})

    @app.route('/postmortem/kb/<int:entry_id>', methods=['DELETE'])
    def pm_kb_delete(entry_id):
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        return jsonify({'ok': kb.delete_entry(entry_id)})

    @app.route('/postmortem/kb/reset/<coin>', methods=['POST'])
    def pm_kb_reset(coin):
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        return jsonify({'ok': kb.reset_coin(coin)})

    @app.route('/postmortem/gate/test', methods=['POST'])
    def pm_gate_test():
        """Diagnostic: run the entry gate with a supplied signal_ctx, don't trade.
        Useful for verifying gate logic before enabling in process()."""
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        d = request.get_json(silent=True) or {}
        coin = d.get('coin'); side = d.get('side')
        if not coin or side not in ('BUY', 'SELL'):
            return jsonify({'ok': False, 'err': 'coin+side required'}), 400
        verdict = entry_gate.evaluate_entry(coin, side, d.get('signal_ctx') or {})
        return jsonify({'ok': True, 'coin': coin, 'side': side, 'verdict': verdict})

    @app.route('/postmortem/gate/clear-cache', methods=['POST'])
    def pm_gate_clear():
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        coin = (request.get_json(silent=True) or {}).get('coin')
        entry_gate.clear_cache(coin)
        return jsonify({'ok': True, 'coin': coin or 'all'})

    # ─────────────────────────────────────────────────────
    # Market context endpoints (news / macro / calendar)
    # ─────────────────────────────────────────────────────
    from . import news, macro, calendar as cal_mod, context as ctx_mod, trade_finder

    @app.route('/postmortem/news', methods=['GET'])
    def pm_news():
        coin = request.args.get('coin')
        if coin:
            items = news.recent_for_coin(coin,
                                         window_sec=int(request.args.get('window', 3600)),
                                         max_items=int(request.args.get('max', 10)))
        else:
            items = news.fetch_all()
        return jsonify({'ok': True, 'count': len(items), 'items': items[:50]})

    @app.route('/postmortem/macro', methods=['GET'])
    def pm_macro():
        return jsonify({'ok': True, 'snapshot': macro.fetch_all()})

    @app.route('/postmortem/calendar', methods=['GET'])
    def pm_calendar():
        window = int(request.args.get('window', 7200))
        impact = request.args.get('impact', 'high')
        cur = request.args.get('currencies')
        cur_list = cur.split(',') if cur else None
        return jsonify({
            'ok': True,
            'events': cal_mod.upcoming(window_sec=window, impact_min=impact, currencies=cur_list)
        })

    @app.route('/postmortem/context', methods=['GET'])
    def pm_context():
        coin = request.args.get('coin')
        if coin:
            return jsonify({'ok': True, 'coin': coin, 'context': ctx_mod.for_coin(coin)})
        return jsonify({'ok': True, 'global': ctx_mod.global_context(), 'health': ctx_mod.health()})

    @app.route('/postmortem/context/refresh', methods=['POST'])
    def pm_context_refresh():
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        ctx_mod.invalidate()
        return jsonify({'ok': True, 'refreshed_at': time.time()})

    # ─────────────────────────────────────────────────────
    # Independent trade finder
    # ─────────────────────────────────────────────────────
    @app.route('/postmortem/finder/scan', methods=['GET', 'POST'])
    def pm_finder_scan():
        coins = None
        if request.method == 'POST':
            d = request.get_json(silent=True) or {}
            coins = d.get('coins')
        result = trade_finder.scan(coins=coins)
        return jsonify(result)

    @app.route('/postmortem/finder/fire', methods=['POST'])
    def pm_finder_fire():
        """Fire a specific proposal through the webhook pipeline. Requires auth."""
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        d = request.get_json(silent=True) or {}
        return jsonify(trade_finder.fire_proposal(d))

    @app.route('/postmortem/finder/status', methods=['GET'])
    def pm_finder_status():
        return jsonify({'ok': True, 'status': trade_finder.status()})

    @app.route('/postmortem/finder/auto/start', methods=['POST'])
    def pm_finder_auto_start():
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        started = trade_finder.start_auto()
        return jsonify({'ok': True, 'started': started})

    @app.route('/postmortem/finder/auto/stop', methods=['POST'])
    def pm_finder_auto_stop():
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        trade_finder.stop_auto()
        return jsonify({'ok': True, 'stopped': True})

    # ─────────────────────────────────────────────────────
    # TradingView macro webhook cache (DXY/SPX/VIX/GOLD/OIL/etc)
    # User configures TV alerts → POST here with auth.
    # ─────────────────────────────────────────────────────
    from . import tv_cache

    @app.route('/postmortem/tv/macro', methods=['POST'])
    def pm_tv_macro():
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        # Accept both JSON and plain-text payloads (TV alert messages can be either)
        d = request.get_json(silent=True)
        if d is None:
            # Fall back to parsing JSON out of plain-text body
            try:
                raw = request.get_data(as_text=True) or ''
                # Strip TradingView's literal wrapper if present
                raw = raw.strip().strip('`').strip()
                import json as _json
                d = _json.loads(raw) if raw.startswith('{') else {}
            except Exception:
                d = {}
        sym = d.get('symbol') or d.get('ticker')
        price = d.get('price') or d.get('close') or d.get('last')
        if sym is None or price is None:
            return jsonify({'ok': False, 'err': 'symbol and price required', 'got': d}), 400
        written = tv_cache.write(
            symbol=sym,
            price=price,
            prev_close=d.get('prev_close') or d.get('open'),
            timeframe=d.get('timeframe') or d.get('interval'),
            raw=d,
        )
        if not written:
            return jsonify({'ok': False, 'err': 'symbol not allowed', 'symbol': sym}), 400
        return jsonify({'ok': True, 'symbol': written, 'price': price})

    @app.route('/postmortem/tv/macro', methods=['GET'])
    def pm_tv_macro_list():
        return jsonify({'entries': tv_cache.list_all()})

    @app.route('/postmortem/tv/macro/<symbol>', methods=['GET'])
    def pm_tv_macro_one(symbol):
        return jsonify({'entry': tv_cache.read(symbol)})

    # ─────────────────────────────────────────────────────
    # Auto macro puller (Stooq + Massive.io) — zero user config
    # ─────────────────────────────────────────────────────
    from . import auto_macro

    @app.route('/postmortem/automacro/pull', methods=['POST'])
    def pm_automacro_pull():
        """Force-refresh auto-pulled macro (Stooq + Massive.io) into tv_cache."""
        if not _auth_ok(request):
            return jsonify({'ok': False, 'err': 'unauthorized'}), 401
        summary = auto_macro.pull_all(force=True)
        return jsonify(summary)

    @app.route('/postmortem/automacro/status', methods=['GET'])
    def pm_automacro_status():
        return jsonify({
            'daemon_alive': auto_macro.daemon_alive(),
            'stooq_symbols': list(auto_macro.STOOQ_SYMBOLS.values()),
            'massive_enabled': bool(os.environ.get('MASSIVE_API_KEY') or os.environ.get('MASSIVE_IO_API_KEY')),
            'massive_symbols': list(auto_macro.MASSIVE_SYMBOLS.values()) if os.environ.get('MASSIVE_API_KEY') or os.environ.get('MASSIVE_IO_API_KEY') else [],
            'ttl_sec': auto_macro.TTL,
        })

    return app
