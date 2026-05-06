#!/usr/bin/env python3
"""
dashboard.py — unified status board for all HL trading engines.

Architecture (push model — minimum bugs):
  Each engine calls push_dashboard_state() inside its save_state()
  → POSTs JSON to this dashboard's /push endpoint.
  Dashboard caches per-engine state in memory (also persists to disk for
  restart resilience) and serves a single HTML page with one panel per engine.

Why push (not poll):
  - One direction of dependency. Engines never depend on dashboard being up.
  - Engine attribution is perfect (each pushes its OWN stats).
  - No cloid-prefix gymnastics.
  - One unified data shape across engines.

Endpoints:
  GET   /                serves dashboard HTML
  GET   /api/state       JSON: per-engine state + global aggregates
  GET   /api/account     JSON: HL account info (cached 30s)
  POST  /push            engines push their state here (X-Push-Secret header)
  GET   /healthz         liveness probe
"""
import os, json, time, threading, traceback, urllib.request
from datetime import datetime, timezone
from collections import deque
from flask import Flask, request, jsonify, Response

PORT          = int(os.environ.get('PORT', '10000'))
PUSH_SECRET   = os.environ.get('DASH_PUSH_SECRET', 'change-me')
WALLET        = os.environ.get('HL_ADDRESS', '0x3eDaD0649Db466E6E7B9a0Caa3E5d6ddc71B5ffE')
STATE_PATH    = os.environ.get('DASH_STATE_PATH', '/var/data/dashboard_state.json')
COMMIT        = os.environ.get('RENDER_GIT_COMMIT', 'dev')[:7]

# Engines we expect (defines panel order on the UI)
ENGINES = ['multi-gate', 'smc-v1', 'smc-v2', 'smc-loose', 'lsr', 'brk', 'pool-arch-rev', 'pool-arch-cont']

# ────────────────────────────────────────────────────────────────
# State cache (thread-safe)
# ────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_engine_states = {}     # {engine_name: {ts, payload, ...}}
_account_cache = {'ts': 0, 'data': None}
_account_lock = threading.Lock()
_log = deque(maxlen=200)

def lg(msg):
    line = f'[{datetime.now(timezone.utc).isoformat()}] {msg}'
    print(line, flush=True)
    _log.append(line)

# Load persisted cache on startup so a dashboard restart doesn't blank the UI
def _load_persisted():
    global _engine_states
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH) as f:
                d = json.load(f)
            with _lock:
                _engine_states = d.get('engines', {})
            lg(f'loaded {len(_engine_states)} engine states from disk')
    except Exception as e:
        lg(f'load err: {e}')

def _persist():
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with _lock:
            snapshot = {'engines': dict(_engine_states), 'ts': int(time.time()*1000)}
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(snapshot, f)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        lg(f'persist err: {e}')

# ────────────────────────────────────────────────────────────────
# HL account info refresher (cache 30s)
# ────────────────────────────────────────────────────────────────
def _hl_post(p):
    body = json.dumps(p).encode()
    req = urllib.request.Request('https://api.hyperliquid.xyz/info', data=body,
        headers={'Content-Type':'application/json','User-Agent':'dashboard'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def _refresh_account_loop():
    while True:
        try:
            us = _hl_post({'type':'clearinghouseState', 'user':WALLET})
            allmids = _hl_post({'type':'allMids'})
            ms = us.get('marginSummary', {})
            data = {
                'equity':       float(ms.get('accountValue', 0)),
                'withdrawable': float(us.get('withdrawable', 0)),
                'totalNtlPos':  float(ms.get('totalNtlPos', 0)),
                'btc_px':       float(allmids.get('BTC', 0) or 0),
                'eth_px':       float(allmids.get('ETH', 0) or 0),
                'open_positions_hl': len(us.get('assetPositions', [])),
                'fetched_t':    int(time.time()*1000),
            }
            with _account_lock:
                _account_cache['data'] = data
                _account_cache['ts'] = data['fetched_t']
        except Exception as e:
            lg(f'account refresh err: {e}')
        time.sleep(30)

# ────────────────────────────────────────────────────────────────
# Aggregate / derive metrics
# ────────────────────────────────────────────────────────────────
def _engine_summary(name):
    """Return UI-ready summary for one engine."""
    with _lock:
        s = dict(_engine_states.get(name, {}))
    if not s:
        return {
            'engine': name, 'present': False, 'stale': True, 'live': None,
            'pnl_total': 0, 'wr': None, 'wins': 0, 'losses': 0, 'breakevens': 0,
            'avg_win': 0, 'avg_loss': 0, 'rr': None,
            'closes': 0, 'open_count': 0, 'max_concurrent': None,
            'open_positions': [], 'history_12h': [], 'telemetry': {}, 'updated_t': 0,
            'sizing_mode': None, 'notional_usd': None,
        }
    age_sec = (time.time()*1000 - s.get('ts', 0)) / 1000.0
    stats = s.get('stats_12h', {}) or {}
    open_pos = s.get('open_positions', []) or []
    return {
        'engine':      name,
        'present':     True,
        'stale':       age_sec > 300,         # >5min = stale
        'age_sec':     int(age_sec),
        'live':        s.get('live', False),
        'sizing_mode': s.get('sizing_mode'),
        'notional_usd': s.get('notional_usd'),
        'max_concurrent': s.get('max_concurrent'),
        'pnl_total':   stats.get('pnl_total', 0),
        'wins':        stats.get('wins', 0),
        'losses':      stats.get('losses', 0),
        'breakevens':  stats.get('breakevens', 0),
        'wr':          stats.get('wr'),
        'avg_win':     stats.get('avg_win', 0),
        'avg_loss':    stats.get('avg_loss', 0),
        'rr':          stats.get('rr_blended'),
        'closes':      stats.get('wins', 0) + stats.get('losses', 0) + stats.get('breakevens', 0),
        'open_count':  len(open_pos),
        'open_positions': open_pos,
        'history_12h': s.get('history_12h', []) or [],
        'telemetry':   s.get('telemetry', {}) or {},
        'updated_t':   s.get('ts', 0),
    }

def _global_aggregate():
    summaries = [_engine_summary(e) for e in ENGINES]
    pnl = sum(s['pnl_total'] or 0 for s in summaries)
    closes = sum(s['closes'] for s in summaries)
    open_count = sum(s['open_count'] for s in summaries)
    wins = sum(s['wins'] for s in summaries)
    losses = sum(s['losses'] for s in summaries)
    return {'pnl_total': pnl, 'closes': closes, 'open': open_count,
            'wins': wins, 'losses': losses,
            'wr': (wins / closes * 100) if closes else None}

# ────────────────────────────────────────────────────────────────
# Flask app
# ────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/healthz')
def healthz():
    return jsonify({'ok': True, 'ts': int(time.time()*1000)})

@app.route('/push', methods=['POST'])
def push():
    secret = request.headers.get('X-Push-Secret', '')
    if secret != PUSH_SECRET:
        return jsonify({'err': 'auth'}), 401
    try:
        body = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({'err': 'bad json'}), 400
    if not isinstance(body, dict):
        return jsonify({'err': 'expected object'}), 400
    name = body.get('engine')
    if not name or name not in ENGINES:
        return jsonify({'err': f'unknown engine: {name}'}), 400
    body['ts'] = int(time.time()*1000)   # use server-side time
    with _lock:
        _engine_states[name] = body
    _persist()
    return jsonify({'ok': True, 'engine': name, 'ts': body['ts']})

@app.route('/api/state')
def api_state():
    return jsonify({
        'engines':   [_engine_summary(e) for e in ENGINES],
        'aggregate': _global_aggregate(),
        'ts':        int(time.time()*1000),
        'commit':    COMMIT,
    })

@app.route('/api/account')
def api_account():
    with _account_lock:
        return jsonify(_account_cache)

@app.route('/api/alerts')
def api_alerts():
    try:
        from alerts import get_alert_status
        return jsonify(get_alert_status())
    except Exception as e:
        return jsonify({'err': str(e), 'active_count': 0, 'active': []})

@app.route('/api/risk_check', methods=['GET', 'POST'])
def api_risk_check():
    """Engine-facing risk gate. Engines query this BEFORE firing a setup.

    GET /api/risk_check?coin=BTC&side=LONG&notional=50&sl_pct=0.005
    POST /api/risk_check  body: {"coin":"BTC","side":"LONG","notional":50,"sl_pct":0.005}

    Returns: {can_fire: bool, block_reason: str|null, equity, limits, current, projected}
    """
    try:
        from risk_cap import evaluate as risk_eval
        if request.method == 'POST':
            body = request.get_json(silent=True) or {}
        else:
            body = request.args
        with _lock:
            engines_snapshot = dict(_engine_states)
        with _account_lock:
            account = dict(_account_cache.get('data') or {})
        equity = account.get('equity', 0)
        result = risk_eval(
            equity=equity,
            engine_states=engines_snapshot,
            requested_coin=body.get('coin'),
            requested_side=body.get('side'),
            requested_notional=float(body.get('notional') or 0),
            requested_sl_pct=float(body.get('sl_pct')) if body.get('sl_pct') else None,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'err': str(e), 'can_fire': True,  # fail-open by default
                        'block_reason': 'check_failed'})

@app.route('/api/attribution')
def api_attribution():
    """Cross-reference HL fills against engine cloid registries.
    Returns real per-engine PnL + collision detection."""
    try:
        from attribution import compute_attribution
        with _lock:
            engines_snapshot = dict(_engine_states)
        return jsonify(compute_attribution(engines_snapshot))
    except Exception as e:
        return jsonify({'err': str(e), 'fills_total': 0, 'attributed': 0, 'by_engine': {}})

@app.route('/')
def index():
    return Response(_HTML, mimetype='text/html')

# ────────────────────────────────────────────────────────────────
# HTML — terminal aesthetic, polls /api/state + /api/account
# ────────────────────────────────────────────────────────────────
_HTML = r"""<!doctype html>
<html><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>// engine_dashboard</title>
<style>
:root {
  --bg:           #0a0a0a;
  --fg:           #d4d4d4;
  --dim:          #6b6b6b;
  --green:        #5fff87;
  --green-dim:    #2a8a3e;
  --red:          #ff6b6b;
  --red-dim:      #8a2a2a;
  --amber:        #ffb86c;
  --cyan:         #67d3ed;
  --grid-line:    #1f1f1f;
  --frame:        #2a2a2a;
}
* { box-sizing: border-box; }
html, body { margin:0; padding:0; background: var(--bg); color: var(--fg);
  font-family: 'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace;
  font-size: 13px; line-height: 1.4; }
.topbar {
  padding: 10px 16px; border: 1px solid var(--frame);
  margin: 8px 8px 4px; position: relative;
  white-space: nowrap; overflow-x: auto;
}
.topbar::before, .topbar::after,
.panel::before, .panel::after { content:''; position:absolute; width:14px; height:14px;
  border: 2px solid var(--green); }
.topbar::before, .panel::before { left:-1px; top:-1px; border-right:none; border-bottom:none; }
.topbar::after, .panel::after { right:-1px; top:-1px; border-left:none; border-bottom:none; }
.panel-bot::before { content:''; position:absolute; left:-1px; bottom:-1px; width:14px; height:14px;
  border: 2px solid var(--red); border-right:none; border-top:none; }
.panel-bot::after  { content:''; position:absolute; right:-1px; bottom:-1px; width:14px; height:14px;
  border: 2px solid var(--red); border-left:none; border-top:none; }
.dim { color: var(--dim); }
.green { color: var(--green); }
.green-dim { color: var(--green-dim); }
.red { color: var(--red); }
.red-dim { color: var(--red-dim); }
.amber { color: var(--amber); }
.cyan { color: var(--cyan); }
.grid {
  display: grid; gap: 8px; padding: 4px 8px 8px;
  grid-template-columns: 1fr;
}
@media (min-width: 1100px) { .grid { grid-template-columns: 1fr 1fr; } }
@media (min-width: 1700px) { .grid { grid-template-columns: 1fr 1fr 1fr; } }
.panel { position: relative; padding: 16px; border: 1px solid var(--frame);
  background: rgba(255,255,255,0.01); }
.panel.stale { opacity: 0.55; border-color: var(--red-dim); }
.panel-head { display:flex; justify-content:space-between; align-items:baseline;
  border-bottom: 1px solid var(--grid-line); padding-bottom: 8px; margin-bottom: 12px;
  font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase; }
.panel-head .name { color: var(--fg); }
.panel-head .meta { color: var(--dim); }
.panel-head .live-on  { color: var(--green); }
.panel-head .live-off { color: var(--amber); }
.metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px 16px; margin-bottom: 12px; }
.metric .v { font-size: 22px; line-height: 1.1; font-weight: 500; }
.metric .l { font-size: 10px; letter-spacing: 0.18em; color: var(--dim); margin-top: 4px; }
.metrics-2 { display: grid; grid-template-columns: repeat(3, minmax(0,1fr));
  gap: 8px 16px; margin-bottom: 14px; }
.section-title { color: var(--red-dim); font-size: 11px; letter-spacing: 0.18em;
  text-transform: uppercase; margin: 14px 0 6px; }
.poslist { font-size: 12px; color: var(--fg); }
.poslist .row { display: grid; grid-template-columns: 60px 60px 1fr 1fr 80px; gap: 8px;
  padding: 2px 0; border-bottom: 1px dotted var(--grid-line); }
.dim-line { color: var(--dim); }
.tele-line { color: var(--dim); font-size: 12px; margin-top: 6px; }
.empty { color: var(--dim); font-style: italic; padding: 4px 0; }
.refreshing { position:fixed; bottom:8px; right:14px; color: var(--dim); font-size: 11px; }
</style>
</head>
<body>
<div class="topbar">
  <span class="dim">// </span>
  <span>equity </span><span id="t-equity" class="green">$--</span>
  <span class="dim"> · </span>
  <span>open </span><span id="t-open" class="cyan">--</span>
  <span class="dim"> · </span>
  <span>BTC </span><span id="t-btc" class="amber">$--</span>
  <span class="dim"> · </span>
  <span>ETH </span><span id="t-eth" class="amber">$--</span>
  <span class="dim"> · </span>
  <span id="t-pnl-12h" class="green">PnL_12h +$0.00</span>
  <span class="dim"> · </span>
  <span>WR_12h </span><span id="t-wr" class="cyan">--</span>
  <span class="dim"> · </span>
  <span>commit </span><span id="t-commit" class="dim">----</span>
</div>

<div class="grid" id="grid"></div>

<div class="refreshing" id="refreshing">// fetching…</div>

<script>
const ENGINES_LABELS = {
  'multi-gate': 'MULTI-GATE  ·  v8.28 confluence',
  'smc-v1':     'SMC v1  ·  long-only',
  'smc-v2':     'SMC v2  ·  R3 strict',
  'smc-loose':  'SMC-LOOSE  ·  R-1_max',
  'lsr':        'LSR  ·  liquidity sweep reversal',
  'brk':        'BRK  ·  break+retest continuation',
  'pool-arch-rev':  'POOL-ARCH (REV)  ·  UZT reversal leg',
  'pool-arch-cont': 'POOL-ARCH (CONT) ·  UZT continuation leg'
};

function fmtUsd(n, opts={}) {
  if (n === null || n === undefined) return '—';
  const sign = n > 0 ? '+' : (n < 0 ? '-' : '+');
  const abs = Math.abs(n);
  if (opts.precision === 3) return `${sign}$${abs.toFixed(3)}`;
  return `${sign}$${abs.toFixed(2)}`;
}
function colorPnl(n) {
  if (n > 0.001) return 'green';
  if (n < -0.001) return 'red';
  return 'dim';
}
function fmtPct(n) {
  if (n === null || n === undefined) return '—';
  return n.toFixed(1) + '%';
}
function fmtRR(n) {
  if (n === null || n === undefined) return '—';
  return '1:' + Number(n).toFixed(2);
}
function fmtAge(sec) {
  if (sec === undefined || sec === null) return '—';
  if (sec < 60) return sec + 's';
  if (sec < 3600) return Math.floor(sec/60) + 'm';
  return (sec/3600).toFixed(1) + 'h';
}
function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function renderPanel(s) {
  const stale = s.stale ? ' stale' : '';
  const liveLabel = !s.present
    ? '<span class="live-off">no_signal</span>'
    : (s.live ? '<span class="live-on">LIVE</span>'
              : '<span class="live-off">DRY-RUN</span>');
  const ageLabel = s.present ? `updated ${fmtAge(s.age_sec)} ago` : 'never reported';
  const sizingLabel = s.present
    ? `${s.sizing_mode || '—'} · $${s.notional_usd ?? '—'} · max ${s.max_concurrent ?? '—'}`
    : '—';

  let openHtml = '<div class="empty">// no open positions</div>';
  if (s.open_positions && s.open_positions.length) {
    openHtml = '<div class="poslist">' + s.open_positions.slice(0, 8).map(p => `
      <div class="row">
        <span class="${p.side === 'LONG' ? 'green' : 'red'}">${escapeHtml(p.side || '?')}</span>
        <span>${escapeHtml(p.coin)}</span>
        <span class="dim">e ${escapeHtml(p.entry)}</span>
        <span class="dim">sl ${escapeHtml(p.sl)}</span>
        <span class="${colorPnl(p.unreal_pnl || 0)}">${fmtUsd(p.unreal_pnl || 0)}</span>
      </div>`).join('') + '</div>';
    if (s.open_positions.length > 8) {
      openHtml += `<div class="dim-line">  + ${s.open_positions.length - 8} more</div>`;
    }
  }

  let teleLine = '';
  const t = s.telemetry || {};
  if (Object.keys(t).length) {
    teleLine = `<div class="tele-line">// fires ${t.fires_total ?? 0} · filled ${t.filled ?? 0}/${t.fires_total ?? 0} · errors ${t.errors ?? 0} · scans ${t.scan_count ?? 0}</div>`;
  }

  const wlb = `${s.wins}/${s.losses}/${s.breakevens}`;
  return `<div class="panel${stale}">
    <div class="panel-head">
      <span class="name">${escapeHtml(ENGINES_LABELS[s.engine] || s.engine)}</span>
      <span class="meta">${liveLabel} · ${escapeHtml(ageLabel)}</span>
    </div>
    <div class="metrics">
      <div class="metric"><div class="v ${colorPnl(s.pnl_total)}">${fmtUsd(s.pnl_total)}</div><div class="l">$ pnl 12h</div></div>
      <div class="metric"><div class="v ${s.wr === null ? 'dim' : 'cyan'}">${fmtPct(s.wr)}</div><div class="l">win rate</div></div>
      <div class="metric"><div class="v">${wlb}</div><div class="l">w/l/b</div></div>
      <div class="metric"><div class="v ${colorPnl(s.avg_win)}">${fmtUsd(s.avg_win, {precision:3})}</div><div class="l">avg win</div></div>
      <div class="metric"><div class="v ${colorPnl(-Math.abs(s.avg_loss))}">${fmtUsd(-Math.abs(s.avg_loss || 0), {precision:3})}</div><div class="l">avg loss</div></div>
    </div>
    <div class="metrics-2">
      <div class="metric"><div class="v">${fmtRR(s.rr)}</div><div class="l">r:r</div></div>
      <div class="metric"><div class="v">${s.closes}</div><div class="l">closes</div></div>
      <div class="metric"><div class="v ${s.open_count > 0 ? 'cyan' : ''}">${s.open_count}/${s.max_concurrent ?? '∞'}</div><div class="l">open</div></div>
    </div>
    <div class="section-title">open positions</div>
    ${openHtml}
    <div class="section-title">engine config</div>
    <div class="dim-line">// ${escapeHtml(sizingLabel)}</div>
    ${teleLine}
    <div class="panel-bot"></div>
  </div>`;
}

async function refresh() {
  document.getElementById('refreshing').textContent = '// fetching…';
  try {
    const [s, a] = await Promise.all([
      fetch('/api/state').then(r => r.json()),
      fetch('/api/account').then(r => r.json()),
    ]);
    const acct = a.data || {};
    document.getElementById('t-equity').textContent = '$' + (acct.equity||0).toFixed(2);
    document.getElementById('t-btc').textContent = '$' + (acct.btc_px || 0).toLocaleString(undefined,{maximumFractionDigits:2});
    document.getElementById('t-eth').textContent = '$' + (acct.eth_px || 0).toLocaleString(undefined,{maximumFractionDigits:2});
    document.getElementById('t-open').textContent = (s.aggregate.open || 0).toString();
    const pnlEl = document.getElementById('t-pnl-12h');
    pnlEl.textContent = 'PnL_12h ' + fmtUsd(s.aggregate.pnl_total || 0);
    pnlEl.className = colorPnl(s.aggregate.pnl_total || 0);
    document.getElementById('t-wr').textContent = fmtPct(s.aggregate.wr);
    document.getElementById('t-commit').textContent = s.commit || 'dev';
    const grid = document.getElementById('grid');
    grid.innerHTML = s.engines.map(renderPanel).join('');
    const ts = new Date().toLocaleTimeString();
    document.getElementById('refreshing').textContent = `// refreshed ${ts}`;
  } catch (e) {
    document.getElementById('refreshing').textContent = '// fetch error: ' + e.message;
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body></html>
"""

# ────────────────────────────────────────────────────────────────
# Boot
# ────────────────────────────────────────────────────────────────
def main():
    lg(f'dashboard starting | port={PORT} | wallet={WALLET[:10]}... | commit={COMMIT}')
    _load_persisted()
    threading.Thread(target=_refresh_account_loop, daemon=True).start()
    # Alerts daemon — monitors engine staleness, dry mode, equity drops, no-fills
    try:
        from alerts import alert_loop
        def _get_engines():
            with _lock:
                return dict(_engine_states)
        def _get_account():
            with _account_lock:
                return dict(_account_cache.get('data') or {})
        threading.Thread(target=alert_loop, args=(_get_engines, _get_account),
                         daemon=True, name='alerts').start()
        lg('alerts daemon thread started')
    except Exception as e:
        lg(f'alerts daemon failed to start: {e}')
    app.run(host='0.0.0.0', port=PORT, threaded=True)

if __name__ == '__main__':
    main()
