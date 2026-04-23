// PRECOG v2 — Cluster handler + funding monitor
// 10x leverage, 1% equity risk, weighted legs up to 5, funding exit at 30% PnL
'use strict';
const hl = require('./hyperliquid-client');

const LEV          = 10;
const RISK_PCT     = 0.01;      // 1% of equity risked per signal
const MAX_LEGS     = 5;
const LEG_WEIGHTS  = [1.0, 0.7, 0.5, 0.35, 0.25]; // decreasing size per leg
const FUNDING_TP_PCT = 0.30;    // take profit at 30% PnL on any cluster
const MIN_NOTIONAL = 10;        // HL min

const clusters = {}; // { coin: { dir, legs: [{size, entry, ts}], totalSize, avgEntry, peakPnlPct } }

function log(msg) {
  const line = `[${new Date().toISOString()}] [PRECOG] ${msg}`;
  console.log(line);
  return line;
}

function calcSize(equity, price) {
  const usd = equity * RISK_PCT * LEV;
  if (usd < MIN_NOTIONAL) return 0;
  const raw = usd / price;
  const dec = raw >= 100 ? 0 : raw >= 10 ? 1 : raw >= 1 ? 2 : raw >= 0.1 ? 3 : 4;
  return parseFloat(raw.toFixed(dec));
}

function legPx(px, isBuy) {
  const mult = isBuy ? 1.005 : 0.995; // 0.5% aggressive limit
  const dec = px >= 10000 ? 0 : px >= 1000 ? 1 : px >= 100 ? 2 : px >= 1 ? 3 : 4;
  return parseFloat((px * mult).toFixed(dec));
}

async function flatten(sdk, coin) {
  const c = clusters[coin];
  if (!c || !c.legs.length) return null;
  const mids = await hl.getAllMids();
  const px = parseFloat(mids[coin] || 0);
  if (!px) return null;
  const isShort = c.dir === 'short';
  const size = c.legs.reduce((a, b) => a + b.size, 0);
  const closeIsBuy = isShort; // buy to close short
  const closePx = legPx(px, closeIsBuy);
  const result = await hl.closePosition(sdk, coin, closeIsBuy, size, closePx);
  log(`FLAT ${coin} ${c.dir.toUpperCase()} size=${size} avgEntry=${c.avgEntry.toFixed(4)} closePx=${closePx} → ${result}`);
  delete clusters[coin];
  return result;
}

// Handle inbound webhook alert. alertType ∈ {flip-long, flip-short, add-long, add-short}
async function handleAlert(sdk, alertType, coin, equity) {
  const isLong = alertType.endsWith('long');
  const isFlip = alertType.startsWith('flip');
  const newDir = isLong ? 'long' : 'short';
  const mids = await hl.getAllMids();
  const px = parseFloat(mids[coin] || 0);
  if (!px) { log(`${coin} no price`); return { err: 'no_price' }; }

  const existing = clusters[coin];

  // Flip: close opposite side if exists, then open new leg
  if (isFlip && existing && existing.dir !== newDir) {
    await flatten(sdk, coin);
  }

  // Re-read existing after possible flat
  let c = clusters[coin];
  if (!c) c = clusters[coin] = { dir: newDir, legs: [], totalSize: 0, avgEntry: 0, peakPnlPct: 0 };
  if (c.dir !== newDir) {
    log(`${coin} dir mismatch after flip attempt — aborting`);
    return { err: 'dir_mismatch' };
  }
  if (c.legs.length >= MAX_LEGS) {
    log(`${coin} MAX_LEGS reached (${MAX_LEGS}) — skipping ${alertType}`);
    return { err: 'max_legs' };
  }

  const baseSize = calcSize(equity, px);
  if (!baseSize) { log(`${coin} size=0 at px=${px}`); return { err: 'size_zero' }; }
  const weight = LEG_WEIGHTS[c.legs.length];
  const legSize = parseFloat((baseSize * weight).toFixed(4));
  if (legSize <= 0) return { err: 'leg_size_zero' };

  const orderPx = legPx(px, isLong);
  const result = await hl.placeOrder(sdk, coin, isLong, legSize, orderPx);
  const ok = result && !result.error;
  if (!ok) {
    log(`${coin} ${alertType} ORDER FAIL: ${JSON.stringify(result).slice(0,200)}`);
    return { err: 'order_failed', result };
  }

  // Record leg
  c.legs.push({ size: legSize, entry: px, ts: Date.now() });
  c.totalSize = c.legs.reduce((a, b) => a + b.size, 0);
  c.avgEntry = c.legs.reduce((a, b) => a + b.size * b.entry, 0) / c.totalSize;
  log(`${coin} ${alertType.toUpperCase()} leg${c.legs.length}/${MAX_LEGS} size=${legSize} px=${px} avgEntry=${c.avgEntry.toFixed(4)} total=${c.totalSize}`);
  return { ok: true, leg: c.legs.length, size: legSize, avgEntry: c.avgEntry };
}

// Monitor open clusters — take profit if any hits FUNDING_TP_PCT
async function fundingMonitor(sdk) {
  const openCoins = Object.keys(clusters);
  if (!openCoins.length) return;
  const mids = await hl.getAllMids();
  for (const coin of openCoins) {
    const c = clusters[coin];
    if (!c || !c.legs.length) continue;
    const px = parseFloat(mids[coin] || 0);
    if (!px) continue;
    const dirMult = c.dir === 'long' ? 1 : -1;
    const pnlPct = ((px - c.avgEntry) / c.avgEntry) * dirMult * LEV;
    if (pnlPct > c.peakPnlPct) c.peakPnlPct = pnlPct;
    if (pnlPct >= FUNDING_TP_PCT) {
      log(`${coin} TP HIT at pnl=${(pnlPct*100).toFixed(1)}% — flatten`);
      await flatten(sdk, coin);
    }
  }
}

function getClusters() {
  return Object.fromEntries(Object.entries(clusters).map(([k,v]) => [k, {
    dir: v.dir, legs: v.legs.length, totalSize: v.totalSize,
    avgEntry: v.avgEntry, peakPnlPct: v.peakPnlPct
  }]));
}

function reset() { for (const k in clusters) delete clusters[k]; }

module.exports = { handleAlert, fundingMonitor, flatten, getClusters, reset,
                    LEV, RISK_PCT, MAX_LEGS, FUNDING_TP_PCT };
