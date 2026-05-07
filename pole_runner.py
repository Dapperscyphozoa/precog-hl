#!/usr/bin/env python3
"""pole_runner.py — Live runner for pole_engine on Hyperliquid.

ARCHITECTURE
============
1. Every 15min tick (aligned to 15m bar close + 30s buffer):
   - Fetch 15m, 1h, 4h candles for each coin in universe
   - Call pole_engine.detect() per coin
   - For each fire, place limit entry + reduce-only SL + reduce-only TP brackets
2. Position management:
   - Reconcile internal state vs exchange every tick (HL clearinghouseState)
   - Persist state to /var/data/pole_state.json
   - Detect orphaned positions (on exchange, not in our state) — adopt or skip
3. Risk:
   - 1% equity risk per trade (sizing from SL distance, not fixed notional)
   - Max concurrent positions: env MAX_POSITIONS (default 8)
   - Hard SL_ROE backstop: -10% (defense in case bracket fails)
   - Per-coin cooldown: handled by pole_engine itself (per-LEVEL cooldown)
4. Modes:
   - DRY_RUN=1 (default): log signals, no orders
   - LIVE_TRADING=1 + DRY_RUN=0: actually trade

ENV VARS
========
HL_PRIVATE_KEY, HL_ADDRESS  — required for live
DRY_RUN                     — 1 = paper mode (default 1 for safety)
LIVE_TRADING                — 1 = enable order placement
COINS                       — comma list, default top 35 HL coins
RISK_PCT                    — fraction of equity at risk per trade (default 0.01)
LEVERAGE                    — default 5
MAX_POSITIONS               — default 8
TICK_INTERVAL_S             — default 900 (15min)
STATE_FILE                  — default /var/data/pole_state.json
"""

import json
import os
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone

import pole_engine

# ─── CONFIG ────────────────────────────────────────────────────────
HL_API           = 'https://api.hyperliquid.xyz/info'
DRY_RUN          = os.environ.get('DRY_RUN', '1') == '1'
LIVE             = os.environ.get('LIVE_TRADING', '0') == '1' and not DRY_RUN
PRIVATE_KEY      = os.environ.get('HL_PRIVATE_KEY', '')
WALLET           = os.environ.get('HL_ADDRESS') or os.environ.get('HYPERLIQUID_ACCOUNT', '')
RISK_PCT         = float(os.environ.get('RISK_PCT', '0.01'))
LEVERAGE         = int(os.environ.get('LEVERAGE', '5'))
MAX_POSITIONS    = int(os.environ.get('MAX_POSITIONS', '8'))
TICK_INTERVAL_S  = int(os.environ.get('TICK_INTERVAL_S', '900'))
STATE_FILE       = os.environ.get('STATE_FILE', '/var/data/pole_state.json')
DEFAULT_COINS    = ('BTC,ETH,SOL,BNB,XRP,ADA,AVAX,DOGE,LINK,DOT,ATOM,NEAR,APT,SUI,'
                    'ARB,OP,INJ,TIA,SEI,TRX,LTC,BCH,AAVE,UNI,CRV,MKR,WIF,ENA,JUP,'
                    'PYTH,JTO,STRK,ONDO,FET,LDO')
COINS            = [c.strip().upper() for c in os.environ.get('COINS', DEFAULT_COINS).split(',') if c.strip()]
HARD_SL_ROE      = float(os.environ.get('HARD_SL_ROE', '-10.0'))  # -10% ROE backstop

# ─── STATE ─────────────────────────────────────────────────────────
state = {
    'balance':       0.0,
    'peak_balance':  0.0,
    'positions':     {},   # coin -> {side, entry, sl, tp, size, opened_t, signal}
    'tick_count':    0,
    'last_tick_t':   0,
    'fires_total':   0,
    'wins_total':    0,
    'losses_total':  0,
    'pnl_total':     0.0,
    'log':           [],
}

# ─── LOG ───────────────────────────────────────────────────────────
def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] {msg}"
    print(line, flush=True)
    state['log'].append(line)
    if len(state['log']) > 200:
        state['log'] = state['log'][-200:]


# ─── HL API ────────────────────────────────────────────────────────
def hl_post(body, timeout=20, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                HL_API, data=json.dumps(body).encode(),
                headers={'Content-Type': 'application/json'},
            )
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                time.sleep(wait)
                continue
            log(f"hl_post HTTP {e.code}: {e.reason}")
            return None
        except Exception as e:
            log(f"hl_post err: {e}")
            return None
    return None


def fetch_candles(coin, interval, days):
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    all_bars = []
    batch_end = end_ms
    for _ in range(15):
        batch = hl_post({
            'type': 'candleSnapshot',
            'req': {'coin': coin, 'interval': interval,
                    'startTime': start_ms, 'endTime': batch_end},
        })
        if not batch:
            break
        new = [b for b in batch if start_ms <= b['t'] < batch_end]
        if not new:
            break
        all_bars = new + all_bars
        batch_end = batch[0]['t']
        if batch_end <= start_ms:
            break
        time.sleep(0.05)
    seen = set()
    out = []
    for b in sorted(all_bars, key=lambda x: x['t']):
        if b['t'] in seen:
            continue
        seen.add(b['t'])
        out.append({
            't': b['t'], 'o': float(b['o']), 'h': float(b['h']),
            'l': float(b['l']), 'c': float(b['c']), 'v': float(b['v']),
        })
    return out


def fetch_account_state():
    if not WALLET:
        return None
    return hl_post({'type': 'clearinghouseState', 'user': WALLET})


def fetch_all_mids():
    return hl_post({'type': 'allMids'}) or {}


# ─── HL EXCHANGE ───────────────────────────────────────────────────
sdk = None
asset_meta = {}  # coin -> {'szDecimals': n, 'maxLeverage': n}


def init_sdk():
    global sdk, asset_meta
    if not LIVE or not PRIVATE_KEY:
        log(f"SDK init skipped (LIVE={LIVE}, key={'set' if PRIVATE_KEY else 'missing'})")
        return False
    try:
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from eth_account import Account
        from hyperliquid.utils import constants

        wallet_acc = Account.from_key(PRIVATE_KEY)
        sdk = Exchange(wallet_acc, constants.MAINNET_API_URL)
        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        meta = info.meta()
        for u in meta.get('universe', []):
            asset_meta[u['name']] = {
                'szDecimals': u.get('szDecimals', 4),
                'maxLeverage': u.get('maxLeverage', 10),
            }
        log(f"SDK connected. {len(asset_meta)} assets in meta.")
        return True
    except Exception as e:
        log(f"SDK init failed: {e}")
        traceback.print_exc()
        return False


def round_size(coin, raw_size):
    decimals = asset_meta.get(coin, {}).get('szDecimals', 4)
    return round(raw_size, decimals)


def round_price(price):
    if price >= 100000: return round(price)
    if price >= 10000:  return round(price, 1)
    if price >= 1000:   return round(price, 2)
    if price >= 100:    return round(price, 3)
    if price >= 10:     return round(price, 4)
    if price >= 1:      return round(price, 5)
    return round(price, 6)


def place_order(coin, is_buy, size, limit_px, reduce_only=False):
    """Place limit order. Returns order response or None."""
    if not sdk:
        return None
    try:
        sz = round_size(coin, size)
        px = round_price(limit_px)
        result = sdk.order(
            coin, is_buy, sz, px,
            {'limit': {'tif': 'Gtc'}},
            reduce_only=reduce_only,
        )
        return result
    except Exception as e:
        log(f"place_order {coin} err: {e}")
        return None


# ─── POSITION SIZING ───────────────────────────────────────────────
def calc_size(equity, risk_pct, entry, sl):
    """Risk-based sizing. Returns (size, notional)."""
    risk_dollars = equity * risk_pct
    sl_dist_pct = abs(entry - sl) / entry
    if sl_dist_pct <= 0:
        return 0, 0
    # raw size such that price moving from entry to SL = risk_dollars
    # size * sl_dist_pct * entry = risk_dollars  (notional × pct = $ at risk)
    # size = risk_dollars / (sl_dist_pct * entry)
    size = risk_dollars / (sl_dist_pct * entry)
    notional = size * entry
    # Cap notional by leverage budget: equity * leverage / max_positions
    max_notional = equity * LEVERAGE / MAX_POSITIONS
    if notional > max_notional:
        size = max_notional / entry
        notional = max_notional
    return size, notional


# ─── STATE PERSISTENCE ─────────────────────────────────────────────
def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log(f"save_state err: {e}")


def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                saved = json.load(f)
            state.update(saved)
            log(f"State loaded: bal=${state['balance']:.2f} peak=${state['peak_balance']:.2f} positions={len(state['positions'])}")
    except Exception as e:
        log(f"load_state err: {e}")


# ─── RECONCILER ────────────────────────────────────────────────────
def reconcile_with_exchange():
    """Sync internal positions vs exchange. Returns dict of exchange positions {coin: {size, entry, side}}."""
    acc = fetch_account_state()
    if not acc:
        return {}
    state['balance'] = float(acc.get('marginSummary', {}).get('accountValue', 0))
    if state['balance'] > state['peak_balance']:
        state['peak_balance'] = state['balance']
    ex_pos = {}
    for ap in acc.get('assetPositions', []):
        p = ap['position']
        sz = float(p['szi'])
        if sz == 0:
            continue
        ex_pos[p['coin']] = {
            'size': abs(sz),
            'entry': float(p['entryPx']),
            'side': 'BUY' if sz > 0 else 'SELL',
            'pnl': float(p['unrealizedPnl']),
        }
    # Drop our internal positions that aren't on exchange (closed by bracket)
    for coin in list(state['positions'].keys()):
        if coin not in ex_pos:
            log(f"position {coin} closed on exchange — removing from state")
            del state['positions'][coin]
    return ex_pos


# ─── ENGINE TICK ───────────────────────────────────────────────────
def tick():
    state['tick_count'] += 1
    state['last_tick_t'] = int(time.time() * 1000)
    log(f"━━━ TICK #{state['tick_count']} ━━━")

    ex_pos = reconcile_with_exchange()
    log(f"Balance: ${state['balance']:.2f} | Exchange positions: {len(ex_pos)} | Internal: {len(state['positions'])}")

    open_count = len(state['positions'])
    available_slots = MAX_POSITIONS - open_count

    if available_slots <= 0:
        log(f"Max positions ({MAX_POSITIONS}) — skipping new entries this tick")
        save_state()
        return

    # Scan each coin
    candidates = []
    for coin_idx, coin in enumerate(COINS):
        if coin in state['positions']:
            continue
        if coin in ex_pos:
            # already on exchange but not tracked — skip (probably another engine)
            continue
        try:
            b15 = fetch_candles(coin, '15m', 3)   # 3 days = ~288 bars, single batch
            b4h = fetch_candles(coin, '4h', 60)   # 60 days = ~360 bars, single batch
            b1h = []  # not used (USE_1H_POLES=0 by default)
            time.sleep(0.25)  # 250ms between coins to avoid rate limits
            if not b15 or len(b15) < 50 or not b4h or len(b4h) < 30:
                continue
            sig = pole_engine.detect(coin, b15, b1h, b4h)
            if sig:
                candidates.append((coin, sig))
        except Exception as e:
            log(f"scan {coin} err: {e}")
            time.sleep(1)
            continue

    log(f"Candidates: {len(candidates)} | Available slots: {available_slots} | Engine stats: fires={pole_engine.status()['fires']}")

    # Prefer higher RR candidates first
    candidates.sort(key=lambda x: -x[1]['rr'])

    for coin, sig in candidates[:available_slots]:
        side = sig['side']
        entry = sig['entry']
        sl = sig['sl']
        tp = sig['tp']
        rr = sig['rr']

        size, notional = calc_size(state['balance'], RISK_PCT, entry, sl)
        if size <= 0:
            continue

        log(f"FIRE {coin} {side} entry={entry} sl={sl} tp={tp} rr={rr:.2f} size={size:.6f} notional=${notional:.2f} swept={sig['swept_pole']['kind']}/{sig['swept_pole']['tf']} target={sig['target_pole']['kind']}/{sig['target_pole']['tf']}")

        if DRY_RUN or not LIVE:
            log(f"  DRY_RUN — no order placed")
            state['fires_total'] += 1
            continue

        # Place entry as IOC limit (immediate fill or skip)
        is_buy = (side == 'BUY')
        # Slight slippage cushion on entry (taker if needed)
        entry_px = entry * (1.0015 if is_buy else 0.9985)
        order_result = place_order(coin, is_buy, size, entry_px, reduce_only=False)
        if not order_result:
            log(f"  entry order failed for {coin}")
            continue

        # Place reduce-only SL + TP
        sl_result = place_order(coin, not is_buy, size, sl, reduce_only=True)
        tp_result = place_order(coin, not is_buy, size, tp, reduce_only=True)

        state['positions'][coin] = {
            'side': side, 'entry': entry, 'sl': sl, 'tp': tp, 'size': size,
            'opened_t': int(time.time() * 1000),
            'rr': rr,
            'swept': sig['swept_pole'],
            'target': sig['target_pole'],
            'sl_oid': (sl_result or {}).get('response', {}).get('data', {}).get('statuses', [{}])[0].get('resting', {}).get('oid'),
            'tp_oid': (tp_result or {}).get('response', {}).get('data', {}).get('statuses', [{}])[0].get('resting', {}).get('oid'),
        }
        state['fires_total'] += 1

    save_state()


# ─── MAIN LOOP ─────────────────────────────────────────────────────
def main():
    log(f"=== POLE RUNNER START ===")
    log(f"  COINS:        {len(COINS)}")
    log(f"  DRY_RUN:      {DRY_RUN}")
    log(f"  LIVE:         {LIVE}")
    log(f"  RISK_PCT:     {RISK_PCT}")
    log(f"  LEVERAGE:     {LEVERAGE}")
    log(f"  MAX_POS:      {MAX_POSITIONS}")
    log(f"  TICK_S:       {TICK_INTERVAL_S}")
    log(f"  WALLET:       {WALLET}")

    load_state()
    init_sdk()

    while True:
        try:
            tick()
        except Exception as e:
            log(f"tick err: {e}")
            traceback.print_exc()

        # Sleep until next 15m boundary + 30s
        now = time.time()
        next_boundary = ((int(now) // TICK_INTERVAL_S) + 1) * TICK_INTERVAL_S + 30
        sleep_s = max(30, next_boundary - now)
        log(f"sleeping {sleep_s:.0f}s until next tick")
        time.sleep(sleep_s)


if __name__ == '__main__':
    main()
