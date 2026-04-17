#!/usr/bin/env python3
"""PreCog Webhook Receiver — TradingView ProAlgo → HL execution with heavy gating

Architecture:
  TradingView ProAlgo alert → POST /signal → gate chain → HL maker order
  
Gate chain (all must pass):
  1. Cloud filter: EMA20 > EMA50 for longs, < for shorts (fetched from HL)
  2. Chase gate: reject if price already beyond 20-bar range
  3. Volume gate: reject if vol < 1.2x 20-bar average
  4. Body filter: reject doji/indecision entry candles (body < 30% of range)
  5. Multi-TF: H1 EMA trend must agree with signal direction

TradingView alert message format (JSON):
  {"coin":"SOL","action":"buy","price":88.5,"tf":"15"}
  {"coin":"SOL","action":"sell","price":89.2,"tf":"15"}
  {"coin":"SOL","action":"exit_buy","price":89.0,"tf":"15"}
  {"coin":"SOL","action":"exit_sell","price":88.8,"tf":"15"}
"""
import os, json, time, math, random, traceback, threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

# ═══════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════
WALLET     = os.environ.get('HYPERLIQUID_ACCOUNT','')
PRIV_KEY   = os.environ.get('HL_PRIVATE_KEY','')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET','precog_webhook_2026')
PORT       = int(os.environ.get('PORT', '10000'))

LEV = 10
RISK_PCT = 0.05
TP_PCT = 0.008           # +0.8% underlying → lock winner
CLOUD_EXIT_BUFFER = 0.002 # 0.2% through EMA50 to trigger cloud exit
MAX_HOLD_SEC = 4 * 3600
MAX_POSITIONS = 20

# Gate config
CHASE_LOOKBACK = 20
VOLUME_MULT = 1.2         # vol must be > 1.2x 20-bar avg
BODY_RATIO_MIN = 0.30     # reject doji (body < 30% of range)

info = Info(constants.MAINNET_API_URL, skip_ws=True)
account = Account.from_key(PRIV_KEY)
exchange = Exchange(account, constants.MAINNET_API_URL, account_address=WALLET)

def log(m): print(f"[{datetime.utcnow().isoformat()}] {m}", flush=True)

# ═══════════════════════════════════════════════════════
# HL HELPERS (shared with precog.py)
# ═══════════════════════════════════════════════════════
_META = None
def _load_meta():
    global _META
    if not _META:
        try: _META = {u['name']: int(u.get('szDecimals',0)) for u in info.meta()['universe']}
        except: _META = {}
    return _META

def round_price(coin, px):
    m=_load_meta(); szD=m.get(coin,2); max_dec=max(0,6-szD)
    if px>0:
        sig_scale=10**(5-int(math.floor(math.log10(abs(px))))-1)
        px=round(px*sig_scale)/sig_scale
    return round(px, max_dec)

def round_size(coin, sz):
    m=_load_meta(); return round(sz, m.get(coin,2))

def get_mid(coin):
    try: return float(info.all_mids()[coin])
    except: return None

def get_balance():
    try: return float(info.user_state(WALLET)['marginSummary']['accountValue'])
    except: return 0

def get_positions():
    out={}
    try:
        for p in info.user_state(WALLET).get('assetPositions',[]):
            pos=p['position']; sz=float(pos.get('szi',0))
            if sz!=0: out[pos['coin']]={'size':sz,'entry':float(pos['entryPx']),'pnl':float(pos['unrealizedPnl'])}
    except: pass
    return out

def set_lev(coin):
    try: exchange.update_leverage(LEV, coin, is_cross=False)
    except: pass

def fetch_candles(coin, interval='5m', n_bars=300):
    end=int(time.time()*1000); start=end-n_bars*5*60*1000
    for attempt in range(3):
        try:
            d=info.candles_snapshot(coin, interval, start, end)
            return [(int(c['t']),float(c['o']),float(c['h']),float(c['l']),float(c['c']),float(c['v'])) for c in d]
        except:
            time.sleep(0.5+random.random())
    return []

# ═══════════════════════════════════════════════════════
# GATE CHAIN — all must pass for signal to execute
# ═══════════════════════════════════════════════════════
def gate_chain(coin, action, price):
    """Returns (pass:bool, reason:str). action='buy'|'sell'."""
    reasons = []
    candles = fetch_candles(coin, '5m', 100)
    if len(candles) < 60:
        return False, 'insufficient_data'
    
    h=[c[2] for c in candles]; l=[c[3] for c in candles]
    cl=[c[4] for c in candles]; vol=[c[5] for c in candles]
    
    # EMA cloud
    def ema(v,n):
        if len(v)<n: return [None]*len(v)
        k=2/(n+1); o=[None]*len(v); s=sum(v[:n])/n; o[n-1]=s
        for i in range(n,len(v)): o[i]=v[i]*k+o[i-1]*(1-k)
        return o
    ef=ema(cl,20); es=ema(cl,50)
    i=len(cl)-1
    if not ef[i] or not es[i]: return False, 'ema_na'
