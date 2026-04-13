#!/usr/bin/env python3
"""PreCog Confluence — single-shot top/bottom catcher with structural gate"""
import os, json, time, traceback
from datetime import datetime
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

WALLET     = os.environ['HYPERLIQUID_ACCOUNT']
PRIV_KEY   = os.environ['HL_PRIVATE_KEY']
STATE_PATH = '/var/data/precog_state.json'

COINS = ['BTC', 'ETH', 'ATOM', 'DYDX', 'SOL', 'AVAX', 'BNB', 'APE', 'OP', 'LTC', 'ARB', 'DOGE', 'INJ', 'SUI', 'kPEPE', 'CRV', 'LDO', 'LINK', 'STX', 'CFX', 'GMX', 'SNX', 'XRP', 'BCH', 'APT', 'AAVE', 'COMP', 'WLD', 'YGG', 'TRX', 'kSHIB', 'UNI', 'SEI', 'RUNE', 'ZRO', 'DOT', 'BANANA', 'TRB', 'FTT', 'ARK', 'BIGTIME', 'KAS', 'BLUR', 'TIA', 'BSV', 'ADA', 'TON', 'MINA', 'POLYX', 'GAS', 'PENDLE', 'FET', 'NEAR', 'MEME', 'ORDI', 'NEO', 'ZEN', 'FIL', 'PYTH', 'SUSHI', 'IMX', 'kBONK', 'GMT', 'SUPER', 'USTC', 'JUP', 'kLUNC', 'RSR', 'GALA', 'JTO', 'ACE', 'MAV', 'WIF', 'CAKE', 'PEOPLE', 'ENS', 'ETC', 'XAI', 'MANTA', 'UMA', 'ONDO', 'ALT', 'ZETA', 'DYM', 'MAVIA', 'W', 'STRK', 'TAO', 'AR', 'kFLOKI', 'BOME', 'ETHFI', 'ENA', 'MNT', 'TNSR', 'SAGA', 'MERL', 'HBAR', 'POPCAT', 'EIGEN', 'REZ', 'NOT', 'TURBO', 'BRETT', 'IO', 'ZK', 'BLAST', 'MEW', 'RENDER', 'POL', 'CELO', 'HMSTR', 'SCR', 'kNEIRO', 'GOAT', 'MOODENG', 'GRASS', 'PURR', 'PNUT', 'XLM', 'CHILLGUY', 'SAND', 'IOTA', 'ALGO', 'HYPE', 'ME', 'MOVE', 'VIRTUAL', 'PENGU', 'USUAL', 'FARTCOIN', 'AIXBT', 'ZEREBRO', 'BIO', 'GRIFFAIN', 'SPX', 'S', 'MORPHO', 'TRUMP', 'MELANIA', 'ANIME', 'VINE', 'VVV', 'BERA', 'TST', 'LAYER', 'IP', 'KAITO', 'NIL', 'PAXG', 'PROMPT', 'BABY', 'WCT', 'HYPER', 'ZORA', 'INIT', 'DOOD', 'NXPC', 'SOPH', 'RESOLV', 'SYRUP', 'PUMP', 'PROVE', 'YZY', 'XPL', 'WLFI', 'LINEA', 'SKY', 'ASTER', 'AVNT', 'STBL', '0G', 'HEMI', 'APEX', '2Z', 'ZEC']
        'rsi_hi':   60 + s['rsi']*1.5,
        'rsi_lo':   40 - s['rsi']*1.5,
        'wick':     0.05 + s['wick']*0.04,
        'struct_n': 99 if s['struct']<2 else max(2, round(7 - s['struct']*0.5)),
        'pivot_lb': max(2, round(8 - s['sens']*0.5)),
        'vol_mult': 1.0 + (s['vol']-1)*0.15,
        'cd':       s['cd']
    }
SP = derive(SELL); BP = derive(BUY)

LEV = 5; RISK_PCT = 0.20; LOOP_SEC = 900
MAX_POSITIONS = 25; MAX_SAME_SIDE = 18; BTC_VOL_THRESHOLD = 0.03

info = Info(constants.MAINNET_API_URL, skip_ws=True)
account = Account.from_key(PRIV_KEY)
exchange = Exchange(account, constants.MAINNET_API_URL, account_address=WALLET)

def log(m): print(f"[{datetime.utcnow().isoformat()}] {m}", flush=True)

def load_state():
    s = {'positions':{}, 'cooldowns':{}}
    try:
        with open(STATE_PATH) as f: loaded = json.load(f)
        if 'positions' in loaded: s['positions'] = loaded['positions']
        if 'cooldowns' in loaded: s['cooldowns'] = loaded['cooldowns']
    except: pass
    return s
    if 'positions' not in s: s['positions']={}
    if 'cooldowns' not in s: s['cooldowns']={}
    return s
def save_state(s):
    os.makedirs('/var/data', exist_ok=True)
    with open(STATE_PATH,'w') as f: json.dump(s,f)

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
def sma(a,n):
    r=[None]*len(a)
    for i in range(n-1,len(a)): r[i]=sum(a[i-n+1:i+1])/n
    return r

def fetch(coin, n_bars=300):
    end=int(time.time()*1000); start=end-n_bars*15*60*1000
    try:
        d=info.candles_snapshot(coin,'15m',start,end)
        return [(int(c['t']),float(c['o']),float(c['h']),float(c['l']),float(c['c']),float(c['v'])) for c in d]
    except Exception as e:
        log(f"candle err {coin}: {e}"); return []

def signal(candles, last_sell_bar, last_buy_bar):
    if len(candles)<100: return None,None
    o=[c[1] for c in candles]; h=[c[2] for c in candles]; l=[c[3] for c in candles]
    cl=[c[4] for c in candles]; v=[c[5] for c in candles]
    N=len(cl); r14=rsi_calc(cl,14); vavg=sma(v,20)
    last_h=[]; last_l=[]
    for i in range(SP['pivot_lb'], N-SP['pivot_lb']):
        if h[i]==max(h[i-SP['pivot_lb']:i+SP['pivot_lb']+1]):
            last_h.append(h[i])
            if len(last_h)>SP['struct_n']: last_h.pop(0)
    for i in range(BP['pivot_lb'], N-BP['pivot_lb']):
        if l[i]==min(l[i-BP['pivot_lb']:i+BP['pivot_lb']+1]):
            last_l.append(l[i])
            if len(last_l)>BP['struct_n']: last_l.pop(0)
    asc_h = SELL['struct']>=2 and len(last_h)>=SP['struct_n'] and all(last_h[j]<last_h[j+1] for j in range(SP['struct_n']-1))
    desc_l= BUY['struct']>=2  and len(last_l)>=BP['struct_n'] and all(last_l[j]>last_l[j+1]  for j in range(BP['struct_n']-1))
    i = N-1
    if r14[i] is None or vavg[i] is None: return None, None
    br = h[i]-l[i]
    uw = (h[i]-max(o[i],cl[i]))/br if br>0 else 0
    lw = (min(o[i],cl[i])-l[i])/br if br>0 else 0
    vol_ok_s = SELL['vol']==1 or v[i]>=vavg[i]*SP['vol_mult']
    vol_ok_b = BUY['vol']==1  or v[i]>=vavg[i]*BP['vol_mult']
    sell = h[i]==max(h[max(0,i-SP['lb']+1):i+1]) and uw>=SP['wick'] and r14[i]>SP['rsi_hi'] and vol_ok_s and not asc_h and (i-last_sell_bar)>SP['cd']
    buy  = l[i]==min(l[max(0,i-BP['lb']+1):i+1]) and lw>=BP['wick'] and r14[i]<BP['rsi_lo'] and vol_ok_b and not desc_l and (i-last_buy_bar)>BP['cd']
    if sell: return 'SELL', i
    if buy:  return 'BUY', i
    return None, None

def get_balance():
    try: return float(info.user_state(WALLET)['marginSummary']['accountValue'])
    except: return 0
def get_mid(coin):
    try: return float(info.all_mids()[coin])
    except: return None
def get_position(coin):
    try:
        for p in info.user_state(WALLET).get('assetPositions',[]):
            if p['position']['coin']==coin and float(p['position']['szi'])!=0:
                return {'size':float(p['position']['szi']), 'pnl':float(p['position']['unrealizedPnl']), 'entry':float(p['position']['entryPx'])}
    except: pass
    return None

def calc_size(equity, px, risk_mult=1.0):
    raw = equity * RISK_PCT * risk_mult * LEV / px
    if raw>=100: return round(raw,0)
    if raw>=10:  return round(raw,1)
    if raw>=1:   return round(raw,2)
    if raw>=0.1: return round(raw,3)
    return round(raw,4)

def place(coin, is_buy, size):
    px=get_mid(coin)
    if not px: return
    slip = round(px*1.01,4) if is_buy else round(px*0.99,4)
    try:
        r=exchange.order(coin,is_buy,size,slip,{'limit':{'tif':'Ioc'}},reduce_only=False)
        log(f"ORDER {coin} {'BUY' if is_buy else 'SELL'} {size}@{slip}: {r}")
    except Exception as e: log(f"order err {coin}: {e}")

def close(coin):
    pos=get_position(coin)
    if not pos: return
    is_buy=pos['size']<0; size=abs(pos['size']); px=get_mid(coin)
    if not px: return
    slip=round(px*1.01,4) if is_buy else round(px*0.99,4)
    try:
        r=exchange.order(coin,is_buy,size,slip,{'limit':{'tif':'Ioc'}},reduce_only=True)
        log(f"CLOSE {coin} {size}@{slip}: {r}")
    except Exception as e: log(f"close err {coin}: {e}")

def process(coin, state, equity, risk_mult=1.0):
    candles=fetch(coin)
    last_s=state['cooldowns'].get(coin+'_sell',-1000)
    last_b=state['cooldowns'].get(coin+'_buy',-1000)
    sig,bar=signal(candles,last_s,last_b)
    if not sig: return
    cur=state['positions'].get(coin)
    # Position concentration caps
    open_pos = {k:v for k,v in state['positions'].items() if v}
    if not cur and len(open_pos) >= MAX_POSITIONS:
        log(f"{coin} {sig} SKIP (max {MAX_POSITIONS} positions)"); return
    same_side_count = sum(1 for v in open_pos.values() if v == ('L' if sig=='BUY' else 'S'))
    if not cur and same_side_count >= MAX_SAME_SIDE:
        log(f"{coin} {sig} SKIP (side cap {MAX_SAME_SIDE})"); return
    log(f"{coin} SIGNAL: {sig} (risk_mult={risk_mult})")
    if sig=='SELL':
        state['cooldowns'][coin+'_sell']=bar
        if cur=='L': close(coin)
        if cur!='S':
            px=get_mid(coin); 
            if px: place(coin, False, calc_size(equity, px, risk_mult)); state['positions'][coin]='S'
    else:
        state['cooldowns'][coin+'_buy']=bar
        if cur=='S': close(coin)
        if cur!='L':
            px=get_mid(coin)
            if px: place(coin, True, calc_size(equity, px, risk_mult)); state['positions'][coin]='L'

def main():
    log(f"PreCog v3 | wallet={WALLET} | coins={len(COINS)} | {LEV}x/{int(RISK_PCT*100)}% | max_pos={MAX_POSITIONS} | side_cap={MAX_SAME_SIDE}")
    log(f"SELL params: {SP}")
    log(f"BUY params:  {BP}")
    while True:
        try:
            state=load_state()
            equity=get_balance()
            # BTC vol throttle check
            risk_mult = 1.0
            try:
                btc_c = fetch('BTC')
                if len(btc_c) >= 4:
                    recent_4 = btc_c[-4:]  # last 4 x 15m bars = 1h
                    hi = max(c[2] for c in recent_4); lo = min(c[3] for c in recent_4)
                    btc_range = (hi-lo)/lo
                    if btc_range > BTC_VOL_THRESHOLD:
                        risk_mult = 0.5
                        log(f"BTC vol {btc_range*100:.1f}% > threshold — risk halved")
            except Exception as e: log(f"vol check err: {e}")
            log(f"--- tick eq=${equity:.2f} positions={sum(1 for v in state['positions'].values() if v)} risk_mult={risk_mult} ---")
            for c in COINS:
                try: process(c, state, equity, risk_mult)
                except Exception as e: log(f"err {c}: {e}")
                time.sleep(0.5)
            save_state(state)
            log(f"--- tick complete ---")
        except Exception as e: log(f"tick err: {e}\n{traceback.format_exc()}")
        time.sleep(LOOP_SEC)

if __name__=='__main__': main()
