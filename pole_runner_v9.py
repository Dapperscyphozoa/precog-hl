#!/usr/bin/env python3
"""V9 runner — paired bounce + breakout orders per fresh wall.

Each fresh wall produces TWO orders:
  - Bounce limit AT wall (placed immediately as resting order)
  - Breakout trigger PAST wall (held in internal watchlist; fires market when
    triggered by 5min candle close beyond the trigger price)

When either fills/triggers, cancel the sibling immediately.

State:
  state['pending']    — bounce limits placed on exchange
  state['triggers']   — armed breakout triggers (internal watchlist)
  state['positions']  — open positions

Isolation: HTTP polling only. No production touched.
"""
import json, os, sys, time, traceback, urllib.request
from datetime import datetime, timezone
from typing import Optional
from pole_engine_v9 import (PoleEngineV9, SpoofBreakoutEngine, WallTracker,
                              cluster_walls, Wall, BounceSetup, BreakoutTrigger)

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
MAX_TRIGGERS     = int(os.environ.get('MAX_TRIGGERS', '20'))
MAX_NOTIONAL_PCT = float(os.environ.get('MAX_NOTIONAL_PCT', '0.20'))
POLL_INTERVAL_S  = int(os.environ.get('POLL_INTERVAL_S', '30'))
COIN_PACE_MS     = int(os.environ.get('COIN_PACE_MS', '400'))
TRIGGER_EXPIRE_S = int(os.environ.get('TRIGGER_EXPIRE_S', '14400'))  # 4h
BOUNCE_EXPIRE_S  = int(os.environ.get('BOUNCE_EXPIRE_S', '14400'))   # 4h
STATE_FILE       = os.environ.get('STATE_FILE', '/var/data/pole_state_v9.json')
DEFAULT_COINS    = ('BTC,ETH,SOL,BNB,XRP,ADA,AVAX,DOGE,LINK,DOT,ATOM,NEAR,APT,SUI,'
                    'ARB,OP,INJ,TIA,SEI,LTC,UNI,CRV,WIF,ENA,JUP,ONDO,FET,LDO')
COINS            = [c.strip().upper() for c in os.environ.get('COINS', DEFAULT_COINS).split(',') if c.strip()]

state = {
    'balance': 0.0, 'positions': {}, 'pending': {}, 'triggers': {},
    'tick_count': 0, 'last_tick_t': 0,
    'fires_bounce': 0, 'fires_breakout_armed': 0, 'fires_breakout_triggered': 0, 'fires_spoof': 0,
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
def fetch_open_orders(): return (http_post(HL_API, {'type':'openOrders','user':WALLET}) or []) if WALLET else []

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
                state.update({k: v for k, v in loaded.items() if k in state})
                log(f"Loaded state: pos={len(state['positions'])} pend={len(state['pending'])} trig={len(state['triggers'])}")
    except Exception as e: log(f"load_state err: {e}")

def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE,'w') as f: json.dump(state, f, default=str)
    except Exception as e: log(f"save_state err: {e}")

EXCHANGE = None
TRACKER = WallTracker(max_history=30)
POLE = PoleEngineV9()
SPOOF = SpoofBreakoutEngine()
ATR_CACHE = {}

def get_atr(coin):
    c = ATR_CACHE.get(coin)
    if c and time.time() - c[0] < 600: return c[1]
    bars = fetch_candles(coin, '15m', 2)
    a = atr(bars, 14)
    ATR_CACHE[coin] = (time.time(), a)
    return a

def get_5m_close(coin):
    """Latest 5m candle close — used for breakout trigger evaluation."""
    bars = fetch_candles(coin, '5m', 1)
    return bars[-1] if bars else None

def init_sdk():
    if DRY_RUN:
        log("SDK skipped (DRY_RUN)"); return None
    try:
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        from eth_account import Account
        wallet = Account.from_key(PRIVATE_KEY)
        ex = Exchange(wallet, constants.MAINNET_API_URL, account_address=WALLET)
        log(f"SDK initialized (LIVE), wallet {wallet.address[:10]}...")
        return ex
    except Exception as e:
        log(f"SDK init failed: {e}"); return None

def place_limit(coin, is_buy, size, price, reduce_only=False, label=''):
    if DRY_RUN or not LIVE or EXCHANGE is None:
        log(f"  [DRY] LIMIT {coin} {'BUY' if is_buy else 'SELL'} sz={size:.6f} px={price:.6f} reduce={reduce_only} {label}")
        return {'status':'ok','response':{'data':{'statuses':[{'resting':{'oid':int(time.time()*1000000)}}]}}}
    try:
        return EXCHANGE.order(coin, is_buy, size, price, {'limit':{'tif':'Gtc'}}, reduce_only=reduce_only)
    except Exception as e:
        log(f"  limit err {coin}: {e}"); return None

def place_market(coin, is_buy, size, slippage=0.005, label=''):
    if DRY_RUN or not LIVE or EXCHANGE is None:
        log(f"  [DRY] MARKET {coin} {'BUY' if is_buy else 'SELL'} sz={size:.6f} {label}")
        return {'status':'ok'}
    try:
        return EXCHANGE.market_open(coin, is_buy, size, slippage=slippage)
    except Exception as e:
        log(f"  market err {coin}: {e}"); return None

def cancel_order(coin, oid):
    if DRY_RUN or not LIVE or EXCHANGE is None:
        log(f"  [DRY] CANCEL {coin} oid={oid}"); return True
    try: EXCHANGE.cancel(coin, oid); return True
    except Exception as e: log(f"  cancel err: {e}"); return False

def place_bounce(s: BounceSetup) -> Optional[str]:
    """Place bounce limit. Returns pkey if placed, None if skipped."""
    # Skip if already pending/positioned on this coin+side
    for p in state['pending'].values():
        if p['coin'] == s.coin and p['side'] == s.side: return None
    if s.coin in state['positions']: return None
    if len(state['pending']) >= MAX_PENDING:
        log(f"  skip bounce {s.coin} {s.side}: max pending"); return None
    size, notional = calc_size(state['balance'], RISK_PCT, s.entry_price, s.sl_price)
    if size <= 0: return None
    log(f"PLACE-BOUNCE {s.coin} {s.side} entry={s.entry_price:.6f} sl={s.sl_price:.6f} tp={s.tp_price:.6f} rr={s.rr:.2f} sz={size:.6f} ${notional:.2f}")
    log(f"  notes: {s.notes}")
    is_buy = (s.side == 'BUY')
    or_res = place_limit(s.coin, is_buy, size, s.entry_price, reduce_only=False, label='ENTRY')
    if not or_res: return None
    entry_oid = or_res.get('response',{}).get('data',{}).get('statuses',[{}])[0].get('resting',{}).get('oid')
    sl_res = place_limit(s.coin, not is_buy, size, s.sl_price, reduce_only=True, label='SL')
    tp_res = place_limit(s.coin, not is_buy, size, s.tp_price, reduce_only=True, label='TP')
    pkey = f"{s.coin}|BOUNCE|{s.wall_id}"
    state['pending'][pkey] = {
        'coin': s.coin, 'side': s.side, 'kind': 'BOUNCE', 'wall_id': s.wall_id,
        'entry_price': s.entry_price, 'sl': s.sl_price, 'tp': s.tp_price,
        'rr': s.rr, 'size': size, 'entry_oid': entry_oid,
        'placed_t': int(time.time()*1000),
        'sibling_breakout_id': s.sibling_breakout_id,
    }
    state['fires_bounce'] += 1
    return pkey

def arm_breakout(t: BreakoutTrigger):
    """Add breakout trigger to internal watchlist."""
    if len(state['triggers']) >= MAX_TRIGGERS:
        log(f"  skip arm-breakout {t.coin}: max triggers"); return
    tkey = f"{t.coin}|BREAKOUT|{t.wall_id}"
    if tkey in state['triggers']: return
    log(f"ARM-BREAKOUT {t.coin} {t.side} trigger@{t.trigger_price:.6f} sl={t.sl_price:.6f} tp={t.tp_price:.6f} rr={t.rr:.2f}")
    log(f"  notes: {t.notes}")
    state['triggers'][tkey] = {
        'coin': t.coin, 'side': t.side, 'wall_id': t.wall_id,
        'trigger_price': t.trigger_price, 'sl': t.sl_price, 'tp': t.tp_price,
        'rr': t.rr, 'armed_t': int(time.time()*1000),
        'sibling_bounce_id': t.sibling_bounce_id,
    }
    state['fires_breakout_armed'] += 1

def check_triggers(coin: str, last_5m: dict):
    """For each armed trigger on this coin, check if 5m close crossed trigger."""
    if not last_5m: return
    fired = []
    for tkey, t in list(state['triggers'].items()):
        if t['coin'] != coin: continue
        c = last_5m['c']
        if t['side'] == 'BUY' and c >= t['trigger_price']:
            fired.append((tkey, t, last_5m))
        elif t['side'] == 'SELL' and c <= t['trigger_price']:
            fired.append((tkey, t, last_5m))
    for tkey, t, bar in fired:
        size, notional = calc_size(state['balance'], RISK_PCT, t['trigger_price'], t['sl'])
        if size <= 0: del state['triggers'][tkey]; continue
        log(f"TRIGGER-FIRE {t['coin']} {t['side']} 5m_close={bar['c']:.6f} trigger={t['trigger_price']:.6f}")
        is_buy = (t['side'] == 'BUY')
        place_market(t['coin'], is_buy, size, label=f"BREAKOUT")
        place_limit(t['coin'], not is_buy, size, t['sl'], reduce_only=True, label='BREAKOUT-SL')
        place_limit(t['coin'], not is_buy, size, t['tp'], reduce_only=True, label='BREAKOUT-TP')
        state['positions'][t['coin']] = {
            'side': t['side'], 'kind': 'BREAKOUT', 'wall_id': t['wall_id'],
            'entry': t['trigger_price'], 'sl': t['sl'], 'tp': t['tp'],
            'size': size, 'opened_t': int(time.time()*1000),
        }
        state['fires_breakout_triggered'] += 1
        # Cancel sibling bounce
        sibling = t.get('sibling_bounce_id')
        if sibling:
            for pkey in list(state['pending'].keys()):
                if state['pending'][pkey].get('wall_id') == sibling:
                    p = state['pending'][pkey]
                    if p.get('entry_oid'): cancel_order(p['coin'], p['entry_oid'])
                    log(f"  CANCEL-SIBLING bounce {pkey}")
                    del state['pending'][pkey]
        del state['triggers'][tkey]

def reconcile():
    """Sync pending limits + positions vs exchange. Cancel siblings on fill."""
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
        oid = p.get('entry_oid')
        if oid and oid not in open_oids:
            coin = p['coin']
            if coin in ex_pos and abs(ex_pos[coin]) > 1e-9:
                state['positions'][coin] = {**p, 'filled_t': int(time.time()*1000)}
                log(f"  FILLED bounce {coin} {p['side']} @ {p['entry_price']}")
                # Cancel sibling breakout
                sibling = p.get('sibling_breakout_id')
                if sibling:
                    for tkey in list(state['triggers'].keys()):
                        if state['triggers'][tkey].get('wall_id') == sibling:
                            del state['triggers'][tkey]
                            log(f"  CANCEL-SIBLING breakout {tkey}")
            else:
                log(f"  UNFILLED bounce {coin} removed")
            to_remove.append(pkey)
    for k in to_remove: del state['pending'][k]

def expire_stale():
    """Cancel bounces and triggers older than expire windows."""
    now_ms = int(time.time()*1000)
    for pkey in list(state['pending'].keys()):
        p = state['pending'][pkey]
        age = (now_ms - p.get('placed_t', now_ms)) / 1000
        if age > BOUNCE_EXPIRE_S:
            if p.get('entry_oid'): cancel_order(p['coin'], p['entry_oid'])
            log(f"  EXPIRE-BOUNCE {pkey} (age={age/3600:.1f}h)")
            del state['pending'][pkey]
    for tkey in list(state['triggers'].keys()):
        t = state['triggers'][tkey]
        age = (now_ms - t.get('armed_t', now_ms)) / 1000
        if age > TRIGGER_EXPIRE_S:
            log(f"  EXPIRE-TRIGGER {tkey} (age={age/3600:.1f}h)")
            del state['triggers'][tkey]

def tick():
    expire_stale()
    reconcile()
    state['tick_count'] += 1
    state['last_tick_t'] = int(time.time()*1000)
    log(f"━━ TICK #{state['tick_count']} ━━")
    log(f"Bal:${state['balance']:.2f} Pos:{len(state['positions'])} Pend:{len(state['pending'])} Trig:{len(state['triggers'])} | "
        f"BO_armed:{state['fires_breakout_armed']} BO_fired:{state['fires_breakout_triggered']} Bounce:{state['fires_bounce']} Spoof:{state['fires_spoof']}")

    now_ts = time.time()
    summary = {}

    for coin in COINS:
        try:
            ob = fetch_orderbook(coin)
            if not ob or not ob.get('mid'):
                time.sleep(COIN_PACE_MS/1000.0); continue
            mid = ob['mid']
            bid_walls = cluster_walls(ob.get('bids', []), mid, 'bid')
            ask_walls = cluster_walls(ob.get('asks', []), mid, 'ask')
            # Need last 5m candle for touch detection + trigger evaluation
            last_5m = get_5m_close(coin)
            last_low = last_5m['l'] if last_5m else mid
            last_high = last_5m['h'] if last_5m else mid
            tracked = TRACKER.update(coin, bid_walls + ask_walls, mid, now_ts, last_low, last_high)

            verified_b = [w for w in tracked if w.side=='bid' and w.persistence_polls >= 5 and w.times_tested == 0]
            verified_a = [w for w in tracked if w.side=='ask' and w.persistence_polls >= 5 and w.times_tested == 0]
            nb = min(verified_b, key=lambda w: w.distance_pct, default=None)
            na = min(verified_a, key=lambda w: w.distance_pct, default=None)
            summary[coin] = {'mid': mid, 'vb': len(verified_b), 'va': len(verified_a), 'nb': nb, 'na': na}

            atr_v = get_atr(coin)
            if atr_v <= 0:
                time.sleep(COIN_PACE_MS/1000.0); continue

            # Check breakout triggers FIRST (before generating new ones)
            check_triggers(coin, last_5m)

            # Generate new paired setups
            bounces, breakouts = POLE.evaluate(coin, tracked, TRACKER, mid, atr_v, now_ts)
            for b in bounces: place_bounce(b)
            for bk in breakouts: arm_breakout(bk)

            # Spoof breakout (separate sub-engine)
            sps = SPOOF.evaluate(coin, tracked, TRACKER, mid, atr_v, now_ts)
            for sp in sps:
                if sp['coin'] in state['positions']: continue
                if len(state['positions']) >= MAX_POSITIONS: continue
                size, notional = calc_size(state['balance'], RISK_PCT, sp['entry_price'], sp['sl_price'])
                if size <= 0: continue
                log(f"PLACE-SPOOF {sp['coin']} {sp['side']} entry={sp['entry_price']:.6f} sl={sp['sl_price']:.6f} tp={sp['tp_price']:.6f} rr={sp['rr']:.2f} sz={size:.6f} ${notional:.2f}")
                log(f"  notes: {sp['notes']}")
                is_buy = (sp['side'] == 'BUY')
                place_market(sp['coin'], is_buy, size, label='SPOOF')
                place_limit(sp['coin'], not is_buy, size, sp['sl_price'], reduce_only=True, label='SPOOF-SL')
                place_limit(sp['coin'], not is_buy, size, sp['tp_price'], reduce_only=True, label='SPOOF-TP')
                state['positions'][sp['coin']] = {
                    'side': sp['side'], 'kind': 'SPOOF', 'entry': sp['entry_price'],
                    'sl': sp['sl_price'], 'tp': sp['tp_price'], 'size': size,
                    'opened_t': int(time.time()*1000),
                }
                state['fires_spoof'] += 1

            time.sleep(COIN_PACE_MS/1000.0)
        except Exception as e:
            log(f"  scan {coin} err: {e}")

    coins_with_zones = [c for c, d in summary.items() if d['nb'] and d['na']]
    log(f"Wall map: {len(coins_with_zones)} coins with both fresh bid+ask verified walls")
    for c in coins_with_zones[:12]:
        d = summary[c]; nb = d['nb']; na = d['na']
        log(f"  {c:6s} mid={d['mid']:>11.4f} | BID ${nb.usd/1000:>5.0f}k @{nb.price:>11.4f} -{nb.distance_pct*100:.2f}% ({nb.persistence_polls}p) | ASK ${na.usd/1000:>5.0f}k @{na.price:>11.4f} +{na.distance_pct*100:.2f}% ({na.persistence_polls}p)")

    save_state()

def main():
    log("=== POLE RUNNER V9 (BOUNCE + BREAKOUT) START ===")
    log(f"  PRECOG_URL: {PRECOG_URL}")
    log(f"  COINS: {len(COINS)}")
    log(f"  DRY_RUN: {DRY_RUN}, LIVE: {LIVE}")
    log(f"  RISK_PCT: {RISK_PCT}, MAX_NOT_PCT: {MAX_NOTIONAL_PCT}, LEVERAGE: {LEVERAGE}")
    log(f"  MAX_POS: {MAX_POSITIONS}, MAX_PENDING: {MAX_PENDING}, MAX_TRIGGERS: {MAX_TRIGGERS}")
    log(f"  POLL_INTERVAL_S: {POLL_INTERVAL_S}, COIN_PACE_MS: {COIN_PACE_MS}")
    log(f"  EXPIRE: bounce={BOUNCE_EXPIRE_S/3600:.1f}h trigger={TRIGGER_EXPIRE_S/3600:.1f}h")
    log(f"  WALLET: {WALLET[:10]+'...' if WALLET else 'NONE'}")
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
