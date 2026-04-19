"""Comprehensive signal tuner. Runs as persistent Render background worker.
Phase 1: base grid (pivot × RSI × SL × trail × side × trend_gates) = 7,800 combos
Phase 2: top 50 × 9 filters ON/OFF (single additions) = 450 evals
Phase 3: greedy stack top-3 filters onto top 20 = 160 evals
Writes top configs to /var/data/tuner_results.json every 100 combos.
"""
import json, os, time, urllib.request, itertools, traceback
import numpy as np

COINS = ['BTC','ETH','SOL','XRP','ADA','AVAX','LINK','BNB','AAVE','INJ',
         'DOGE','ARB','OP','HYPE','FARTCOIN','kBONK','TRB','LDO','MOODENG','APE',
         'TAO','APT','JUP','SAND','TON','UMA','ALGO','DOT','WLD','PENDLE',
         'BLUR','LIT','COMP','AIXBT','MORPHO','AR','VVV','SUSHI','TIA','ATOM']

OUT = '/var/data/tuner_results.json' if os.path.isdir('/var/data') else '/tmp/tuner_results.json'
LOG = '/var/data/tuner.log' if os.path.isdir('/var/data') else '/tmp/tuner.log'
CACHE = '/var/data/tuner_cache' if os.path.isdir('/var/data') else '/tmp/tuner_cache'
os.makedirs(CACHE, exist_ok=True)

def L(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG,'a') as f: f.write(line+'\n')
    except: pass

def fetch(coin, tf, days):
    NOW=int(time.time()*1000); ST=NOW-days*86400*1000
    cache_f = f'{CACHE}/{coin}_{tf}_{days}.json'
    if os.path.exists(cache_f) and os.path.getmtime(cache_f) > time.time() - 3600*6:
        return json.load(open(cache_f))
    req=urllib.request.Request('https://api.hyperliquid.xyz/info',
        data=json.dumps({'type':'candleSnapshot','req':{'coin':coin,'interval':tf,'startTime':ST,'endTime':NOW}}).encode(),
        headers={'Content-Type':'application/json'})
    for t in range(5):
        try:
            d = json.loads(urllib.request.urlopen(req,timeout=30).read())
            json.dump(d, open(cache_f,'w'))
            return d
        except Exception as e:
            if '429' in str(e): time.sleep(3+t*2); continue
            return None
    return None

# ============ INDICATORS ============
def rsi_np(c,p=14):
    d=np.diff(c); g=np.maximum(d,0); lo=np.maximum(-d,0)
    ag=np.full(len(c),np.nan); al=np.full(len(c),np.nan)
    if len(c)<=p: return ag
    ag[p]=g[:p].mean(); al[p]=lo[:p].mean()
    for i in range(p+1,len(c)):
        ag[i]=(ag[i-1]*(p-1)+g[i-1])/p; al[i]=(al[i-1]*(p-1)+lo[i-1])/p
    rs=ag/np.where(al==0,1e-10,al); return 100-100/(1+rs)

def ema_np(a,p):
    e=np.full(len(a),np.nan); k=2/(p+1)
    if len(a)<p: return e
    e[p-1]=a[:p].mean()
    for i in range(p,len(a)): e[i]=a[i]*k+e[i-1]*(1-k)
    return e

def atr_np(h,l,c,p=14):
    tr=np.maximum(h-l, np.maximum(np.abs(h-np.roll(c,1)), np.abs(l-np.roll(c,1))))
    tr[0]=h[0]-l[0]
    a=np.full(len(c),np.nan)
    if len(c)<p: return a
    a[p-1]=tr[:p].mean()
    for i in range(p,len(c)): a[i]=(a[i-1]*(p-1)+tr[i])/p
    return a

def adx_np(h,l,c,p=14):
    # Simplified ADX
    up=np.diff(h,prepend=h[0]); dn=-np.diff(l,prepend=l[0])
    pdm=np.where((up>dn)&(up>0), up, 0); ndm=np.where((dn>up)&(dn>0), dn, 0)
    tr=np.maximum(h-l, np.maximum(np.abs(h-np.roll(c,1)), np.abs(l-np.roll(c,1))))
    tr[0]=h[0]-l[0]
    atr=ema_np(tr,p); pdi=100*ema_np(pdm,p)/np.where(atr==0,1,atr); ndi=100*ema_np(ndm,p)/np.where(atr==0,1,atr)
    dx=100*np.abs(pdi-ndi)/np.where(pdi+ndi==0,1,pdi+ndi)
    return ema_np(dx,p)

def bb_np(c, p=20, mult=2):
    sma=np.full(len(c),np.nan); std=np.full(len(c),np.nan)
    for i in range(p-1,len(c)):
        sma[i]=c[i-p+1:i+1].mean(); std[i]=c[i-p+1:i+1].std()
    return sma, sma+mult*std, sma-mult*std

def macd_np(c, fast=12, slow=26, sig=9):
    ef=ema_np(c,fast); es=ema_np(c,slow); m=ef-es
    s=ema_np(m,sig)
    return m, s, m-s

def stoch_np(h,l,c,p=14):
    k=np.full(len(c),np.nan)
    for i in range(p-1,len(c)):
        hh=h[i-p+1:i+1].max(); ll=l[i-p+1:i+1].min()
        k[i]=100*(c[i]-ll)/(hh-ll) if hh>ll else 50
    return k

def vol_ratio(v, p=20):
    r=np.full(len(v),np.nan)
    for i in range(p,len(v)):
        avg=v[i-p:i].mean()
        r[i]=v[i]/avg if avg>0 else 1
    return r

def idx_map(ts_lo, ts_hi):
    out=np.zeros(len(ts_lo),dtype=int); j=0
    for i,t in enumerate(ts_lo):
        while j+1<len(ts_hi) and ts_hi[j+1]<=t: j+=1
        out[i]=j
    return out

# ============ BACKTEST ============
def bt(prepared, P):
    """prepared: dict with numpy arrays. P: param dict."""
    FEE=0.00045
    all_trades=[]
    for coin, d in prepared.items():
        h,l,c,o,v,ts = d['h'],d['l'],d['c'],d['o'],d['v'],d['ts']
        r=d['rsi']; ema4h=d['ema4h']; c4h=d['c4h']; tf4h=d['tf4h']
        adx=d['adx']; atr=d['atr']; bb_u=d['bb_u']; bb_l=d['bb_l']
        macd_m=d['macd_m']; macd_s=d['macd_s']; stoch=d['stoch']; vr=d['vr']

        pos=None; lbi=-9999; lsi=-9999; PLB=P['plb']; CD=P['cd']
        N=len(c); start=max(PLB,30)
        for i in range(start,N):
            if pos:
                if pos['s']=='L':
                    pk=max(pos['pk'],h[i]); pos['pk']=pk
                    if l[i]<=pos['e']*(1-P['sl']): all_trades.append({'s':'L','pnl':-P['sl']-2*FEE}); pos=None
                    elif pk>pos['e']*(1+P['trl']) and l[i]<=pk*(1-P['trl']):
                        all_trades.append({'s':'L','pnl':(pk*(1-P['trl'])-pos['e'])/pos['e']-2*FEE}); pos=None
                else:
                    tr=min(pos['pk'],l[i]); pos['pk']=tr
                    if h[i]>=pos['e']*(1+P['sl']): all_trades.append({'s':'S','pnl':-P['sl']-2*FEE}); pos=None
                    elif tr<pos['e']*(1-P['trl']) and h[i]>=tr*(1+P['trl']):
                        all_trades.append({'s':'S','pnl':(pos['e']-tr*(1+P['trl']))/pos['e']-2*FEE}); pos=None
            if pos: continue
            if np.isnan(r[i]): continue
            is_ph=h[i]==h[max(0,i-PLB):i+1].max()
            is_pl=l[i]==l[max(0,i-PLB):i+1].min()
            sell_ok = P['side'] in ('both','short') and is_ph and r[i]>P['rhi'] and (i-lsi)>CD
            buy_ok  = P['side'] in ('both','long')  and is_pl and r[i]<P['rlo'] and (i-lbi)>CD
            if not(sell_ok or buy_ok): continue
            # V3: 4H EMA9 trend
            if P.get('v3') and tf4h is not None:
                hi=tf4h[i]
                if hi>=9 and not np.isnan(ema4h[hi]):
                    if sell_ok and c4h[hi]>ema4h[hi]: sell_ok=False
                    if buy_ok  and c4h[hi]<ema4h[hi]: buy_ok=False
            # ADX strength
            if P.get('adx') and not np.isnan(adx[i]) and adx[i] < P['adx_min']: sell_ok=False; buy_ok=False
            # Volume filter
            if P.get('vol') and not np.isnan(vr[i]) and vr[i] < P['vol_min']: sell_ok=False; buy_ok=False
            # ATR gate — require min volatility
            if P.get('atr_gate') and not np.isnan(atr[i]):
                if atr[i]/c[i] < P['atr_min']: sell_ok=False; buy_ok=False
            # Bollinger band extremes
            if P.get('bb') and not np.isnan(bb_u[i]):
                if sell_ok and c[i] < bb_u[i]: sell_ok=False  # only sell at/above upper band
                if buy_ok  and c[i] > bb_l[i]: buy_ok=False   # only buy at/below lower band
            # MACD alignment
            if P.get('macd') and not np.isnan(macd_m[i]):
                if sell_ok and macd_m[i] > macd_s[i]: sell_ok=False
                if buy_ok  and macd_m[i] < macd_s[i]: buy_ok=False
            # Stochastic extremes
            if P.get('stoch') and not np.isnan(stoch[i]):
                if sell_ok and stoch[i] < 80: sell_ok=False
                if buy_ok  and stoch[i] > 20: buy_ok=False
            # BOS: break of structure (last N bars high/low broken)
            if P.get('bos'):
                lb=P['bos_lb']
                if i>lb:
                    hi=h[i-lb:i].max(); lo=l[i-lb:i].min()
                    if sell_ok and c[i] > lo: sell_ok=False  # require break of recent low to sell-continuation
                    if buy_ok  and c[i] < hi: buy_ok=False
            if sell_ok: pos={'s':'S','e':c[i],'pk':c[i]}; lsi=i
            elif buy_ok: pos={'s':'L','e':c[i],'pk':c[i]}; lbi=i
    return all_trades

def score(trades):
    if not trades or len(trades)<10: return {'n':len(trades),'wr':0,'pnl':0,'score':-999}
    n=len(trades); w=sum(1 for t in trades if t['pnl']>0)
    pnl=sum(t['pnl'] for t in trades)*100
    wr=w/n*100
    sc = pnl * (n**0.35) if pnl>0 else pnl
    return {'n':n,'wr':wr,'pnl':pnl,'score':sc}

def prepare_data(data):
    out={}
    for coin,tfs in data.items():
        d5=tfs.get('5m'); d4=tfs.get('4h')
        if not d5 or len(d5)<500: continue
        h=np.array([float(x['h']) for x in d5]); l=np.array([float(x['l']) for x in d5])
        c=np.array([float(x['c']) for x in d5]); o=np.array([float(x['o']) for x in d5])
        v=np.array([float(x['v']) for x in d5]); ts=np.array([int(x['t']) for x in d5])
        rsi=rsi_np(c); adx=adx_np(h,l,c); atr=atr_np(h,l,c)
        bb_m,bb_u,bb_l=bb_np(c); macd_m,macd_s,_=macd_np(c); stoch=stoch_np(h,l,c); vr=vol_ratio(v)
        c4h=tf4h_map=ema4h=None
        if d4 and len(d4)>=30:
            c4h=np.array([float(x['c']) for x in d4]); ts4=np.array([int(x['t']) for x in d4])
            ema4h=ema_np(c4h,9); tf4h_map=idx_map(ts,ts4)
        out[coin]={'h':h,'l':l,'c':c,'o':o,'v':v,'ts':ts,'rsi':rsi,'adx':adx,'atr':atr,
                   'bb_u':bb_u,'bb_l':bb_l,'macd_m':macd_m,'macd_s':macd_s,'stoch':stoch,'vr':vr,
                   'c4h':c4h,'ema4h':ema4h,'tf4h':tf4h_map}
    return out

def checkpoint(best, phase, combo_count, total_combos, elapsed, extra=None):
    best.sort(key=lambda x:-x['score'])
    payload={'phase':phase,'completed':combo_count,'total':total_combos,
             'elapsed_sec':int(elapsed),'top':best[:30]}
    if extra: payload.update(extra)
    try: json.dump(payload, open(OUT,'w'), indent=2)
    except Exception as e: L(f"checkpoint err: {e}")

def main_loop():
    L("BOOT tuner_worker")
    L(f"OUT={OUT} LOG={LOG} CACHE={CACHE}")
    while True:
        try:
            # FETCH
            L(f"Fetching {len(COINS)} coins × 2 TFs")
            data={}
            for c in COINS:
                data[c]={}
                for tf,days in [('5m',21),('4h',90)]:
                    d=fetch(c,tf,days)
                    if d: data[c][tf]=d
                    time.sleep(0.3)
            prepared=prepare_data(data)
            L(f"Prepared {len(prepared)} coins with indicators")

            # PHASE 1: base grid
            grid = {
                'plb':  [3,5,10,15,17,25,29,36,50,75,100],
                'rhi':  [65,70,75,80,85],
                'rlo':  [35,30,25,20,15],
                'sl':   [0.008, 0.012, 0.02],
                'trl':  [0.003, 0.005, 0.007, 0.01],
                'cd':   [10, 30, 60],
                'side': ['both','long','short'],
                'v3':   [True, False],
            }
            keys=list(grid.keys())
            combos=list(itertools.product(*[grid[k] for k in keys]))
            L(f"PHASE 1: {len(combos)} base combos")
            t0=time.time(); best=[]
            for idx,combo in enumerate(combos):
                P={k:combo[i] for i,k in enumerate(keys)}
                if P['rhi']<=50 or P['rlo']>=50: continue
                # defaults for filter params
                P.update({'adx':False,'vol':False,'atr_gate':False,'bb':False,'macd':False,'stoch':False,'bos':False})
                trades=bt(prepared,P)
                s=score(trades); s['params']=dict(P); best.append(s)
                if (idx+1)%200==0:
                    checkpoint(best,'phase1',idx+1,len(combos),time.time()-t0)
                    L(f"P1 {idx+1}/{len(combos)} | top: n={best[0]['n']} WR={best[0]['wr']:.1f}% pnl={best[0]['pnl']:+.1f}%")
                    best.sort(key=lambda x:-x['score']); best=best[:500]
            checkpoint(best,'phase1_done',len(combos),len(combos),time.time()-t0)
            best.sort(key=lambda x:-x['score'])
            L(f"PHASE 1 done. Top base: n={best[0]['n']} WR={best[0]['wr']:.1f}% pnl={best[0]['pnl']:+.1f}%")

            # PHASE 2: add each filter ON/OFF to top 30
            top30=[dict(b['params']) for b in best[:30]]
            filters=[
                ('adx',{'adx':True,'adx_min':20}),
                ('vol',{'vol':True,'vol_min':1.3}),
                ('atr_gate',{'atr_gate':True,'atr_min':0.002}),
                ('bb',{'bb':True}),
                ('macd',{'macd':True}),
                ('stoch',{'stoch':True}),
                ('bos',{'bos':True,'bos_lb':20}),
            ]
            p2=[]
            for base in top30:
                for fname,fadd in filters:
                    P=dict(base); P.update(fadd)
                    trades=bt(prepared,P); s=score(trades); s['params']=dict(P); s['added']=fname
                    p2.append(s)
            checkpoint(p2,'phase2',len(p2),len(top30)*len(filters),time.time()-t0,
                       extra={'phase1_top':best[:10]})
            p2.sort(key=lambda x:-x['score'])
            L(f"PHASE 2 done. Top with filter: n={p2[0]['n']} WR={p2[0]['wr']:.1f}% pnl={p2[0]['pnl']:+.1f}% +{p2[0].get('added','')}")

            # PHASE 3: greedy stack top 3 filters onto top 10 configs
            top10=[dict(b['params']) for b in best[:10]]
            p3=[]
            for base in top10:
                current=dict(base)
                improvements=[]
                for _ in range(3):
                    best_add=None; best_pnl=score(bt(prepared,current))['pnl']
                    for fname,fadd in filters:
                        if current.get(fname): continue
                        P=dict(current); P.update(fadd)
                        s=score(bt(prepared,P))
                        if s['pnl']>best_pnl:
                            best_pnl=s['pnl']; best_add=(fname,fadd)
                    if not best_add: break
                    current.update(best_add[1]); improvements.append(best_add[0])
                s=score(bt(prepared,current)); s['params']=current; s['stack']=improvements
                p3.append(s)
            p3.sort(key=lambda x:-x['score'])
            checkpoint(p3,'phase3',len(p3),len(top10),time.time()-t0,
                       extra={'phase1_top':best[:10],'phase2_top':p2[:10]})
            L(f"PHASE 3 done. Best stack: n={p3[0]['n']} WR={p3[0]['wr']:.1f}% pnl={p3[0]['pnl']:+.1f}% stack={p3[0].get('stack')}")
            L(f"Cycle complete in {int(time.time()-t0)}s. Sleeping 30min then re-fetching.")
            time.sleep(1800)
        except Exception as e:
            L(f"CYCLE ERR {e}\n{traceback.format_exc()}")
            time.sleep(60)

if __name__=='__main__':
    main_loop()
