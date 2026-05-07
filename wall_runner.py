#!/usr/bin/env python3
"""wall_runner.py — Live runner for wall_engine using 6-venue orderbook + liq feed."""
import json, os, sys, time, traceback, urllib.request
from datetime import datetime, timezone
import orderbook_ws
import liquidation_ws
import wall_engine

HL_API           = 'https://api.hyperliquid.xyz/info'
DRY_RUN          = os.environ.get('DRY_RUN', '1') == '1'
LIVE             = os.environ.get('LIVE_TRADING', '0') == '1' and not DRY_RUN
PRIVATE_KEY      = os.environ.get('HL_PRIVATE_KEY', '')
WALLET           = os.environ.get('HL_ADDRESS') or os.environ.get('HYPERLIQUID_ACCOUNT', '')
RISK_PCT         = float(os.environ.get('RISK_PCT', '0.005'))
LEVERAGE         = int(os.environ.get('LEVERAGE', '5'))
MAX_POSITIONS    = int(os.environ.get('MAX_POSITIONS', '6'))
MAX_PENDING      = int(os.environ.get('MAX_PENDING_LIMITS', '12'))
MAX_NOTIONAL_PCT = float(os.environ.get('MAX_NOTIONAL_PCT', '0.20'))
TICK_INTERVAL_S  = int(os.environ.get('TICK_INTERVAL_S', '60'))   # 60s tick — walls update faster
STATE_FILE       = os.environ.get('STATE_FILE', '/var/data/wall_state.json')
WS_WARMUP_S      = int(os.environ.get('WS_WARMUP_S', '300'))      # 5min for walls to verify
DEFAULT_COINS    = ('BTC,ETH,SOL,BNB,XRP,ADA,AVAX,DOGE,LINK,DOT,ATOM,NEAR,APT,SUI,'
                    'ARB,OP,INJ,TIA,LTC,UNI,CRV,WIF,ENA,JUP,ONDO,LDO,PYTH,SEI,TON')
COINS            = [c.strip().upper() for c in os.environ.get('COINS', DEFAULT_COINS).split(',') if c.strip()]

state = {
    'balance': 0.0, 'positions': {}, 'pending': {},
    'tick_count': 0, 'last_tick_t': 0, 'fires_total': 0,
    'walls_seen_total': 0, 'log': [],
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
                                         headers={'Content-Type':'application/json'})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries-1: time.sleep(2 ** attempt); continue
            return None
        except Exception:
            if attempt < retries-1: time.sleep(2 ** attempt); continue
            return None
    return None

def fetch_account_state():
    if not WALLET: return None
    return hl_post({'type':'clearinghouseState','user':WALLET})

def fetch_open_orders():
    if not WALLET: return []
    return hl_post({'type':'openOrders','user':WALLET}) or []

def fetch_all_mids():
    return hl_post({'type':'allMids'}) or {}

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                loaded = json.load(f)
                state.update({k:v for k,v in loaded.items() if k in state})
                log(f"Loaded state: {len(state.get('positions',{}))} positions, {len(state.get('pending',{}))} pending")
    except Exception as e: log(f"load_state err: {e}")

def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, default=str)
    except Exception as e: log(f"save_state err: {e}")

def calc_size(balance, risk_pct, entry, sl, boost=1.0):
    risk_amount = balance * risk_pct * boost
    sl_dist_pct = abs(entry - sl) / entry
    if sl_dist_pct <= 0: return 0, 0
    notional = risk_amount / sl_dist_pct
    notional = min(notional, balance * LEVERAGE, balance * MAX_NOTIONAL_PCT * LEVERAGE)
    size = notional / entry
    return size, notional

EXCHANGE = None

def init_sdk():
    global EXCHANGE
    if DRY_RUN: log("SDK init skipped (DRY_RUN)"); return
    try:
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        from eth_account import Account
        wallet = Account.from_key(PRIVATE_KEY)
        EXCHANGE = Exchange(wallet, constants.MAINNET_API_URL, account_address=WALLET)
        log(f"SDK initialized (LIVE)")
    except Exception as e: log(f"SDK init failed: {e}")

def place_limit(coin, is_buy, size, price, reduce_only=False):
    if DRY_RUN or not LIVE or EXCHANGE is None:
        log(f"  [DRY] limit {coin} {'BUY' if is_buy else 'SELL'} sz={size:.6f} px={price:.6f} reduce={reduce_only}")
        return {'status':'ok','response':{'data':{'statuses':[{'resting':{'oid': int(time.time()*1000000) % 999999}}]}}}
    try:
        return EXCHANGE.order(coin, is_buy, size, price,
                               {'limit':{'tif':'Gtc'}}, reduce_only=reduce_only)
    except Exception as e: log(f"  order err: {e}"); return None

def cancel_order(coin, oid):
    if DRY_RUN or not LIVE or EXCHANGE is None:
        log(f"  [DRY] cancel {coin} oid={oid}"); return True
    try: EXCHANGE.cancel(coin, oid); return True
    except Exception as e: log(f"  cancel err {coin} oid={oid}: {e}"); return False

def cancel_stale_pending(stale_age_s=900):
    now_ms = int(time.time()*1000)
    stale = []
    for pkey, p in list(state['pending'].items()):
        age = (now_ms - p.get('placed_t', now_ms)) / 1000
        if age > stale_age_s: stale.append(pkey)
    for k in stale:
        p = state['pending'][k]
        if p.get('entry_oid'): cancel_order(p['coin'], p['entry_oid'])
        if p.get('sl_oid'): cancel_order(p['coin'], p['sl_oid'])
        if p.get('tp_oid'): cancel_order(p['coin'], p['tp_oid'])
        log(f"  STALE-CANCEL: {p['coin']} {p['side']} (age={(now_ms - p.get('placed_t',now_ms))/1000/60:.0f}min)")
        del state['pending'][k]

def tick():
    cancel_stale_pending()
    state['tick_count'] += 1
    state['last_tick_t'] = int(time.time()*1000)

    acct = fetch_account_state()
    if acct:
        state['balance'] = float(acct['marginSummary']['accountValue'])
    ex_pos = {}
    if acct:
        for ap in acct.get('assetPositions', []):
            p = ap['position']
            ex_pos[p['coin']] = {'size':float(p['szi']),'entry':float(p['entryPx']),'pnl':float(p['unrealizedPnl'])}

    open_orders = fetch_open_orders()
    open_oids = {o['oid'] for o in open_orders}
    to_remove = []
    for pkey, p in list(state['pending'].items()):
        if p.get('entry_oid') and p['entry_oid'] not in open_oids:
            coin = p['coin']
            if coin in ex_pos and abs(ex_pos[coin]['size']) > 1e-9:
                state['positions'][coin] = {**p, 'filled_t': state['last_tick_t']}
                log(f"  FILLED: {coin} {p['side']} @ {p['limit_price']}")
            else:
                log(f"  GONE: {coin} {p['side']} (unfilled or cancelled)")
            to_remove.append(pkey)
    for k in to_remove: del state['pending'][k]

    # Get live mid prices for all coins
    all_mids = fetch_all_mids()

    # Wall-engine summary
    venues = orderbook_ws.get_venue_status() if hasattr(orderbook_ws, 'get_venue_status') else {}
    walls_now = sum(len(orderbook_ws.get_walls(c)) for c in COINS)
    venues_alive = sum(1 for sec in venues.values() if isinstance(sec, (int, float)) and sec < 30)
    log(f"━━━ TICK #{state['tick_count']} | bal=${state['balance']:.2f} | ex_pos={len(ex_pos)} | pending={len(state['pending'])} | walls_now={walls_now} | venues_alive={venues_alive}/{len(venues)}")

    if walls_now == 0:
        log("  No walls verified yet (need WS warmup or no real walls present)")
        save_state()
        return

    new_setups = []
    for coin in COINS:
        if coin in state['positions']: continue
        if any(p['coin']==coin for p in state['pending'].values()): continue
        mid_str = all_mids.get(coin) if all_mids else None
        if not mid_str: continue
        try: mid = float(mid_str)
        except: continue
        try:
            setups = wall_engine.evaluate(coin, mid, orderbook_ws, liquidation_ws)
            for s in setups: new_setups.append((coin, s))
        except Exception as e: log(f"eval {coin}: {e}")

    log(f"New setups this tick: {len(new_setups)}")
    new_setups.sort(key=lambda x: -x[1].rr)

    slots = MAX_PENDING - len(state['pending'])
    placed = 0
    for coin, s in new_setups:
        if placed >= slots: break
        size, notional = calc_size(state['balance'], RISK_PCT, s.limit_price, s.sl_price, s.cascade_boost)
        if size <= 0: continue
        is_buy = (s.side == 'BUY')
        log(f"PLACING {coin} {s.side} limit={s.limit_price:.6f} sl={s.sl_price:.6f} tp={s.tp_price:.6f} rr={s.rr:.2f} sz={size:.6f} ${notional:.2f}")
        log(f"  {s.notes}")

        order_res = place_limit(coin, is_buy, size, s.limit_price, reduce_only=False)
        if not order_res or order_res.get('status') != 'ok':
            log(f"  entry failed for {coin}"); continue
        entry_oid = order_res.get('response',{}).get('data',{}).get('statuses',[{}])[0].get('resting',{}).get('oid')

        sl_res = place_limit(coin, not is_buy, size, s.sl_price, reduce_only=True)
        tp_res = place_limit(coin, not is_buy, size, s.tp_price, reduce_only=True)

        pkey = f"{coin}|{s.side}|{s.limit_price:.8f}"
        state['pending'][pkey] = {
            'coin': coin, 'side': s.side,
            'limit_price': s.limit_price, 'sl': s.sl_price, 'tp': s.tp_price,
            'rr': s.rr, 'size': size,
            'entry_oid': entry_oid,
            'sl_oid': (sl_res or {}).get('response',{}).get('data',{}).get('statuses',[{}])[0].get('resting',{}).get('oid'),
            'tp_oid': (tp_res or {}).get('response',{}).get('data',{}).get('statuses',[{}])[0].get('resting',{}).get('oid'),
            'placed_t': state['last_tick_t'],
            'entry_wall': s.entry_wall, 'target_wall': s.target_wall,
            'cascade_boost': s.cascade_boost,
        }
        state['fires_total'] += 1
        placed += 1

    save_state()

def main():
    log("=== WALL RUNNER START (6-venue orderbook + liq feed) ===")
    log(f"  COINS:        {len(COINS)}")
    log(f"  DRY_RUN:      {DRY_RUN}")
    log(f"  LIVE:         {LIVE}")
    log(f"  RISK_PCT:     {RISK_PCT}")
    log(f"  LEVERAGE:     {LEVERAGE}")
    log(f"  MAX_POS:      {MAX_POSITIONS}")
    log(f"  MAX_PENDING:  {MAX_PENDING}")
    log(f"  TICK_S:       {TICK_INTERVAL_S}")
    log(f"  WS_WARMUP_S:  {WS_WARMUP_S}")
    load_state()
    init_sdk()

    log("Starting WS streams: orderbook (6 venues) + liquidation (Binance)")
    orderbook_ws.start()
    liquidation_ws.start()

    log(f"Warming up WS for {WS_WARMUP_S}s before first tick (walls need ~5min to verify)")
    time.sleep(WS_WARMUP_S)

    while True:
        try: tick()
        except Exception as e: log(f"tick err: {e}"); traceback.print_exc()
        time.sleep(TICK_INTERVAL_S)

if __name__ == '__main__':
    main()
