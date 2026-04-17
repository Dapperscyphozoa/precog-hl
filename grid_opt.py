#!/usr/bin/env python3
"""Grid optimizer — find optimal gate config per ticker to maximize WR."""
import csv, os, glob, re, json, time
from multiprocessing import Pool

def load(fname):
    rows=[]
    with open(fname) as f:
        r=csv.reader(f); hdr=next(r)
        cb=cs=ceb=ces=ccf=ccs=-1
        for i,h in enumerate(hdr):
            hs=h.strip()
            if hs=='Buy Signal': cb=i
            elif hs=='Sell Signal': cs=i
            elif hs=='Exit Buy': ceb=i
            elif hs=='Exit Sell': ces=i
            elif hs=='EMA Cloud Fast': ccf=i
            elif hs=='EMA Cloud Slow': ccs=i
        if cb<0: return []
        for row in r:
            try:
                rows.append({'h':float(row[2]),'l':float(row[3]),'c':float(row[4]),'o':float(row[1]),
                   'buy':bool(row[cb] and row[cb].strip() not in ('','0')),
                   'sell':bool(row[cs] and row[cs].strip() not in ('','0')),
                   'exitB':bool(ceb>=0 and len(row)>ceb and row[ceb] and row[ceb].strip() not in ('','0')),
                   'exitS':bool(ces>=0 and len(row)>ces and row[ces] and row[ces].strip() not in ('','0')),
                   'cf':float(row[ccf]) if ccf>=0 and len(row)>ccf and row[ccf].strip() else None,
                   'cs':float(row[ccs]) if ccs>=0 and len(row)>ccs and row[ccs].strip() else None})
            except: continue
    return rows

def bt(rows, gb=False, gs=False, glb=20, cloud=False, tp=None, body=0.0, momo=0, mcl=0):
    pos=None; w=0; l=0; cl=0
    for i,r in enumerate(rows):
        if pos:
            if tp and pos['s']=='L' and r['h']>=pos['e']*(1+tp): w+=1; cl=0; pos=None; continue
            elif tp and pos['s']=='S' and r['l']<=pos['e']*(1-tp): w+=1; cl=0; pos=None; continue
            if pos['s']=='L' and r['exitB']:
                if r['c']>pos['e']: w+=1; cl=0
                else: l+=1; cl+=1
                pos=None; continue
            elif pos['s']=='S' and r['exitS']:
                if r['c']<pos['e']: w+=1; cl=0
                else: l+=1; cl+=1
                pos=None; continue
            if (pos['s']=='L' and r['sell']) or (pos['s']=='S' and r['buy']):
                pnl=(r['c']-pos['e'])/pos['e'] if pos['s']=='L' else (pos['e']-r['c'])/pos['e']
                if pnl>0: w+=1; cl=0
                else: l+=1; cl+=1
                pos=None
        if not pos:
            tb=r['buy']; ts=r['sell']
            if mcl>0 and cl>=mcl: tb=ts=False
            if cloud and r['cf'] and r['cs']:
                if tb and r['cf']<r['cs']: tb=False
                if ts and r['cf']>r['cs']: ts=False
            if (gb or gs) and i>=glb:
                hi=max(rows[j]['h'] for j in range(i-glb,i)); lo=min(rows[j]['l'] for j in range(i-glb,i))
                if hi>lo:
                    if gb and tb and r['c']>hi: tb=False
                    if gs and ts and r['c']<lo: ts=False
            if body>0 and (tb or ts):
                br=r['h']-r['l']
                if br>0 and abs(r['c']-r['o'])/br<body: tb=ts=False
            if momo>0 and i>=momo:
                if tb and r['c']<=rows[i-momo]['c']: tb=False
                if ts and r['c']>=rows[i-momo]['c']: ts=False
            if tb: pos={'s':'L','e':r['c']}
            elif ts: pos={'s':'S','e':r['c']}
    n=w+l
    return n, w/n*100 if n else 0

def optimize_ticker(args):
    path, ticker = args
    rows = load(path)
    if len(rows)<50: return None
    best_wr=0; best_n=0; best_cfg={}; base_n=0; base_wr=0
    # Base (no gates)
    base_n, base_wr = bt(rows)
    if base_n<2: return None
    # Grid search
    for gb in [False, True]:
     for gs in [False, True]:
      for cloud in [False, True]:
       for tp in [None, 0.003, 0.005, 0.008, 0.01]:
        for body in [0.0, 0.3]:
         for momo in [0, 3]:
          for mcl in [0, 3]:
           for glb in [15, 20]:
            n, wr = bt(rows, gb=gb, gs=gs, glb=glb, cloud=cloud, tp=tp, body=body, momo=momo, mcl=mcl)
            if n>=2 and wr>best_wr:
                best_wr=wr; best_n=n
                best_cfg={'gb':gb,'gs':gs,'glb':glb,'cloud':cloud,'tp':tp,'body':body,'momo':momo,'mcl':mcl}
            elif n>=2 and wr==best_wr and n>best_n:
                best_n=n
                best_cfg={'gb':gb,'gs':gs,'glb':glb,'cloud':cloud,'tp':tp,'body':body,'momo':momo,'mcl':mcl}
    return {'ticker':ticker,'base_n':base_n,'base_wr':round(base_wr,1),
            'opt_n':best_n,'opt_wr':round(best_wr,1),'cfg':best_cfg,
            'delta_wr':round(best_wr-base_wr,1)}

if __name__=='__main__':
    paths = sorted(glob.glob(os.path.expanduser('~/Downloads/*.csv')))
    tasks = []
    for p in paths:
        m=re.match(r'(?:BINANCE|BYBIT|KUCOIN|OKX|PEPPERSTONE|PIONEX|BINANCEUS|CRYPTO)_([^,]+)',os.path.basename(p))
        tk=m.group(1)[:22] if m else os.path.basename(p)[:22]
        tasks.append((p,tk))

    print(f"Optimizing {len(tasks)} tickers across grid (640 configs each)...")
    t0=time.time()
    with Pool(8) as pool:
        results = list(pool.map(optimize_ticker, tasks))
    results = [r for r in results if r]
    results.sort(key=lambda x:-x['opt_wr'])
    elapsed=time.time()-t0

    print(f"\nDone in {elapsed:.1f}s — {len(results)} tickers optimized\n")
    print(f"{'Ticker':<24} {'Base':>5} {'BaseWR':>7} {'Opt':>5} {'OptWR':>7} {'Δ':>5}  Best Config")
    print('-'*100)
    
    t_base=0; w_base=0; t_opt=0; w_opt=0
    above90=0; above85=0; above80=0; t90=0; t85=0; t80=0
    for r in results:
        cfg=r['cfg']
        cfg_str=[]
        if cfg.get('gb'): cfg_str.append('gate_buy')
        if cfg.get('gs'): cfg_str.append('gate_sell')
        if cfg.get('cloud'): cfg_str.append('cloud')
        if cfg.get('tp'): cfg_str.append(f"tp={cfg['tp']*100:.1f}%")
        if cfg.get('body')>0: cfg_str.append(f"body>{cfg['body']}")
        if cfg.get('momo')>0: cfg_str.append(f"momo{cfg['momo']}")
        if cfg.get('mcl')>0: cfg_str.append(f"cb{cfg['mcl']}")
        if cfg.get('glb')!=20: cfg_str.append(f"lb{cfg['glb']}")
        flag='***' if r['opt_wr']>=90 else ' **' if r['opt_wr']>=85 else '  *' if r['opt_wr']>=80 else ''
        print(f"{r['ticker']:<24} {r['base_n']:>5} {r['base_wr']:>6.1f}% {r['opt_n']:>5} {r['opt_wr']:>6.1f}% {r['delta_wr']:>+4.1f}  {', '.join(cfg_str)} {flag}")
        t_base+=r['base_n']; w_base+=int(r['base_n']*r['base_wr']/100)
        t_opt+=r['opt_n']; w_opt+=int(r['opt_n']*r['opt_wr']/100)
        if r['opt_wr']>=90: above90+=1; t90+=r['opt_n']
        if r['opt_wr']>=85: above85+=1; t85+=r['opt_n']
        if r['opt_wr']>=80: above80+=1; t80+=r['opt_n']

    days=max(len(load(t[0]))/96 for t in tasks if load(t[0]))
    print(f"\nBASE:  {t_base} trades {w_base/t_base*100:.1f}% WR ({t_base/days:.0f}/day)")
    print(f"OPT:   {t_opt} trades {w_opt/t_opt*100:.1f}% WR ({t_opt/days:.0f}/day)")
    print(f"\n>=90%: {above90} tickers {t90} trades ({t90/days:.1f}/day)")
    print(f">=85%: {above85} tickers {t85} trades ({t85/days:.1f}/day)")
    print(f">=80%: {above80} tickers {t80} trades ({t80/days:.1f}/day)")

    # Save results JSON
    with open('/Users/zecvic/Desktop/precog-hl/grid_results.json','w') as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to grid_results.json")
