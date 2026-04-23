// PRECOG v2 — HL Webhook Execution Engine
// 10x / 1% equity risk / max 5 legs / funding exit at 30%
// Pine indicator fires → webhook → this server → HL execution
'use strict';
const http   = require('http');
const fs     = require('fs');
const { Hyperliquid } = require('hyperliquid');
const hl        = require('./hyperliquid-client');
const precog    = require('./precog-cluster');
const config    = require('./config');

const PORT    = process.env.PORT || 3000;
const SECRET  = process.env.WEBHOOK_SECRET || 'fd40f4c63e239dfe3028a45b5c6f8c5c078af5d5d2c5b059dd5af2841684b4af';
const WALLET  = process.env.HYPERLIQUID_ACCOUNT || config.WALLET_ADDRESS;
const KEY     = process.env.HYPERLIQUID_API_KEY || process.env.HL_PRIVATE_KEY || config.PRIVATE_KEY;
const LIVE    = process.env.LIVE_TRADING === 'true' || process.env.HL_AUTO_EXECUTE === 'true';
const FUNDING_MONITOR_MS = 15 * 60 * 1000; // 15 min
const CIRCUIT_DD = 0.25; // 25% from peak pauses new entries

// ── STATE ─────────────────────────────────────────────────────────
let sdk;
const state = {
  balance: 0, peak: 0, started: Date.now(),
  signalCount: 0, errorCount: 0, lastSignalTs: 0,
  log: [],
};
const mt4Queue = {};

function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}`;
  console.log(line);
  state.log.push(line);
  if (state.log.length > 300) state.log.shift();
  try {
    fs.writeFileSync('/tmp/precog-status.json', JSON.stringify({
      ...state, live: LIVE, wallet: WALLET, clusters: precog.getClusters(),
    }));
  } catch(e){}
}

async function refreshBalance() {
  const acct = await hl.getAccountState(WALLET);
  if (acct && acct.balance) {
    state.balance = acct.balance;
    if (state.balance > state.peak) state.peak = state.balance;
  }
}

function drawdown() {
  return state.peak > 0 ? (state.peak - state.balance) / state.peak : 0;
}

// ── INIT ──────────────────────────────────────────────────────────
async function initHL() {
  log(`PRECOG starting | LIVE=${LIVE} | WALLET=${WALLET} | LEV=${precog.LEV}x | RISK=${precog.RISK_PCT*100}%`);
  if (!KEY) { log('FATAL: no HL_PRIVATE_KEY'); return; }
  sdk = new Hyperliquid({ privateKey: KEY, walletAddress: WALLET, testnet: false });
  await sdk.connect();
  log('HL SDK connected');
  await refreshBalance();
  log(`Starting balance: $${state.balance.toFixed(2)}`);
  setInterval(async () => {
    try {
      await refreshBalance();
      await precog.fundingMonitor(sdk);
    } catch(e) { log(`monitor err: ${e.message}`); }
  }, FUNDING_MONITOR_MS);
}

// ── HTTP SERVER ───────────────────────────────────────────────────
function parseBody(req) {
  return new Promise((res, rej) => {
    let b = '';
    req.on('data', d => b += d);
    req.on('end', () => { try { res(JSON.parse(b)); } catch { res({}); } });
    req.on('error', rej);
  });
}

function sendJson(res, code, obj) {
  res.writeHead(code, {'Content-Type':'application/json'});
  res.end(JSON.stringify(obj));
}

async function handleSignalEndpoint(req, res, alertType) {
  const body = await parseBody(req);
  const secret = body.secret || req.headers['x-webhook-secret'] || '';
  if (secret !== SECRET) return sendJson(res, 403, { err: 'forbidden' });
  const coin = (body.ticker || body.coin || '').toUpperCase();
  if (!coin) return sendJson(res, 400, { err: 'missing ticker' });
  if (drawdown() >= CIRCUIT_DD) {
    log(`CIRCUIT BREAKER — DD=${(drawdown()*100).toFixed(1)}% blocking ${alertType} ${coin}`);
    return sendJson(res, 200, { skipped: 'circuit_breaker', dd: drawdown() });
  }
  state.signalCount++;
  state.lastSignalTs = Date.now();
  await refreshBalance();
  if (state.balance <= 0) return sendJson(res, 200, { skipped: 'no_balance' });
  try {
    const result = LIVE
      ? await precog.handleAlert(sdk, alertType, coin, state.balance)
      : { dryrun: true, alertType, coin, eq: state.balance };
    return sendJson(res, 200, { ok: true, ...result });
  } catch(e) {
    state.errorCount++;
    log(`${alertType} ${coin} ERR: ${e.message}`);
    return sendJson(res, 500, { err: e.message });
  }
}

const server = http.createServer(async (req, res) => {
  const url = req.url.split('?')[0];

  // Landing page
  if (url === '/' || url === '/index.html') {
    try {
      const html = fs.readFileSync('./public/index.html', 'utf8');
      res.writeHead(200, {'Content-Type':'text/html'});
      return res.end(html);
    } catch(e) {
      res.writeHead(200, {'Content-Type':'text/html'});
      return res.end(landingFallback());
    }
  }

  if (url === '/health') {
    return sendJson(res, 200, {
      status: 'PRECOG v2 ONLINE',
      live: LIVE, wallet: WALLET,
      balance: state.balance, peak: state.peak, dd: drawdown(),
      uptime_sec: (Date.now() - state.started) / 1000,
      signal_count: state.signalCount, error_count: state.errorCount,
      last_signal_ms_ago: state.lastSignalTs ? Date.now() - state.lastSignalTs : null,
      clusters: precog.getClusters(),
      config: { LEV: precog.LEV, RISK_PCT: precog.RISK_PCT,
                MAX_LEGS: precog.MAX_LEGS, FUNDING_TP_PCT: precog.FUNDING_TP_PCT },
    });
  }

  if (url === '/clusters') return sendJson(res, 200, precog.getClusters());

  if (url === '/logs') {
    return sendJson(res, 200, { log: state.log.slice(-100) });
  }

  // 4 PRECOG webhook endpoints
  if (req.method === 'POST') {
    if (url === '/precog/flip-long')  return handleSignalEndpoint(req, res, 'flip-long');
    if (url === '/precog/flip-short') return handleSignalEndpoint(req, res, 'flip-short');
    if (url === '/precog/add-long')   return handleSignalEndpoint(req, res, 'add-long');
    if (url === '/precog/add-short')  return handleSignalEndpoint(req, res, 'add-short');

    // Manual flatten (for emergencies)
    if (url === '/flatten') {
      const body = await parseBody(req);
      const secret = body.secret || req.headers['x-webhook-secret'] || '';
      if (secret !== SECRET) return sendJson(res, 403, { err: 'forbidden' });
      const coin = (body.ticker || body.coin || '').toUpperCase();
      if (!coin) return sendJson(res, 400, { err: 'missing ticker' });
      const r = await precog.flatten(sdk, coin);
      return sendJson(res, 200, { ok: true, result: r });
    }
    if (url === '/flatten-all') {
      const body = await parseBody(req);
      const secret = body.secret || req.headers['x-webhook-secret'] || '';
      if (secret !== SECRET) return sendJson(res, 403, { err: 'forbidden' });
      const out = [];
      for (const coin of Object.keys(precog.getClusters())) {
        try { out.push({ coin, result: await precog.flatten(sdk, coin) }); }
        catch(e) { out.push({ coin, err: e.message }); }
      }
      return sendJson(res, 200, { ok: true, flattened: out });
    }

    // MT4 bridge (preserved — separate from HL)
    if (url === '/webhook') {
      const body = await parseBody(req);
      const secret = body.secret || req.headers['x-webhook-secret'] || '';
      if (secret !== SECRET) return sendJson(res, 403, { err: 'forbidden' });
      const ticker = (body.ticker || '').toUpperCase();
      const direction = (body.direction || '').toUpperCase();
      if (!ticker || (direction !== 'BUY' && direction !== 'SELL')) {
        return sendJson(res, 400, { err: 'need ticker + direction' });
      }
      const sig = { direction, sl: parseFloat(body.sl)||0, tp: parseFloat(body.tp)||0,
                    entry: parseFloat(body.entry)||0, ts: Date.now() };
      mt4Queue[ticker] = [sig];
      log(`[MT4] ${ticker} ${direction} entry:${sig.entry}`);
      return sendJson(res, 200, { ok: true, ticker, direction });
    }
  }

  if (url === '/mt4/signals') {
    const out = [];
    for (const [ticker, signals] of Object.entries(mt4Queue)) {
      if (signals.length) {
        const s = signals.shift();
        if (!signals.length) delete mt4Queue[ticker];
        out.push({ ticker, mt4Symbol: ticker, direction: s.direction,
                    sl: s.sl, tp: s.tp, entry: s.entry, ts: s.ts });
        break;
      }
    }
    return sendJson(res, 200, out);
  }

  // Static assets
  if (url.startsWith('/public/') || url.endsWith('.css') || url.endsWith('.js') || url.endsWith('.png')) {
    try {
      const p = url.startsWith('/public/') ? '.' + url : './public' + url;
      const data = fs.readFileSync(p);
      const ct = url.endsWith('.css') ? 'text/css'
               : url.endsWith('.js')  ? 'application/javascript'
               : url.endsWith('.png') ? 'image/png'
               : 'application/octet-stream';
      res.writeHead(200, {'Content-Type': ct});
      return res.end(data);
    } catch(e) {}
  }

  res.writeHead(404); res.end('not found');
});

function landingFallback() {
  return `<!DOCTYPE html><html><head><meta charset="utf-8"><title>PRECOG v2</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{background:#0b0b10;color:#e6e6e6;font:14px ui-monospace,Menlo,monospace;padding:20px;max-width:720px;margin:auto}
h1{color:#00e676;border-bottom:1px solid #222;padding-bottom:8px}code{background:#1a1a1e;padding:2px 6px;border-radius:3px}
.card{background:#13131a;border:1px solid #222;border-radius:8px;padding:16px;margin:12px 0}
a{color:#00e676;text-decoration:none}</style></head><body>
<h1>◈ PRECOG v2</h1><p>HL webhook execution engine. 10x leverage, 1% equity risk, max 5 legs.</p>
<div class="card"><strong>Endpoints</strong><br>
<a href="/health">GET /health</a><br>
<a href="/clusters">GET /clusters</a><br>
<a href="/logs">GET /logs</a></div>
<div class="card"><strong>TradingView webhooks</strong><br>
<code>POST /precog/flip-long</code><br><code>POST /precog/flip-short</code><br>
<code>POST /precog/add-long</code><br><code>POST /precog/add-short</code></div></body></html>`;
}

server.listen(PORT, () => {
  console.log(`[PRECOG] Server listening on :${PORT}`);
  initHL().catch(e => log(`FATAL initHL: ${e.message}`));
});
