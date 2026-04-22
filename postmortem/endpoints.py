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

from . import db, bounds, params_api, runner


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
                'dry_run': runner.DRY_RUN if hasattr(runner, 'DRY_RUN') else False,
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

    return app
