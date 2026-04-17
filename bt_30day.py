#!/usr/bin/env python3
"""30-Day Backtest — Real Per-Ticker Gate Data"""
import json, random, sys
random.seed(2026)

gates = json.load(open('ticker_gates.json'))
grid = json.load(open('grid_results.json'))

grid_map = {}
for r in grid:
    t = r['ticker'].replace('.P','').replace('USDT','').replace('USD','')
    t = t.split(',')[0].strip()
    if t.startswith('CRYPTOCAP_'): t = t.replace('CRYPTOCAP_','')
    if t not in grid_map or r['opt_wr'] > grid_map[t]['opt_wr']:
        grid_map[t] = r

HL_COINS = [
    'SOL','LINK','UNI','ENS','AAVE','POL','SAND','APT','MON','COMP',
    'AERO','LIT','SPX','PEPE','BONK','SHIB','MORPHO','JUP','XRP',
    'SUSHI','ADA','WLD','PUMP','PENGU','FARTCOIN',
    'AIXBT','AVAX','PENDLE','TAO','WIF',
    'BTC','BNB','DOT','ATOM','SUI','LDO','INJ','UMA','ALGO',
    'BLUR','VVV','APE','OP','TON','TIA','LTC','MOODENG',
    'AR','GALA','VIRTUAL',
]

MT4_TICKERS = {
    'XAUUSD': {'class':'metal','pip_val':1.0,'avg_move':55,'avg_loss':25,'signals_day':1.5},
    'XAGUSD': {'class':'metal','pip_val':0.50,'avg_move':45,'avg_loss':20,'signals_day':1.0},
    'XPTUSD': {'class':'metal','pip_val':0.10,'avg_move':30,'avg_loss':15,'signals_day':0.5},
    'XPDUSD': {'class':'metal','pip_val':0.10,'avg_move':35,'avg_loss':18,'signals_day':0.5},
    'SPOTCRUDE': {'class':'energy','pip_val':0.10,'avg_move':25,'avg_loss':12,'signals_day':1.0,'swap':0.50},
    'SPOTBRENT': {'class':'energy','pip_val':0.10,'avg_move':25,'avg_loss':12,'signals_day':1.0,'swap':0.50},
    'NATGAS': {'class':'energy','pip_val':0.10,'avg_move':20,'avg_loss':10,'signals_day':0.8,'swap':0.30},
    'EURUSD': {'class':'fx','pip_val':0.10,'avg_move':15,'avg_loss':8,'signals_day':1.5},
    'GBPUSD': {'class':'fx','pip_val':0.10,'avg_move':18,'avg_loss':10,'signals_day':1.5},
    'USDJPY': {'class':'fx','pip_val':0.10,'avg_move':15,'avg_loss':8,'signals_day':1.5},
    'AUDUSD': {'class':'fx','pip_val':0.10,'avg_move':12,'avg_loss':7,'signals_day':1.0},
    'NZDUSD': {'class':'fx','pip_val':0.10,'avg_move':12,'avg_loss':7,'signals_day':1.0},
    'USDCAD': {'class':'fx','pip_val':0.10,'avg_move':12,'avg_loss':7,'signals_day':1.0},
    'USDCHF': {'class':'fx','pip_val':0.10,'avg_move':12,'avg_loss':7,'signals_day':0.8},
    'EURGBP': {'class':'fx','pip_val':0.10,'avg_move':10,'avg_loss':6,'signals_day':0.8},
    'GBPNZD': {'class':'fx','pip_val':0.10,'avg_move':22,'avg_loss':12,'signals_day':0.8},
    'GBPJPY': {'class':'fx','pip_val':0.10,'avg_move':25,'avg_loss':14,'signals_day':0.8},
    'AUDCAD': {'class':'fx','pip_val':0.10,'avg_move':10,'avg_loss':6,'signals_day':0.5},
    'AUDCHF': {'class':'fx','pip_val':0.10,'avg_move':10,'avg_loss':6,'signals_day':0.5},
    'AUDNZD': {'class':'fx','pip_val':0.10,'avg_move':8,'avg_loss':5,'signals_day':0.5},
    'AUDJPY': {'class':'fx','pip_val':0.10,'avg_move':15,'avg_loss':8,'signals_day':0.5},
    'CADCHF': {'class':'fx','pip_val':0.10,'avg_move':8,'avg_loss':5,'signals_day':0.3},
    'CADJPY': {'class':'fx','pip_val':0.10,'avg_move':12,'avg_loss':7,'signals_day':0.5},
    'CHFJPY': {'class':'fx','pip_val':0.10,'avg_move':12,'avg_loss':7,'signals_day':0.3},
    'EURAUD': {'class':'fx','pip_val':0.10,'avg_move':18,'avg_loss':10,'signals_day':0.8},
    'EURCAD': {'class':'fx','pip_val':0.10,'avg_move':12,'avg_loss':7,'signals_day':0.5},
    'EURCHF': {'class':'fx','pip_val':0.10,'avg_move':8,'avg_loss':5,'signals_day':0.3},
    'GBPAUD': {'class':'fx','pip_val':0.10,'avg_move':22,'avg_loss':12,'signals_day':0.8},
