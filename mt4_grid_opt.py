#!/usr/bin/env python3
"""MT4 Grid Optimizer — Per-Ticker Gate Optimization
Fetches candle data from yfinance, runs precog signal engine,
tests all gate combinations, finds optimal config per ticker.
"""
import json, sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
import yfinance as yf
import numpy as np
from datetime import datetime, timedelta

# yfinance symbol mapping for MT4 tickers
YF_MAP = {
    'XAUUSD':'GC=F','XAGUSD':'SI=F','XPTUSD':'PL=F','XPDUSD':'PA=F',
    'SPOTCRUDE':'CL=F','SPOTBRENT':'BZ=F','NATGAS':'NG=F',
    'EURUSD':'EURUSD=X','GBPUSD':'GBPUSD=X','USDJPY':'JPY=X',
    'AUDUSD':'AUDUSD=X','NZDUSD':'NZDUSD=X','USDCAD':'CAD=X',
    'USDCHF':'CHF=X','EURGBP':'EURGBP=X','GBPNZD':'GBPNZD=X',
    'GBPJPY':'GBPJPY=X','AUDCAD':'AUDCAD=X','AUDCHF':'AUDCHF=X',
    'AUDNZD':'AUDNZD=X','AUDJPY':'AUDJPY=X','CADCHF':'CADCHF=X',
    'CADJPY':'CADJPY=X','CHFJPY':'CHFJPY=X','EURAUD':'EURAUD=X',
    'EURCAD':'EURCAD=X','EURCHF':'EURCHF=X','GBPAUD':'GBPAUD=X',
    'GBPCHF':'GBPCHF=X','NZDCAD':'NZDCAD=X',
    'NAS100':'^IXIC','US30':'^DJI','US500':'^GSPC',
    'US2000':'^RUT','GER40':'^GDAXI','UK100':'^FTSE',
    'JPN225':'^N225','HK50':'^HSI',
    'COPPER':'HG=F','CORN':'ZC=F','WHEAT':'ZW=F',
    'SOYBEANS':'ZS=F','COFFEE':'KC=F','SUGAR':'SB=F',
}

# Precog signal engine (simplified for BT — same core logic as precog.py)
def ema(data, period):
    if len(data) < period: return [0]*len(data)
    result = [0.0]*len(data)
    result[period-1] = sum(data[:period])/period
    m = 2/(period+1)
    for i in range(period, len(data)):
        result[i] = data[i]*m + result[i-1]*(1-m)
    return result

def rsi(closes, period=14):
    if len(closes) < period+1: return [50]*len(closes)
    result = [50.0]*len(closes)
    gains=[]; losses=[]
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    avg_g = sum(gains[:period])/period
    avg_l = sum(losses[:period])/period
    for i in range(period, len(gains)):
        avg_g = (avg_g*(period-1)+gains[i])/period
        avg_l = (avg_l*(period-1)+losses[i])/period
        rs = avg_g/avg_l if avg_l>0 else 100
        result[i+1] = 100 - 100/(1+rs)
    return result

def signal_from_candles(candles, sens=1, rsi_p=10, pivot_lb=8):
    """Generate BUY/SELL signal from OHLCV candles."""
    if not candles or len(candles) < 60: return None, None
    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)
    rsi_vals = rsi(closes, rsi_p)
    
    i = len(candles)-1
    if i < 50: return None, None
    
    # Pivot detection
    def is_pivot_high(idx, lb):
        if idx < lb or idx >= len(highs)-1: return False
        return all(highs[idx] >= highs[idx-j] for j in range(1,lb+1))
    def is_pivot_low(idx, lb):
        if idx < lb or idx >= len(lows)-1: return False
        return all(lows[idx] <= lows[idx-j] for j in range(1,lb+1))
    
    # Find recent pivots
    last_ph = None; last_pl = None
    for j in range(i-1, max(i-30,pivot_lb), -1):
        if last_ph is None and is_pivot_high(j, pivot_lb): last_ph = j
        if last_pl is None and is_pivot_low(j, pivot_lb): last_pl = j
        if last_ph and last_pl: break
    
    # BOS detection
    bos_bull = last_ph and closes[i] > highs[last_ph]
    bos_bear = last_pl and closes[i] < lows[last_pl]
    
    # Signal
    sig = None
    if bos_bull and ema9[i] > ema21[i] and rsi_vals[i] < 80:
        sig = 'BUY'
    elif bos_bear and ema9[i] < ema21[i] and rsi_vals[i] > 20:
        sig = 'SELL'
    
    return sig, candles[i][0] if sig else None

def apply_gate(candles, sig, price, gb, gs, cloud, body, glb):
    """Apply gate config. Returns True if signal passes."""
    if not candles or len(candles) < glb+1: return True
    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    ema50_vals = ema(closes, 50)
    i = len(candles)-1
    
    # Chase gate buy
    if gb and sig == 'BUY':
        window_hi = max(highs[i-glb:i])
        if price > window_hi: return False
    # Chase gate sell
    if gs and sig == 'SELL':
        window_lo = min(lows[i-glb:i])
        if price < window_lo: return False
    # Cloud gate
    if cloud:
        if sig == 'BUY' and price < ema50_vals[i]: return False
        if sig == 'SELL' and price > ema50_vals[i]: return False
    # Body filter
    if body > 0:
        o, h, l, c = candles[i][1], candles[i][2], candles[i][3], candles[i][4]
        rng = h - l
        if rng > 0 and abs(c - o) / rng < body: return False
    return True

def fetch_candles(ticker, period='30d', interval='15m'):
    """Fetch candle data from yfinance."""
    yf_sym = YF_MAP.get(ticker)
    if not yf_sym:
        print(f"  {ticker}: no yfinance mapping, skip")
        return None
    try:
        df = yf.download(yf_sym, period=period, interval=interval, progress=False)
        if df.empty or len(df) < 100:
            print(f"  {ticker}: insufficient data ({len(df)} bars)")
            return None
        candles = []
        for idx, row in df.iterrows():
            ts = int(idx.timestamp()*1000)
            o = float(row['Open'].iloc[0]) if hasattr(row['Open'],'iloc') else float(row['Open'])
            h = float(row['High'].iloc[0]) if hasattr(row['High'],'iloc') else float(row['High'])
            l = float(row['Low'].iloc[0]) if hasattr(row['Low'],'iloc') else float(row['Low'])
            c = float(row['Close'].iloc[0]) if hasattr(row['Close'],'iloc') else float(row['Close'])
            v = float(row['Volume'].iloc[0]) if hasattr(row['Volume'],'iloc') else float(row['Volume'])
            candles.append([ts, o, h, l, c, v])
        return candles
    except Exception as e:
        print(f"  {ticker}: fetch error: {e}")
        return None

TP_PCT = 0.003  # 0.3% TP for BT evaluation

def backtest(candles, gb, gs, cloud, body, glb):
    """Run BT with gate config. Returns (wins, losses, total)."""
    wins = 0; losses = 0
    i = 60
    cooldown = 0
    while i < len(candles) - 10:
        sig, _ = signal_from_candles(candles[:i+1])
        if not sig or i < cooldown:
            i += 1; continue
        price = candles[i][4]
        if not apply_gate(candles[:i+1], sig, price, gb, gs, cloud, body, glb):
            i += 1; continue
        # Evaluate: did price move TP_PCT in signal direction within 10 bars?
        entry = price
        won = False
        for j in range(1, min(11, len(candles)-i)):
            future = candles[i+j]
            if sig == 'BUY':
                if (future[2] - entry)/entry >= TP_PCT: won = True; break
            else:
                if (entry - future[3])/entry >= TP_PCT: won = True; break
        if won: wins += 1
        else: losses += 1
        cooldown = i + 3  # 3-bar cooldown
        i += 1
    return wins, losses, wins+losses

def grid_search(ticker, candles):
    """Test all gate combinations, find optimal config."""
    # Base (no gates)
    bw, bl, bt = backtest(candles, False, False, False, 0, 20)
    base_wr = bw/bt*100 if bt > 0 else 0
    print(f"  {ticker}: BASE {bt} trades, {base_wr:.1f}% WR")
    
    best = {'wr': base_wr, 'n': bt, 'cfg': None}
    
    # Grid: gb × gs × cloud × body × glb
    for gb in [False, True]:
        for gs in [False, True]:
            for cloud_on in [False, True]:
                for body_val in [0, 0.3]:
                    for glb_val in [15, 20]:
                        if not gb and not gs and not cloud_on and body_val == 0:
                            continue  # skip no-gate config
                        w, l, t = backtest(candles, gb, gs, cloud_on, body_val, glb_val)
                        if t < 5: continue  # need minimum trades
                        wr = w/t*100
                        if wr > best['wr'] or (wr == best['wr'] and t > best['n']):
                            best = {'wr': wr, 'n': t, 'cfg': {
                                'gb':gb,'gs':gs,'cloud':cloud_on,
                                'body':body_val,'glb':glb_val,'tp':TP_PCT
                            }}
    
    if best['cfg']:
        print(f"  {ticker}: OPT  {best['n']} trades, {best['wr']:.1f}% WR (+{best['wr']-base_wr:.1f}pp) cfg={best['cfg']}")
    else:
        print(f"  {ticker}: NO IMPROVEMENT found")
    
    return {
        'ticker': ticker, 'base_wr': round(base_wr,1), 'base_n': bt,
        'opt_wr': round(best['wr'],1), 'opt_n': best['n'],
        'cfg': best['cfg'], 'delta_wr': round(best['wr']-base_wr,1)
    }

if __name__ == '__main__':
    ALL_MT4 = list(YF_MAP.keys())
    print(f"MT4 Grid Optimizer — {len(ALL_MT4)} tickers")
    print(f"Gate grid: gb×gs×cloud×body×glb = 2×2×2×2×2 = 32 configs per ticker")
    print(f"{'='*60}")
    
    results = []
    gates = {}
    
    for ticker in ALL_MT4:
        print(f"\n[{ALL_MT4.index(ticker)+1}/{len(ALL_MT4)}] {ticker}")
        candles = fetch_candles(ticker)
        if not candles: continue
        
        r = grid_search(ticker, candles)
        results.append(r)
        if r['cfg']:
            gates[ticker] = r['cfg']
        time.sleep(0.5)  # rate limit yfinance
    
    # Save results
    with open('mt4_grid_results.json','w') as f:
        json.dump(results, f, indent=2)
    with open('mt4_ticker_gates.json','w') as f:
        json.dump(gates, f, indent=2)
    
    # Summary
    print(f"\n{'='*60}")
    print(f"RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"{'Ticker':<12} {'Base WR':>8} {'Opt WR':>8} {'Delta':>7} {'Trades':>7} {'Gate':>6}")
    print(f"{'─'*12} {'─'*8} {'─'*8} {'─'*7} {'─'*7} {'─'*6}")
    for r in sorted(results, key=lambda x: -x['opt_wr']):
        g = '✓' if r['cfg'] else '·'
        print(f"{r['ticker']:<12} {r['base_wr']:>7.1f}% {r['opt_wr']:>7.1f}% {r['delta_wr']:>+6.1f}% {r['opt_n']:>7} {g:>6}")
    
    under75 = [r for r in results if r['opt_wr'] < 75]
    over90 = [r for r in results if r['opt_wr'] >= 90]
    print(f"\n  ≥90% WR: {len(over90)} tickers")
    print(f"  <75% WR: {len(under75)} tickers (NEED ATTENTION)")
    print(f"\nSaved: mt4_grid_results.json + mt4_ticker_gates.json")
