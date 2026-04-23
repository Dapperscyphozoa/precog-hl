// CP Engine v8 — Hyperliquid API Client
const https = require('https');
const config = require('./config');

const BASE = 'https://api.hyperliquid.xyz';

function post(path, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = https.request(BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
      timeout: 15000,
    }, res => {
      let d = '';
      res.on('data', c => d += c);
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { resolve(null); } });
    });
    req.on('error', e => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
    req.write(data);
    req.end();
  });
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// Fetch candles with pagination (5000 candle API limit)
async function getCandles(coin, interval, days) {
  const endMs = Date.now();
  const startMs = endMs - days * 24 * 60 * 60 * 1000;
  const allCandles = [];
  let batchEnd = endMs;

  for (let i = 0; i < 10; i++) {
    const batch = await post('/info', {
      type: 'candleSnapshot',
      req: { coin, interval, startTime: startMs, endTime: batchEnd }
    });
    if (!batch || !batch.length) break;
    const newCandles = batch.filter(c => c.t >= startMs && c.t < batchEnd);
    if (!newCandles.length) break;
    allCandles.unshift(...newCandles);
    batchEnd = batch[0].t;
    if (batchEnd <= startMs) break;
    await sleep(80);
  }

  // Deduplicate and sort
  const seen = new Set();
  return allCandles
    .filter(c => { if (seen.has(c.t)) return false; seen.add(c.t); return true; })
    .sort((a, b) => a.t - b.t)
    .map(c => ({ t: c.t, o: +c.o, h: +c.h, l: +c.l, c: +c.c, v: +c.v }));
}

// Fetch funding rate history with pagination
async function getFundingHistory(coin, days) {
  const endMs = Date.now();
  const startMs = endMs - days * 24 * 60 * 60 * 1000;
  const all = [];
  let batchEnd = endMs;

  for (let i = 0; i < 10; i++) {
    const batch = await post('/info', {
      type: 'fundingHistory', coin, startTime: startMs, endTime: batchEnd
    });
    if (!batch || !batch.length) break;
    const newRates = batch.filter(f => f.time >= startMs && f.time < batchEnd);
    if (!newRates.length) break;
    all.unshift(...newRates);
    batchEnd = batch[0].time;
    if (batchEnd <= startMs) break;
    await sleep(80);
  }

  // Return as hourly map: { hourKey: fundingRate }
  const map = {};
  for (const f of all) {
    const hk = Math.floor(f.time / 3600000);
    map[hk] = parseFloat(f.fundingRate);
  }
  return map;
}

// Get current account state (balance + open positions)
async function getAccountState(wallet) {
  const state = await post('/info', { type: 'clearinghouseState', user: wallet });
  if (!state || !state.marginSummary) return null;
  return {
    balance: parseFloat(state.marginSummary.accountValue),
    positions: (state.assetPositions || [])
      .filter(p => parseFloat(p.position.szi) !== 0)
      .map(p => ({
        coin: p.position.coin,
        size: parseFloat(p.position.szi),
        entry: parseFloat(p.position.entryPx),
        pnl: parseFloat(p.position.unrealizedPnl),
        direction: parseFloat(p.position.szi) > 0 ? 'BUY' : 'SELL',
      })),
  };
}

// Get all mid prices
async function getAllMids() {
  const mids = await post('/info', { type: 'allMids' });
  return mids || {};
}

// Get mark price for a coin
async function getMarkPrice(coin) {
  const mids = await getAllMids();
  return mids[coin] ? parseFloat(mids[coin]) : null;
}

// Get universe meta (asset indices, size decimals)
async function getMeta() {
  const meta = await post('/info', { type: 'meta' });
  return meta;
}

// Place limit order (GTC)
async function placeOrder(sdk, coin, isBuy, size, price) {
  if (!sdk) return null;
  try {
    const order = await sdk.exchange.placeOrder({
      coin: coin + '-PERP',
      is_buy: isBuy,
      sz: parseFloat(size.toFixed(4)),
      limit_px: parseFloat(price.toFixed(2)),
      order_type: { limit: { tif: 'Ioc' } },
      reduce_only: false,
    });
    return order.response?.data?.statuses?.[0] || null;
  } catch (e) {
    console.error(`[HL] Order error ${coin}:`, e.message, e.stack?.split('\n')[1]);
    return { error: e.message };
  }
}

// Close position (IOC reduceOnly)
async function closePosition(sdk, coin, isShort, size, price) {
  if (!sdk) return null;
  try {
    const order = await sdk.exchange.placeOrder({
      coin: coin + '-PERP',
      is_buy: isShort, // buy to close short, sell to close long
      sz: size,
      limit_px: price,
      order_type: { limit: { tif: 'Ioc' } },
      reduce_only: true,
    });
    const status = order.response?.data?.statuses?.[0];
    return status?.filled ? parseFloat(status.filled.avgPx) : null;
  } catch (e) {
    console.error(`[HL] Close error ${coin}:`, e.message);
    return null;
  }
}

module.exports = {
  getCandles, getFundingHistory, getAccountState,
  getAllMids, getMarkPrice, getMeta,
  placeOrder, closePosition, sleep,
};
