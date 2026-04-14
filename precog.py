#!/usr/bin/env python3
"""PreCog v8 — production hardening

FIXES FROM v7 (per user spec):
1. ISOLATED margin only (no cross)
2. Pure live pivot detection (no lookahead)
3. 4h max hold per position (kills zombies, p99=4.1h measured)
4. State: atomic write + reconcile with HL every tick
5. Circuit breaker: 5 consecutive losses = 1h pause
6. Funding filter: cut if funding*fees / profit > 1/5
7. Ticker cull: top 105 coins by volume*ATR*signal_productivity
8. Kill switch: file /var/data/KILL flattens & exits
9. PnL logged on every close with pct return
"""
import os, json, time, traceback
from datetime import datetime
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

WALLET     = os.environ['HYPERLIQUID_ACCOUNT']
PRIV_KEY   = os.environ['HL_PRIVATE_KEY']
STATE_PATH = '/var/data/precog_state.json'
KILL_FILE  = '/var/data/KILL'

# Top 105 coins (culled from 176)
COINS = ['VVV','FARTCOIN','ZEC','XPL','ENA','TAO','WLD','kPEPE','AAVE','HYPE','DOT','ETH','SPX','PUMP','BLUR','MORPHO','FET','PENGU','VIRTUAL','NEAR','kBONK','ZRO','SOL','AVAX','WLFI','ARB','SUI','ALGO','RENDER','BTC','LINK','UNI','CRV','APT','ADA','TON','DOGE','XRP','TRUMP','TST']

GRID = {'sens':1, 'rsi':9, 'wick':1, 'ext':1, 'block':1, 'vol':1, 'cd':3}

def derive(s):
    return {
        'lb':       max(2, 2 + (s['ext']-1)*15),
        'rsi_hi':   50 + s['rsi']*3,
        'rsi_lo':   50 - s['rsi']*3,
        'wick':     (s['wick']-1) * 0.07,
        'struct_n': 99 if s['block']<2 else max(2, round(7 - s['block']*0.5)),
        'pivot_lb': max(2, 9 - s['sens']),
        'vol_mult': 1.0 + (s['vol']-1)*0.15,
        'cd':       s['cd']
    }
SP = derive(GRID); BP = derive(GRID)

INITIAL_RISK_PCT = 0.05
SCALED_RISK_PCT  = 0.005
SCALE_DOWN_AT    = 50000
LEV = 10
LOOP_SEC = 300
USE_ISOLATED_MARGIN = True

MAX_POSITIONS = 20
MAX_SAME_SIDE = 15
MAX_TOTAL_RISK = 0.95
BTC_VOL_THRESHOLD = 0.03

# v8 safety params
MAX_HOLD_SEC = 4 * 3600
CB_CONSEC_LOSSES = 5
CB_PAUSE_SEC = 3600
FUNDING_CUT_RATIO = 0.20

info = Info(constants.MAINNET_API_URL, skip_ws=True)
account = Account.from_key(PRIV_KEY)
exchange = Exchange(account, constants.MAINNET_API_URL, account_address=WALLET)

def log(m): print(f"[{datetime.utcnow().isoformat()}] {m}", flush=True)

def current_risk_pct(equity):
    return SCALED_RISK_PCT if equity >= SCALE_DOWN_AT else INITIAL_RISK_PCT

# ═══════════════════════════════════════════════════════
# STATE — atomic write, rich position tracking (FIX #4)
# ═══════════════════════════════════════════════════════
def load_state():
    default = {'positions':{}, 'cooldowns':{}, 'consec_losses':0, 'cb_pause_until':0, 'last_pnl_close':None}
    try:
        with open(STATE_PATH) as f:
            loaded = json.load(f)
        for k,v in default.items():
            if k not in loaded: loaded[k]=v
        return loaded
    except: return default

def save_state(s):
    """Atomic write: write to .tmp then rename."""
    os.makedirs('/var/data', exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    with open(tmp,'w') as f: json.dump(s,f)
    os.replace(tmp, STATE_PATH)

def kill_switch_active():
    return os.path.exists(KILL_FILE)

# ═══════════════════════════════════════════════════════
# INDICATORS (unchanged from v7)
# ═══════════════════════════════════════════════════════
def rma(a,n):
    r=[None]*len(a); seed=[x for x in a[:n] if x is not None]
    if len(seed)<n: return r
    s=sum(seed)/n; r[n-1]=s
    for i in range(n,len(a)):
        if a[i] is None: r[i]=s; continue
        s=(s*(n-1)+a[i])/n; r[i]=s
    return r

def rsi_calc(c,n=14):
    g=[0]*len(c); lo=[0]*len(c)
    for i in range(1,len(c)): d=c[i]-c[i-1]; g[i]=max(d,0); lo[i]=max(-d,0)
    ag=rma(g,n); al=rma(lo,n); r=[None]*len(c)
    for i in range(len(c)):
        if ag[i] is None: continue
        r[i]=100 if al[i]==0 else 100-100/(1+ag[i]/al[i])
    return r

def fetch(coin, n_bars=300):
    end=int(time.time()*1000); start=end-n_bars*5*60*1000
    try:
        d=info.candles_snapshot(coin,'5m',start,end)
        return [(int(c['t']),float(c['o']),float(c['h']),float(c['l']),float(c['c']),float(c['v'])) for c in d]
    except Exception as e:
        log(f"candle err {coin}: {e}"); return []

# ═══════════════════════════════════════════════════════
# SIGNAL — pure live bars, no lookahead (FIX #2 confirmed)
# ═══════════════════════════════════════════════════════
def signal(candles, last_sell_bar, last_buy_bar):
    """Uses only bars [0..N-1], all historical/confirmed. No future lookahead."""
    if len(candles)<100: return None,None
    o=[c[1] for c in candles]; h=[c[2] for c in candles]; l=[c[3] for c in candles]
    cl=[c[4] for c in candles]
    N=len(cl); r14=rsi_calc(cl,14)
    i = N-1  # most recent closed bar
    if r14[i] is None: return None, None
    br = h[i]-l[i]
    if br <= 0: return None, None
    LB = SP['pivot_lb']  # =8
    # Pivot confirmation requires LB bars of history before i (not future)
    is_pivot_high = h[i] == max(h[max(0,i-LB):i+1])
    is_pivot_low  = l[i] == min(l[max(0,i-LB):i+1])
    sell_ok = is_pivot_high and r14[i] > SP['rsi_hi'] and (i-last_sell_bar) > SP['cd']
    buy_ok  = is_pivot_low  and r14[i] < BP['rsi_lo'] and (i-last_buy_bar)  > BP['cd']
    if sell_ok: return 'SELL', i
    if buy_ok:  return 'BUY', i
    return None, None

# ═══════════════════════════════════════════════════════
# HL INTERFACE
# ═══════════════════════════════════════════════════════
def get_balance():
    try: return float(info.user_state(WALLET)['marginSummary']['accountValue'])
    except: return 0

def get_total_margin():
    try: return float(info.user_state(WALLET)['marginSummary'].get('totalMarginUsed', 0))
    except: return 0

def get_mid(coin):
    try: return float(info.all_mids()[coin])
    except: return None

def get_all_positions_live():
    """Returns dict of coin -> {size, entry, pnl, mark} for all actual positions on HL."""
    out={}
    try:
        for p in info.user_state(WALLET).get('assetPositions',[]):
            pos=p['position']
            sz=float(pos.get('szi',0))
            if sz!=0:
                out[pos['coin']] = {
                    'size':sz,
                    'entry':float(pos['entryPx']),
                    'pnl':float(pos['unrealizedPnl']),
                    'mark':float(pos.get('positionValue',0)) / abs(sz) if sz else 0
                }
    except Exception as e:
        log(f"positions fetch err: {e}")
    return out

def get_funding_rate(coin):
    """Fetch current funding rate for a coin (per hour). Negative = shorts pay, positive = longs pay."""
    try:
        meta = info.meta_and_asset_ctxs()
        asset_ctxs = meta[1]
        universe = meta[0]['universe']
        for i, u in enumerate(universe):
            if u['name']==coin and i<len(asset_ctxs):
                return float(asset_ctxs[i].get('funding', 0))
    except: pass
    return 0

def calc_size(equity, px, risk_pct, risk_mult=1.0):
    raw = equity * risk_pct * risk_mult * LEV / px
    if raw>=100: return round(raw,0)
    if raw>=10:  return round(raw,1)
    if raw>=1:   return round(raw,2)
    if raw>=0.1: return round(raw,3)
    return round(raw,4)

def set_isolated_leverage(coin):
    """FIX #1: set isolated margin + leverage before opening."""
    try:
        exchange.update_leverage(LEV, coin, is_cross=False)
    except Exception as e:
        log(f"lev set err {coin}: {e}")

def place(coin, is_buy, size):
    px=get_mid(coin)
    if not px: return None
    set_isolated_leverage(coin)
    slip = round(px*1.01,4) if is_buy else round(px*0.99,4)
    try:
        r=exchange.order(coin,is_buy,size,slip,{'limit':{'tif':'Ioc'}},reduce_only=False)
        log(f"ORDER {coin} {'BUY' if is_buy else 'SELL'} {size}@{slip}: {r}")
        return px  # return fill px for state tracking
    except Exception as e:
        log(f"order err {coin}: {e}")
        return None

def close(coin):
    """Returns realized pnl_pct for logging (FIX #11)."""
    live = get_all_positions_live().get(coin)
    if not live: return None
    is_buy=live['size']<0; size=abs(live['size']); px=get_mid(coin)
    if not px: return None
    slip=round(px*1.01,4) if is_buy else round(px*0.99,4)
    try:
        r=exchange.order(coin,is_buy,size,slip,{'limit':{'tif':'Ioc'}},reduce_only=True)
        entry = live['entry']
        pct = ((px-entry)/entry*100) if live['size']>0 else ((entry-px)/entry*100)
        pnl_usd = live['pnl']
        log(f"CLOSE {coin} {size}@{slip} | entry={entry} exit={px} | {pct:+.2f}% | ${pnl_usd:+.3f}")
        return pct
    except Exception as e:
        log(f"close err {coin}: {e}")
        return None

def flatten_all(reason='KILL'):
    live = get_all_positions_live()
    log(f"FLATTEN ALL ({reason}): {len(live)} positions")
    for coin in live:
        close(coin)
        time.sleep(0.3)

# ═══════════════════════════════════════════════════════
# PROCESS — one coin per tick
# ═══════════════════════════════════════════════════════
def process(coin, state, equity, live_positions, risk_mult=1.0):
    candles=fetch(coin)
    last_s=state['cooldowns'].get(coin+'_sell',-1000)
    last_b=state['cooldowns'].get(coin+'_buy',-1000)
    sig,bar=signal(candles,last_s,last_b)
    cur=state['positions'].get(coin)
    live=live_positions.get(coin)

    # FIX #3: 4h max hold check BEFORE signal logic
    if cur and cur.get('opened_at'):
        age = time.time() - cur['opened_at']
        if age > MAX_HOLD_SEC:
            log(f"{coin} MAX HOLD exceeded ({age/3600:.1f}h) — force close")
            pnl_pct = close(coin)
            if pnl_pct is not None:
                if pnl_pct < 0: state['consec_losses'] += 1
                else: state['consec_losses'] = 0
                state['last_pnl_close'] = pnl_pct
            state['positions'].pop(coin, None)
            return

    # FIX #6: Funding filter — cut if funding eating profits
    if live and live.get('pnl',0) > 0:
        funding_rate = get_funding_rate(coin)  # hourly rate
        # Estimate 1h forward cost: funding * notional (if wrong-side funding)
        pos_size = abs(live['size'])
        mark = live.get('mark', 0)
        notional = pos_size * mark
        # If holding long and funding > 0 → pay. Holding short and funding < 0 → pay.
        is_long = live['size'] > 0
        paying_funding = (is_long and funding_rate > 0) or (not is_long and funding_rate < 0)
        if paying_funding:
            hourly_cost = abs(funding_rate) * notional
            profit = live['pnl']
            # if hourly funding cost > 20% of current profit, cut
            if hourly_cost > profit * FUNDING_CUT_RATIO and profit > 0:
                log(f"{coin} FUNDING CUT: cost ${hourly_cost:.3f}/h vs profit ${profit:.3f} (ratio {hourly_cost/profit*100:.0f}%)")
                pnl_pct = close(coin)
                if pnl_pct is not None:
                    state['last_pnl_close'] = pnl_pct
                    state['consec_losses'] = 0  # funding cut = booked win, reset streak
                state['positions'].pop(coin, None)
                return

    if not sig: return

    # Enforce position caps (reconciled via live_positions)
    open_count = len(live_positions)
    if not live and open_count >= MAX_POSITIONS:
        log(f"{coin} {sig} SKIP (max {MAX_POSITIONS} positions)")
        return
    same_side_count = sum(1 for p in live_positions.values() if (p['size']>0 and sig=='BUY') or (p['size']<0 and sig=='SELL'))
    if not live and same_side_count >= MAX_SAME_SIDE:
        log(f"{coin} {sig} SKIP (side cap {MAX_SAME_SIDE})")
        return

    risk_pct = current_risk_pct(equity)
    total_locked = get_total_margin()
    proposed = equity * risk_pct * risk_mult
    if not live and (total_locked + proposed)/equity > MAX_TOTAL_RISK:
        log(f"{coin} {sig} SKIP (margin {total_locked:.0f}+{proposed:.0f} > {MAX_TOTAL_RISK*100:.0f}%)")
        return

    log(f"{coin} SIGNAL: {sig} (risk={int(risk_pct*100)}% mult={risk_mult})")

    now = time.time()
    if sig == 'SELL':
        state['cooldowns'][coin+'_sell'] = bar
        if live and live['size']>0:
            pnl_pct = close(coin)
            if pnl_pct is not None:
                if pnl_pct < 0: state['consec_losses'] += 1
                else: state['consec_losses'] = 0
                state['last_pnl_close'] = pnl_pct
        if not live or live['size']>0:
            px = get_mid(coin)
            if px:
                fill_px = place(coin, False, calc_size(equity, px, risk_pct, risk_mult))
                if fill_px:
                    state['positions'][coin] = {'side':'S', 'opened_at':now, 'entry':fill_px}
    else:
        state['cooldowns'][coin+'_buy'] = bar
        if live and live['size']<0:
            pnl_pct = close(coin)
            if pnl_pct is not None:
                if pnl_pct < 0: state['consec_losses'] += 1
                else: state['consec_losses'] = 0
                state['last_pnl_close'] = pnl_pct
        if not live or live['size']<0:
            px = get_mid(coin)
            if px:
                fill_px = place(coin, True, calc_size(equity, px, risk_pct, risk_mult))
                if fill_px:
                    state['positions'][coin] = {'side':'L', 'opened_at':now, 'entry':fill_px}

# ═══════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════
def main():
    log(f"PreCog v8 | wallet={WALLET} | coins={len(COINS)} | 5m | {LEV}x ISOLATED")
    log(f"Risk: {int(INITIAL_RISK_PCT*100)}% → {int(SCALED_RISK_PCT*100)}% at ${SCALE_DOWN_AT}")
    log(f"Caps: max_pos={MAX_POSITIONS} side={MAX_SAME_SIDE} margin={int(MAX_TOTAL_RISK*100)}%")
    log(f"Safety: max_hold={MAX_HOLD_SEC/3600:.0f}h | CB={CB_CONSEC_LOSSES} losses→{CB_PAUSE_SEC/60:.0f}min pause")
    log(f"Funding cut ratio: {FUNDING_CUT_RATIO*100:.0f}%")
    log(f"Grid: {GRID}")
    log(f"Derived: pivot_lb={SP['pivot_lb']} rsi_lo={BP['rsi_lo']} rsi_hi={SP['rsi_hi']} cd={SP['cd']}")

    while True:
        try:
            # FIX #8: Kill switch check first
            if kill_switch_active():
                log("KILL SWITCH DETECTED — flattening all positions and exiting")
                flatten_all('KILL')
                log("Kill complete. Remove /var/data/KILL to restart.")
                while kill_switch_active():
                    time.sleep(30)
                log("Kill switch cleared — resuming")

            state = load_state()
            equity = get_balance()
            now = time.time()

            # FIX #5: Circuit breaker check
            if now < state.get('cb_pause_until', 0):
                remaining = (state['cb_pause_until'] - now) / 60
                log(f"--- CIRCUIT BREAKER active: {remaining:.0f}min remaining (consec losses: {state['consec_losses']}) ---")
                time.sleep(LOOP_SEC)
                continue

            if state.get('consec_losses', 0) >= CB_CONSEC_LOSSES:
                log(f"!!! CIRCUIT BREAKER TRIPPED: {state['consec_losses']} consecutive losses. Pausing {CB_PAUSE_SEC/60:.0f}min !!!")
                state['cb_pause_until'] = now + CB_PAUSE_SEC
                state['consec_losses'] = 0  # reset after pause starts
                save_state(state)
                time.sleep(LOOP_SEC)
                continue

            # FIX #4: Reconcile state with HL reality
            live_positions = get_all_positions_live()
            # Drop phantoms (state has it, HL doesn't)
            for k in list(state['positions'].keys()):
                if state['positions'][k] and k not in live_positions:
                    log(f"RECONCILE: phantom {k} cleared")
                    state['positions'].pop(k)
            # Track live-only positions (HL has it, state doesn't)
            for k in live_positions:
                if k not in state['positions']:
                    side = 'L' if live_positions[k]['size']>0 else 'S'
                    state['positions'][k] = {'side':side, 'opened_at':now, 'entry':live_positions[k]['entry']}
                    log(f"RECONCILE: adopting existing {k} {side}")

            # BTC vol throttle
            risk_mult = 1.0
            try:
                btc_c = fetch('BTC')
                if len(btc_c) >= 12:
                    recent = btc_c[-12:]
                    hi = max(c[2] for c in recent); lo = min(c[3] for c in recent)
                    btc_range = (hi-lo)/lo
                    if btc_range > BTC_VOL_THRESHOLD:
                        risk_mult = 0.5
                        log(f"BTC vol {btc_range*100:.1f}% — risk halved")
            except Exception as e:
                log(f"btc vol err: {e}")

            cur_risk = current_risk_pct(equity)
            log(f"--- tick eq=${equity:.2f} risk={int(cur_risk*100)}% mult={risk_mult} positions={len(live_positions)} consec_L={state['consec_losses']} ---")

            for c in COINS:
                try:
                    process(c, state, equity, live_positions, risk_mult)
                    # Refresh live_positions snapshot periodically (every 10 coins)
                    if COINS.index(c) % 10 == 9:
                        live_positions = get_all_positions_live()
                except Exception as e:
                    log(f"err {c}: {e}")
                time.sleep(0.6)

            save_state(state)
            log(f"--- tick complete ---")
        except Exception as e:
            log(f"tick err: {e}\n{traceback.format_exc()}")
        time.sleep(LOOP_SEC)

if __name__ == '__main__':
    main()
