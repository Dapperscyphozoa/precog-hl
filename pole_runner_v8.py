#!/usr/bin/env python3
"""V8 runner — polls production /orderbook/<coin> for live multi-venue
wall data. Runs WallBounceEngine + SpoofBreakoutEngine.

Isolation:
  - Polls production via HTTP only. No shared process state.
  - Hits HL public API directly for account state + candles.
  - Own state file at /var/data/pole_state_v8.json.
  - Production untouched.

Modes:
  DRY_RUN=1: log every setup, place no orders.
  LIVE_TRADING=1 + DRY_RUN=0: place real orders.
"""
import json, os, sys, time, traceback, urllib.request
from datetime import datetime, timezone
from pole_engine_v8 import (WallBounceEngine, SpoofBreakoutEngine, WallTracker,
                              cluster_walls, Wall, Setup)

PRECOG_URL       = os.environ.get('PRECOG_URL', 'https://precog-i8c3.onrender.com')
HL_API           = 'https://api.hyperliquid.xyz/info'
DRY_RUN          = os.environ.get('DRY_RUN', '1') == '1'
LIVE             = os.environ.get('LIVE_TRADING', '0') == '1' and not DRY_RUN
PRIVATE_KEY      = os.environ.get('HL_PRIVATE_KEY', '')
WALLET           = os.environ.get('HL_ADDRESS') or os.environ.get('HYPERLIQUID_ACCOUNT', '')
RISK_PCT         = float(os.environ.get('RISK_PCT', '0.0025'))
LEVERAGE         = int(os.environ.get('LEVERAGE', '5'))
MAX_POSITIONS    = int(os.environ.get('MAX_POSITIONS', '6'))
MAX_PENDING      = int(os.environ.get('MAX_PENDING_LIMITS', '12'))
MAX_NOTIONAL_PCT = float(os.environ.get('MAX_NOTIONAL_PCT', '0.20'))
POLL_INTERVAL_S  = int(os.environ.get('POLL_INTERVAL_S', '30'))
COIN_PACE_MS     = int(os.environ.get('COIN_PACE_MS', '400'))
STATE_FILE       = os.environ.get('STATE_FILE', '/var/data/pole_state_v8.json')
DEFAULT_COINS    = ('BTC,ETH,SOL,BNB,XRP,ADA,AVAX,DOGE,LINK,DOT,ATOM,NEAR,APT,SUI,'
                    'ARB,OP,INJ,TIA,SEI,LTC,UNI,CRV,WIF,ENA,JUP,ONDO,FET,LDO')
COINS            = [c.strip().upper() for c in os.environ.get('COINS', DEFAULT_COINS).split(',') if c.strip()]

state = {
    'balance': 0.0, 'positions': {}, 'pending': {},
    'tick_count': 0, 'last_tick_t': 0, 'fires_total': 0,
    'fires_wb': 0, 'fires_sb': 0,
    'log': [],
}

def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%SZ')}] {msg}"
    print(line, flush=True)
    state['log'].append(line)
    if len(state['log']) > 500: state['log'] = state['log'][-500:]

def http_get(url, timeout=10):
    try: return json.loads(urllib.request.urlopen(url, timeout=timeout).read())
    except: return None

def http_post(url, body, timeout=10):
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                       headers={'Content-Type':'application/json'})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except: return None

def fetch_orderbook(coin): return http_get(f"{PRECOG_URL}/orderbook/{coin}")
def fetch_account(): return http_post(HL_API, {'type':'clearinghouseState','user':WALLET}) if WALLET else None
def fetch_open_orders(): return http_post(HL_API, {'type':'openOrders','user':WALLET}) or [] if WALLET else []

def fetch_candles(coin, interval='15m', days=2):
    end = int(time.time()*1000); start = end - days*86400000
    raw = http_post(HL_API, {'type':'candleSnapshot','req':{'coin':coin,'interval':interval,'startTime':start,'endTime':end}})
    if not raw: return []
    bars = [{'t':b['t'],'o':float(b['o']),'h':float(b['h']),'l':float(b['l']),'c':float(b['c']),'v':float(b['v'])} for b in raw]
    bars.sort(key=lambda x: x['t'])
    return bars

def atr(bars, period=14):
    if len(bars) < period+1: return 0.0
    trs = []
    for i in range(len(bars)-period, len(bars)):
        if i == 0: continue
        tr = max(bars[i]['h']-bars[i]['l'], abs(bars[i]['h']-bars[i-1]['c']), abs(bars[i]['l']-bars[i-1]['c']))
        trs.append(tr)
    return sum(trs)/len(trs) if trs else 0.0

def calc_size(balance, risk_pct, entry, sl):
    if balance <= 0: return 0, 0
    risk_amt = balance * risk_pct
    sl_dist = abs(entry - sl) / entry
    if sl_dist <= 0: return 0, 0
    notional = min(risk_amt / sl_dist, balance * LEVERAGE, balance * MAX_NOTIONAL_PCT * LEVERAGE)
    return notional / entry, notional

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                loaded = json.load(f)
                state.update({k:v for k,v in loaded.items() if k in state})
                log(f"Loaded state: pos={len(state.get('positions',{}))} pending={len(state.get('pending',{}))}")
    except Exception as e: log(f"load_state err: {e}")

def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE,'w') as f: json.dump(state, f, default=str)
    except Exception as e: log(f"save_state err: {e}")

EXCHANGE = None
TRACKER = WallTracker(max_history=30)
WALL_BOUNCE = WallBounceEngine()
SPOOF_BREAKOUT = SpoofBreakoutEngine()
ATR_CACHE = {}

def get_atr(coin):
    c = ATR_CACHE.get(coin)
    if c and time.time() - c[0] < 600: return c[1]
    bars = fetch_candles(coin, '15m', 2)
    a = atr(bars, 14)
    ATR_CACHE[coin] = (time.time(), a)
    return a

def init_sdk():
    if DRY_RUN:
        log("SDK skipped (DRY_RUN)")
        return None
    try:
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        from eth_account import Account
        wallet = Account.from_key(PRIVATE_KEY)
        ex = Exchange(wallet, constants.MAINNET_API_URL, account_address=WALLET)
        log(f"SDK initialized (LIVE), wallet {wallet.address[:10]}...")
        return ex
    except Exception as e:
        log(f"SDK init failed: {e}")
        return None

def place_limit(coin, is_buy, size, price, reduce_only=False):
    if DRY_RUN or not LIVE or EXCHANGE is None:
        log(f"  [DRY] LIMIT {coin} {'BUY' if is_buy else 'SELL'} sz={size:.6f} px={price:.6f} reduce={reduce_only}")
        return {'status':'ok','response':{'data':{'statuses':[{'resting':{'oid':int(time.time()*1000000)}}]}}}
    try:
        return EXCHANGE.order(coin, is_buy, size, price, {'limit':{'tif':'Gtc'}}, reduce_only=reduce_only)
    except Exception as e:
        log(f"  limit err {coin}: {e}"); return None

def place_market(coin, is_buy, size, slippage_pct=0.005):
    if DRY_RUN or not LIVE or EXCHANGE is None:
        log(f"  [DRY] MARKET {coin} {'BUY' if is_buy else 'SELL'} sz={size:.6f}")
        return {'status':'ok'}
    try:
        return EXCHANGE.market_open(coin, is_buy, size, slippage=slippage_pct)
    except Exception as e:
        log(f"  market err {coin}: {e}"); return None

def handle_setup(s: Setup):
    """Place orders for one setup. Updates state."""
    if s.kind == 'WALL_BOUNCE':
        # Skip if already pending/open on this coin+side
        for p in state['pending'].values():
            if p['coin'] == s.coin and p['side'] == s.side: return
        if s.coin in state['positions']: return
        if len(state['pending']) >= MAX_PENDING:
            log(f"  skip {s.coin} {s.side}: max pending"); return
    elif s.kind == 'SPOOF_BREAKOUT':
        if s.coin in state['positions']: return
        if len(state['positions']) >= MAX_POSITIONS:
            log(f"  skip spoof {s.coin}: max positions"); return

    size, notional = calc_size(state['balance'], RISK_PCT, s.entry_price, s.sl_price)
    if size <= 0:
        log(f"  size=0, skip"); return
    log(f"PLACE {s.kind} {s.coin} {s.side} entry={s.entry_price:.6f} sl={s.sl_price:.6f} tp={s.tp_price:.6f} rr={s.rr:.2f} sz={size:.6f} ${notional:.2f}")
    log(f"  notes: {s.notes}")

    is_buy = (s.side == 'BUY')
    if s.order_type == 'MARKET':
        res = place_market(s.coin, is_buy, size)
        if not res: return
        # Place SL + TP brackets
        place_limit(s.coin, not is_buy, size, s.sl_price, reduce_only=True)
        place_limit(s.coin, not is_buy, size, s.tp_price, reduce_only=True)
        state['positions'][s.coin] = {
            'side': s.side, 'entry': s.entry_price, 'sl': s.sl_price, 'tp': s.tp_price,
            'size': size, 'kind': s.kind, 'opened_t': int(time.time()*1000),
        }
    else:
        # LIMIT
        order_res = place_limit(s.coin, is_buy, size, s.entry_price, reduce_only=False)
        if not order_res: return
        entry_oid = order_res.get('response',{}).get('data',{}).get('statuses',[{}])[0].get('resting',{}).get('oid')
        sl_res = place_limit(s.coin, not is_buy, size, s.sl_price, reduce_only=True)
        tp_res = place_limit(s.coin, not is_buy, size, s.tp_price, reduce_only=True)
        pkey = f"{s.coin}|{s.side}|{s.entry_price:.8f}"
        state['pending'][pkey] = {
            'coin': s.coin, 'side': s.side, 'limit_price': s.entry_price,
            'sl': s.sl_price, 'tp': s.tp_price, 'rr': s.rr, 'size': size, 'kind': s.kind,
            'entry_oid': entry_oid, 'placed_t': int(time.time()*1000),
        }
    state['fires_total'] += 1
    if s.kind == 'WALL_BOUNCE': state['fires_wb'] += 1
    else: state['fires_sb'] += 1

def reconcile_pending():
    """Check exchange for fills, demote pending → positions or drop."""
    open_orders = fetch_open_orders()
    open_oids = {o['oid'] for o in open_orders}
    acct = fetch_account()
    ex_pos = {}
    if acct:
        state['balance'] = float(acct['marginSummary']['accountValue'])
        for ap in acct.get('assetPositions', []):
            p = ap['position']
            ex_pos[p['coin']] = float(p['szi'])
    to_remove = []
    for pkey, p in list(state['pending'].items()):
        if p.get('entry_oid') and p['entry_oid'] not in open_oids:
            coin = p['coin']
            if coin in ex_pos and abs(ex_pos[coin]) > 1e-9:
                state['positions'][coin] = {**p, 'filled_t': int(time.time()*1000)}
                log(f"  FILLED: {coin} {p['side']} @ {p['limit_price']}")
            else:
                log(f"  UNFILLED/CANCELLED: {coin} {p['side']} removed")
            to_remove.append(pkey)
    for k in to_remove:
        del state['pending'][k]

def cancel_stale_pending(stale_age_s=900):
    """Cancel limit orders older than stale_age_s (15min default)."""
    now_ms = int(time.time()*1000)
    stale = []
    for pkey, p in list(state['pending'].items()):
        age_s = (now_ms - p.get('placed_t', now_ms)) / 1000
        if age_s > stale_age_s: stale.append(pkey)
    for k in stale:
        p = state['pending'][k]
        if p.get('entry_oid') and not DRY_RUN and LIVE and EXCHANGE:
            try: EXCHANGE.cancel(p['coin'], p['entry_oid'])
            except: pass
        log(f"  STALE-CANCEL: {p['coin']} {p['side']} (age={age_s/60:.0f}min)")
        del state['pending'][k]

def tick():
    cancel_stale_pending()
    reconcile_pending()
    state['tick_count'] += 1
    state['last_tick_t'] = int(time.time()*1000)
    log(f"━━ TICK #{state['tick_count']} ━━")
    log(f"Balance: ${state['balance']:.2f} | Positions: {len(state['positions'])} | Pending: {len(state['pending'])} | Fires: WB={state['fires_wb']} SB={state['fires_sb']}")

    now_ts = time.time()
    summary = {}
    setups = []

    for coin in COINS:
        try:
            ob = fetch_orderbook(coin)
            if not ob or not ob.get('mid'):
                time.sleep(COIN_PACE_MS/1000.0); continue
            mid = ob['mid']
            bid_walls = cluster_walls(ob.get('bids', []), mid, 'bid')
            ask_walls = cluster_walls(ob.get('asks', []), mid, 'ask')
            tracked = TRACKER.update(coin, bid_walls + ask_walls, mid, now_ts)

            verified_bids = [w for w in tracked if w.side=='bid' and w.persistence_polls >= 5]
            verified_asks = [w for w in tracked if w.side=='ask' and w.persistence_polls >= 5]
            nb = min(verified_bids, key=lambda w: w.distance_pct, default=None)
            na = min(verified_asks, key=lambda w: w.distance_pct, default=None)
            summary[coin] = {'mid': mid, 'vb': len(verified_bids), 'va': len(verified_asks), 'nb': nb, 'na': na}

            atr_v = get_atr(coin)
            if atr_v <= 0:
                time.sleep(COIN_PACE_MS/1000.0); continue

            wb = WALL_BOUNCE.evaluate(coin, tracked, TRACKER, mid, atr_v, now_ts)
            sb = SPOOF_BREAKOUT.evaluate(coin, tracked, TRACKER, mid, atr_v, now_ts)
            setups.extend(wb); setups.extend(sb)

            time.sleep(COIN_PACE_MS/1000.0)
        except Exception as e:
            log(f"  scan {coin} err: {e}")

    coins_with_zones = [c for c, d in summary.items() if d['nb'] and d['na']]
    log(f"Wall map: {len(coins_with_zones)} coins with both bid+ask verified walls")
    for c in coins_with_zones[:15]:
        d = summary[c]; nb = d['nb']; na = d['na']
        log(f"  {c:6s} mid={d['mid']:>11.4f} | BID ${nb.usd/1000:>6.0f}k @{nb.price:>11.4f} -{nb.distance_pct*100:.2f}% ({nb.persistence_polls}p) | ASK ${na.usd/1000:>6.0f}k @{na.price:>11.4f} +{na.distance_pct*100:.2f}% ({na.persistence_polls}p)")

    log(f"Setups generated: {len(setups)}")
    for s in setups: handle_setup(s)
    save_state()

def main():
    log("=== POLE RUNNER V8 (WALL+SPOOF) START ===")
    log(f"  PRECOG_URL: {PRECOG_URL}")
    log(f"  COINS: {len(COINS)}")
    log(f"  DRY_RUN: {DRY_RUN}, LIVE: {LIVE}")
    log(f"  RISK_PCT: {RISK_PCT}, MAX_NOT_PCT: {MAX_NOTIONAL_PCT}, LEVERAGE: {LEVERAGE}")
    log(f"  MAX_POS: {MAX_POSITIONS}, MAX_PENDING: {MAX_PENDING}")
    log(f"  POLL_INTERVAL_S: {POLL_INTERVAL_S}, COIN_PACE_MS: {COIN_PACE_MS}")
    log(f"  WALLET: {WALLET[:10] + '...' if WALLET else 'NONE'}")
    load_state()
    global EXCHANGE
    EXCHANGE = init_sdk()
    while True:
        try: tick()
        except Exception as e:
            log(f"tick err: {e}"); traceback.print_exc()
        log(f"sleeping {POLL_INTERVAL_S}s")
        time.sleep(POLL_INTERVAL_S)

if __name__ == '__main__':
    main()
