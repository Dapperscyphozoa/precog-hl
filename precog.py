#!/usr/bin/env python3
"""PreCog v6.3 Lite + Structural Gate - Direct HL execution"""
import os, json, time, traceback, urllib.request, urllib.error
from datetime import datetime
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

WALLET     = os.environ['HYPERLIQUID_ACCOUNT']
PRIV_KEY   = os.environ['HL_PRIVATE_KEY']
STATE_PATH = '/var/data/precog_state.json'

COINS = ['FARTCOIN','kBONK','TRB','POLYX','HYPE','LINK','ARB','OP','ADA','SOL',
         'AAVE','ETH','INJ','BLUR','DOGE','XRP','BTC','AVAX','BNB']

# Structural-gate config (beat baseline: 80.2% WR, max streak 5)
EXT_LB=70; RSI_HI=75; RSI_LO=25; WICK_MIN=0.2; STRUCT_N=3; PIVOT_LB=5; MAX_LEGS=5
LEV=15; RISK_PCT=0.15; FUNDING_EXIT=0.30; LOOP_SEC=900   # 15 min

info = Info(constants.MAINNET_API_URL, skip_ws=True)
account = Account.from_key(PRIV_KEY)
exchange = Exchange(account, constants.MAINNET_API_URL, account_address=WALLET)

def log(m): print(f"[{datetime.utcnow().isoformat()}] {m}", flush=True)

def load_state():
    try:
        with open(STATE_PATH) as f: return json.load(f)
    except: return {'clusters':{}}
def save_state(s):
    os.makedirs('/var/data', exist_ok=True)
    with open(STATE_PATH,'w') as f: json.dump(s,f)

# ============ INDICATORS ============
def rma(a,n):
    r=[None]*len(a); seed=[x for x in a[:n] if x is not None]
    if len(seed)<n: return r
    s=sum(seed)/n; r[n-1]=s
    for i in range(n,len(a)):
        if a[i] is None: r[i]=s; continue
        s=(s*(n-1)+a[i])/n; r[i]=s
    return r
def rsi(c,n=14):
    g=[0]*len(c); lo=[0]*len(c)
    for i in range(1,len(c)): d=c[i]-c[i-1]; g[i]=max(d,0); lo[i]=max(-d,0)
    ag=rma(g,n); al=rma(lo,n); r=[None]*len(c)
    for i in range(len(c)):
        if ag[i] is None: continue
        r[i]=100 if al[i]==0 else 100-100/(1+ag[i]/al[i])
    return r

def fetch_candles(coin, n_bars=300):
    end = int(time.time()*1000); start = end - n_bars*15*60*1000
    try:
        data = info.candles_snapshot(coin, '15m', start, end)
        return [(int(c['t']), float(c['o']), float(c['h']), float(c['l']), float(c['c'])) for c in data]
    except Exception as e:
        log(f"candle err {coin}: {e}"); return []

def compute_signal(candles):
    """Returns ('BUY'|'SELL'|None) on the latest CLOSED bar."""
    if len(candles) < 100: return None
    o=[c[1] for c in candles]; h=[c[2] for c in candles]; l=[c[3] for c in candles]; cl=[c[4] for c in candles]
    N=len(cl); r14=rsi(cl,14)
    # Pivots (5,5)
    last_highs=[]; last_lows=[]
    for i in range(PIVOT_LB, N-PIVOT_LB):
        if h[i]==max(h[i-PIVOT_LB:i+PIVOT_LB+1]): last_highs.append(h[i])
        if l[i]==min(l[i-PIVOT_LB:i+PIVOT_LB+1]): last_lows.append(l[i])
    last_highs=last_highs[-STRUCT_N:]; last_lows=last_lows[-STRUCT_N:]
    asc_h = len(last_highs)>=STRUCT_N and all(last_highs[j]<last_highs[j+1] for j in range(STRUCT_N-1))
    desc_l= len(last_lows)>=STRUCT_N  and all(last_lows[j] >last_lows[j+1]  for j in range(STRUCT_N-1))
    # Last closed bar = N-1 (live bar excluded by candle endpoint at bar close)
    i = N-1
    if r14[i] is None: return None
    br = h[i]-l[i]
    uw = (h[i]-max(o[i],cl[i]))/br if br>0 else 0
    lw = (min(o[i],cl[i])-l[i])/br if br>0 else 0
    is_high = h[i]==max(h[i-EXT_LB+1:i+1])
    is_low  = l[i]==min(l[i-EXT_LB+1:i+1])
    if is_high and uw>=WICK_MIN and r14[i]>RSI_HI and not asc_h: return 'SELL'
    if is_low  and lw>=WICK_MIN and r14[i]<RSI_LO and not desc_l: return 'BUY'
    return None

# ============ EXECUTION ============
def get_balance():
    try: return float(info.user_state(WALLET)['marginSummary']['accountValue'])
    except: return 0
def get_mid(coin):
    try: return float(info.all_mids()[coin])
    except: return None
def get_position(coin):
    try:
        for p in info.user_state(WALLET).get('assetPositions', []):
            if p['position']['coin']==coin and float(p['position']['szi'])!=0:
                return {'size':float(p['position']['szi']), 'pnl':float(p['position']['unrealizedPnl']), 'entry':float(p['position']['entryPx'])}
    except: pass
    return None

def calc_size(equity, px):
    raw = equity * RISK_PCT * LEV / px
    if raw>=100: return round(raw,0)
    if raw>=10:  return round(raw,1)
    if raw>=1:   return round(raw,2)
    if raw>=0.1: return round(raw,3)
    return round(raw,4)

def place_market(coin, is_buy, size):
    px = get_mid(coin)
    if not px: return None
    slip_px = round(px*1.01,4) if is_buy else round(px*0.99,4)
    try:
        r = exchange.order(coin, is_buy, size, slip_px, {'limit':{'tif':'Ioc'}}, reduce_only=False)
        log(f"ORDER {coin} {'BUY' if is_buy else 'SELL'} {size}@{slip_px}: {r}")
        return r
    except Exception as e:
        log(f"order err {coin}: {e}"); return None

def close_position(coin):
    pos = get_position(coin)
    if not pos: return
    is_buy_to_close = pos['size']<0
    size = abs(pos['size'])
    px = get_mid(coin)
    if not px: return
    slip_px = round(px*1.01,4) if is_buy_to_close else round(px*0.99,4)
    try:
        r = exchange.order(coin, is_buy_to_close, size, slip_px, {'limit':{'tif':'Ioc'}}, reduce_only=True)
        log(f"CLOSE {coin} {size}@{slip_px}: {r}")
    except Exception as e: log(f"close err {coin}: {e}")

# ============ MAIN LOOP ============
def process_coin(coin, state, equity):
    candles = fetch_candles(coin)
    sig = compute_signal(candles)
    if not sig: return
    cluster = state['clusters'].get(coin, {'side':None, 'legs':0, 'opened_at':0})
    px = get_mid(coin)
    if not px: return
    new_side = 'L' if sig=='BUY' else 'S'
    # Flip
    if cluster['side'] and cluster['side']!=new_side:
        log(f"{coin} FLIP {cluster['side']}→{new_side}, closing")
        close_position(coin)
        size = calc_size(equity, px)
        place_market(coin, sig=='BUY', size)
        state['clusters'][coin] = {'side':new_side,'legs':1,'opened_at':int(time.time())}
    # Add leg
    elif cluster['side']==new_side and cluster['legs']<MAX_LEGS:
        log(f"{coin} ADD {new_side} leg {cluster['legs']+1}/{MAX_LEGS}")
        size = calc_size(equity, px)
        place_market(coin, sig=='BUY', size)
        state['clusters'][coin]['legs'] += 1
    # Open fresh
    elif not cluster['side']:
        log(f"{coin} OPEN {new_side} leg 1/{MAX_LEGS}")
        size = calc_size(equity, px)
        place_market(coin, sig=='BUY', size)
        state['clusters'][coin] = {'side':new_side,'legs':1,'opened_at':int(time.time())}

def funding_check(state):
    for coin in list(state['clusters'].keys()):
        cluster = state['clusters'][coin]
        if not cluster['side']: continue
        pos = get_position(coin)
        if not pos:
            state['clusters'][coin] = {'side':None,'legs':0,'opened_at':0}; continue
        hours_held = (time.time() - cluster['opened_at']) / 3600
        funding_cost = abs(pos['size']) * pos['entry'] * 0.0001 * hours_held
        if pos['pnl']>0 and funding_cost > pos['pnl'] * FUNDING_EXIT:
            log(f"{coin} FUNDING EXIT cost={funding_cost:.2f} pnl={pos['pnl']:.2f}")
            close_position(coin)
            state['clusters'][coin] = {'side':None,'legs':0,'opened_at':0}

def main():
    log(f"PreCog starting | wallet={WALLET} | coins={len(COINS)} | lev={LEV}x risk={RISK_PCT}")
    while True:
        try:
            state = load_state()
            equity = get_balance()
            log(f"--- tick equity=${equity:.2f} clusters={sum(1 for c in state['clusters'].values() if c.get('side'))} ---")
            for coin in COINS:
                try: process_coin(coin, state, equity)
                except Exception as e: log(f"err {coin}: {e}\n{traceback.format_exc()}")
                time.sleep(0.5)
            log(f"--- tick complete, scanned {len(COINS)} coins ---")
            funding_check(state)
            save_state(state)
        except Exception as e: log(f"tick err: {e}\n{traceback.format_exc()}")
        time.sleep(LOOP_SEC)

if __name__=='__main__': main()
