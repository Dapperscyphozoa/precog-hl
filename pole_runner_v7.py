#!/usr/bin/env python3
"""pole_runner_v7.py — Live runner for pole_engine_v7 (MM-mimic limit orders).

Per tick (every 15min):
  1. For each coin: fetch 1h, 15m, 5m bars
  2. Call PoleScannerV7.evaluate() → returns 0-2 LimitSetups (BUY + SELL)
  3. For each setup, place a limit entry order + reduce-only SL + reduce-only TP
  4. Track placed limit OIDs in state. On next tick:
     - If a limit was filled: position is now open, leave SL/TP brackets
     - If unfilled and zone moved: cancel the limit
  5. State persists to /var/data/pole_state_v7.json

Modes:
  DRY_RUN=1: log every setup, place no orders
  LIVE_TRADING=1 + DRY_RUN=0: place real limit orders
"""
import json, os, sys, time, traceback, urllib.request
from datetime import datetime, timezone
import pole_engine_v7

HL_API           = 'https://api.hyperliquid.xyz/info'
DRY_RUN          = os.environ.get('DRY_RUN', '1') == '1'
LIVE             = os.environ.get('LIVE_TRADING', '0') == '1' and not DRY_RUN
PRIVATE_KEY      = os.environ.get('HL_PRIVATE_KEY', '')
WALLET           = os.environ.get('HL_ADDRESS') or os.environ.get('HYPERLIQUID_ACCOUNT', '')
RISK_PCT         = float(os.environ.get('RISK_PCT', '0.005'))  # 0.5% risk per trade (conservative for v7)
LEVERAGE         = int(os.environ.get('LEVERAGE', '5'))
MAX_POSITIONS    = int(os.environ.get('MAX_POSITIONS', '8'))
MAX_PENDING      = int(os.environ.get('MAX_PENDING_LIMITS', '16'))
MAX_NOTIONAL_PCT = float(os.environ.get('MAX_NOTIONAL_PCT', '0.20'))  # per-trade max notional as fraction of equity  # cap open limit orders
TICK_INTERVAL_S  = int(os.environ.get('TICK_INTERVAL_S', '900'))
STATE_FILE       = os.environ.get('STATE_FILE', '/var/data/pole_state_v7.json')
DEFAULT_COINS    = ('BTC,ETH,SOL,BNB,XRP,ADA,AVAX,DOGE,LINK,DOT,ATOM,NEAR,APT,SUI,'
                    'ARB,OP,INJ,TIA,SEI,LTC,UNI,CRV,WIF,ENA,JUP,ONDO,FET,LDO')
COINS            = [c.strip().upper() for c in os.environ.get('COINS', DEFAULT_COINS).split(',') if c.strip()]

state = {
    'balance':       0.0,
    'positions':     {},   # coin -> filled-position dict
    'pending':       {},   # (coin, side, limit) -> pending limit setup
    'tick_count':    0,
    'last_tick_t':   0,
    'fires_total':   0,
    'log':           [],
}

def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] {msg}"
    print(line, flush=True)
    state['log'].append(line)
    if len(state['log']) > 200: state['log'] = state['log'][-200:]

def hl_post(body, timeout=20, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(HL_API, data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries-1: time.sleep(2 ** attempt); continue
            log(f"hl_post HTTP {e.code}"); return None
        except Exception as e:
            if attempt < retries-1: time.sleep(2 ** attempt); continue
            log(f"hl_post err: {e}"); return None
    return None

def fetch_candles(coin, interval, days):
    end = int(time.time()*1000); start = end - days*86400000
    body = {'type':'candleSnapshot','req':{'coin':coin,'interval':interval,'startTime':start,'endTime':end}}
    raw = hl_post(body)
    if not raw: return []
    bars = [{'t':b['t'],'o':float(b['o']),'h':float(b['h']),'l':float(b['l']),'c':float(b['c']),'v':float(b['v'])} for b in raw]
    bars.sort(key=lambda x:x['t'])
    return bars

def fetch_account_state():
    if not WALLET: return None
    return hl_post({'type':'clearinghouseState','user':WALLET})

def fetch_open_orders():
    if not WALLET: return []
    return hl_post({'type':'openOrders','user':WALLET}) or []

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                loaded = json.load(f)
                state.update({k:v for k,v in loaded.items() if k in state})
                log(f"Loaded state: {len(state.get('positions',{}))} positions, {len(state.get('pending',{}))} pending")
    except Exception as e:
        log(f"load_state err: {e}")

def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, default=str)
    except Exception as e:
        log(f"save_state err: {e}")

def calc_size(balance, risk_pct, entry, sl):
    risk_amount = balance * risk_pct
    sl_distance_pct = abs(entry - sl) / entry
    if sl_distance_pct <= 0: return 0, 0
    notional = risk_amount / sl_distance_pct
    notional = min(notional, balance * LEVERAGE, balance * MAX_NOTIONAL_PCT * LEVERAGE)
    size = notional / entry
    return size, notional

def init_sdk():
    if DRY_RUN:
        log("SDK init skipped (DRY_RUN)")
        return None
    try:
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        from eth_account import Account
        wallet = Account.from_key(PRIVATE_KEY)
        ex = Exchange(wallet, constants.MAINNET_API_URL, account_address=WALLET)
        log(f"SDK initialized (LIVE). Wallet: {wallet.address[:10]}...")
        return ex
    except Exception as e:
        log(f"SDK init failed: {e}")
        return None

EXCHANGE = None

def place_limit_order(coin, is_buy, size, price, reduce_only=False):
    if DRY_RUN or not LIVE or EXCHANGE is None:
        log(f"  [DRY] limit {coin} {'BUY' if is_buy else 'SELL'} sz={size:.6f} px={price:.6f} reduce={reduce_only}")
        return {'status':'ok', 'response':{'data':{'statuses':[{'resting':{'oid': int(time.time()*1000)}}]}}}
    try:
        return EXCHANGE.order(coin, is_buy, size, price,
                               {'limit':{'tif':'Gtc'}}, reduce_only=reduce_only)
    except Exception as e:
        log(f"  order err: {e}")
        return None

def cancel_order(coin, oid):
    if DRY_RUN or not LIVE or EXCHANGE is None:
        log(f"  [DRY] cancel {coin} oid={oid}")
        return True
    try:
        EXCHANGE.cancel(coin, oid)
        return True
    except Exception as e:
        log(f"  cancel err {coin} oid={oid}: {e}")
        return False

# Per-coin scanner instance (preserves cooldown state)
_scanners = {}
def get_scanner(coin):
    if coin not in _scanners:
        _scanners[coin] = pole_engine_v7.PoleScannerV7()
    return _scanners[coin]

def cancel_stale_pending(stale_age_s=1800):
    """Cancel limits older than stale_age_s."""
    now_ms = int(time.time()*1000)
    stale = []
    for pkey, p in list(state['pending'].items()):
        age_s = (now_ms - p.get('placed_t', now_ms)) / 1000
        if age_s > stale_age_s:
            stale.append(pkey)
    for k in stale:
        p = state['pending'][k]
        if p.get('entry_oid'): cancel_order(p['coin'], p['entry_oid'])
        if p.get('sl_oid'):    cancel_order(p['coin'], p['sl_oid'])
        if p.get('tp_oid'):    cancel_order(p['coin'], p['tp_oid'])
        log(f"  STALE-CANCEL: {p['coin']} {p['side']} (age={age_s/60:.0f}min)")
        del state['pending'][k]


def tick():
    cancel_stale_pending()
    state['tick_count'] += 1
    state['last_tick_t'] = int(time.time()*1000)
    log(f"━━━ TICK #{state['tick_count']} ━━━")

    # Account state
    acct = fetch_account_state()
    if acct:
        state['balance'] = float(acct['marginSummary']['accountValue'])
    ex_pos = {}
    if acct:
        for ap in acct.get('assetPositions', []):
            p = ap['position']
            ex_pos[p['coin']] = {'size':float(p['szi']), 'entry':float(p['entryPx']), 'pnl':float(p['unrealizedPnl'])}
    log(f"Balance: ${state['balance']:.2f} | Exchange positions: {len(ex_pos)} | Internal pending: {len(state['pending'])} | Internal positions: {len(state['positions'])}")

    # Get current open orders to reconcile
    open_orders = fetch_open_orders()
    open_oids = {o['oid'] for o in open_orders}

    # Reconcile pending: anything in our pending dict with oid not in open_oids = filled or cancelled
    to_remove = []
    for pkey, p in list(state['pending'].items()):
        if p.get('entry_oid') and p['entry_oid'] not in open_oids:
            # Either filled (now in ex_pos) or cancelled
            coin = p['coin']
            if coin in ex_pos and abs(ex_pos[coin]['size']) > 1e-9:
                # Filled — promote to position
                state['positions'][coin] = {**p, 'filled_t': state['last_tick_t']}
                log(f"  FILLED: {coin} {p['side']} @ {p['limit_price']}")
            else:
                log(f"  UNFILLED/CANCELLED: {coin} {p['side']} limit removed")
            to_remove.append(pkey)
    for k in to_remove:
        del state['pending'][k]

    # Available slots
    slots_pos = MAX_POSITIONS - len(state['positions'])
    slots_pending = MAX_PENDING - len(state['pending'])
    if slots_pending <= 0:
        log("Max pending limits reached, skipping new setups")
        save_state()
        return

    # Scan
    new_setups = []
    for coin in COINS:
        if coin in state['positions']: continue
        if any(p['coin']==coin for p in state['pending'].values()): continue  # already pending
        try:
            b1 = fetch_candles(coin, '1h', 14)
            b15 = fetch_candles(coin, '15m', 5)
            b5 = fetch_candles(coin, '5m', 2)
            time.sleep(0.2)
            if len(b1)<30 or len(b15)<30 or len(b5)<5: continue
            sc = get_scanner(coin)
            setups = sc.evaluate(coin, b1, b15, b5)
            for s in setups:
                new_setups.append((coin, s))
        except Exception as e:
            log(f"scan {coin} err: {e}"); time.sleep(1)

    log(f"New setups this tick: {len(new_setups)} | slots avail: pos={slots_pos} pending={slots_pending}")
    new_setups.sort(key=lambda x: -x[1].rr)

    placed = 0
    for coin, s in new_setups:
        if placed >= slots_pending: break
        size, notional = calc_size(state['balance'], RISK_PCT, s.limit_price, s.sl_price)
        if size <= 0: continue
        is_buy = (s.side == 'BUY')
        log(f"PLACING {coin} {s.side} limit={s.limit_price:.6f} sl={s.sl_price:.6f} tp={s.tp_price:.6f} rr={s.rr:.2f} sz={size:.6f} ${notional:.2f}")
        log(f"  zone: {s.entry_zone.kind}/{s.entry_zone.timeframe} → tgt: {s.target_zone.kind}/{s.target_zone.timeframe}")

        order_res = place_limit_order(coin, is_buy, size, s.limit_price, reduce_only=False)
        if not order_res or order_res.get('status') != 'ok':
            log(f"  entry order failed for {coin}"); continue
        entry_oid = order_res.get('response',{}).get('data',{}).get('statuses',[{}])[0].get('resting',{}).get('oid')

        sl_res = place_limit_order(coin, not is_buy, size, s.sl_price, reduce_only=True)
        tp_res = place_limit_order(coin, not is_buy, size, s.tp_price, reduce_only=True)

        pkey = f"{coin}|{s.side}|{s.limit_price:.8f}"
        state['pending'][pkey] = {
            'coin': coin, 'side': s.side,
            'limit_price': s.limit_price, 'sl': s.sl_price, 'tp': s.tp_price,
            'rr': s.rr, 'size': size,
            'entry_oid': entry_oid,
            'sl_oid': (sl_res or {}).get('response',{}).get('data',{}).get('statuses',[{}])[0].get('resting',{}).get('oid'),
            'tp_oid': (tp_res or {}).get('response',{}).get('data',{}).get('statuses',[{}])[0].get('resting',{}).get('oid'),
            'placed_t': state['last_tick_t'],
            'entry_zone_kind': s.entry_zone.kind,
            'target_zone_kind': s.target_zone.kind,
        }
        state['fires_total'] += 1
        placed += 1

    save_state()

def main():
    log("=== POLE RUNNER V7 (MM-MIMIC LIMITS) START ===")
    log(f"  COINS:        {len(COINS)}")
    log(f"  DRY_RUN:      {DRY_RUN}")
    log(f"  LIVE:         {LIVE}")
    log(f"  RISK_PCT:     {RISK_PCT}")
    log(f"  LEVERAGE:     {LEVERAGE}")
    log(f"  MAX_POS:      {MAX_POSITIONS}")
    log(f"  MAX_PENDING:  {MAX_PENDING}")
    log(f"  MAX_NOT_PCT:  {MAX_NOTIONAL_PCT}")
    log(f"  TICK_S:       {TICK_INTERVAL_S}")
    log(f"  WALLET:       {WALLET}")
    load_state()
    global EXCHANGE
    EXCHANGE = init_sdk()
    while True:
        try: tick()
        except Exception as e:
            log(f"tick err: {e}"); traceback.print_exc()
        now = time.time()
        nxt = ((int(now) // TICK_INTERVAL_S) + 1) * TICK_INTERVAL_S + 30
        sleep_s = max(30, nxt - now)
        log(f"sleeping {sleep_s:.0f}s until next tick")
        time.sleep(sleep_s)

if __name__ == '__main__':
    main()
