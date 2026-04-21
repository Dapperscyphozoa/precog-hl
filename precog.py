#!/usr/bin/env python3
"""PreCog v8.28 — 50-coin universe + 48 MT4 tickers

Dual signal engine:
  1. Internal BOS/pivot/RSI → per-ticker gated (73 configs)
  2. TV Trend Buy/Sell webhooks → per-ticker gated + EMA confirm (EA)

10% risk | 10x lev | 0.7% trail | 1% SL | native HL stop orders
"""
import os, json, time, random, traceback
from datetime import datetime
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account
import threading
from queue import Queue
from flask import Flask, request as flask_request, jsonify, Response
import bybit_ws
import percoin_configs
import orderbook_ws
import news_filter
import wall_confluence
import risk_ladder
import signal_persistence
import profit_lock
import leverage_map
import wall_bounce
import liquidation_ws
import bybit_lead
import funding_filter
import btc_correlation
import confidence
import spoof_detection
import session_scaler
import whale_filter
import cvd_ws
import oi_tracker
import funding_arb

# ═══════════════════════════════════════════════════════
# TRADE LOG — persistent CSV for real WR tracking
# ═══════════════════════════════════════════════════════
TRADE_LOG = '/var/data/trades.csv'

# Per-coin kill-switch: disable a coin if rolling 10-trade WR < 35%
COIN_KILL_MIN_N = 10
COIN_KILL_WR_THRESHOLD = 0.35
COIN_KILL_COOLDOWN_SEC = 12 * 3600  # 12h

def coin_disabled(coin, state):
    k = state.get('coin_kill', {}).get(coin)
    if not k: return False
    return time.time() < k.get('until', 0)

def update_coin_wr(coin, win, state):
    h = state.setdefault('coin_hist', {}).setdefault(coin, [])
    h.append(1 if win else 0)
    if len(h) > COIN_KILL_MIN_N:
        h.pop(0)
    if len(h) >= COIN_KILL_MIN_N:
        wr = sum(h)/len(h)
        if wr < COIN_KILL_WR_THRESHOLD:
            state.setdefault('coin_kill', {})[coin] = {'until': time.time() + COIN_KILL_COOLDOWN_SEC, 'wr': wr}
            log(f"COIN KILL {coin}: rolling 10-trade WR {wr*100:.0f}% < {COIN_KILL_WR_THRESHOLD*100:.0f}% → disabled 12h")

def record_close(pos, coin, pnl_pct, state):
    """Record a closed trade. pnl_pct is already percent (e.g. -2.0 = -2%)."""
    if pnl_pct is None: return
    # Clamp to sanity range — SL caps at -2%, but leveraged wild fills can blow through
    pnl_pct = max(-10.0, min(50.0, float(pnl_pct)))
    win = pnl_pct > 0
    now = time.time()
    update_coin_wr(coin, win, state)
    stats = state.setdefault('stats', {
        'by_engine': {}, 'by_hour': {}, 'by_side': {}, 'by_coin': {},
        'by_conf': {}, 'total_wins': 0, 'total_losses': 0, 'total_pnl': 0.0
    })
    def bump(bucket_name, key):
        b = stats[bucket_name].setdefault(str(key), {'w':0,'l':0,'pnl':0.0})
        if win: b['w'] += 1
        else: b['l'] += 1
        b['pnl'] += pnl_pct  # already percent
    engine = pos.get('engine') or 'UNKNOWN'
    side   = pos.get('side','?')
    utc_h  = pos.get('utc_h', time.gmtime(now).tm_hour)
    conf   = pos.get('conf', 0)
    conf_bucket = '0-29' if conf<30 else '30-49' if conf<50 else '50-69' if conf<70 else '70+'
    bump('by_engine', engine)
    bump('by_hour',   utc_h)
    bump('by_side',   side)
    bump('by_coin',   coin)
    bump('by_conf',   conf_bucket)
    if win: stats['total_wins'] += 1
    else: stats['total_losses'] += 1
    stats['total_pnl'] += pnl_pct

def wr_to_mult(wr, n, min_n=5):
    """Adaptive size multiplier based on rolling WR. Never returns 0 (never blocks).
    <40%: 0.4x | 40-55%: 0.7x | 55-70%: 1.0x | 70%+: 1.3x
    Not enough data (<min_n): 1.0x (neutral)."""
    if n < min_n: return 1.0
    if wr < 0.40: return 0.4
    if wr < 0.55: return 0.7
    if wr < 0.70: return 1.0
    return 1.3

def adaptive_mult(coin, side, state):
    """Compose multiplier from per-coin × per-hour × per-side stats."""
    stats = state.get('stats', {})
    mult = 1.0
    # Per-coin
    b = stats.get('by_coin', {}).get(coin)
    if b:
        n = b.get('w',0)+b.get('l',0)
        wr = b.get('w',0)/n if n else 0
        mult *= wr_to_mult(wr, n, min_n=10)
    # Per-hour
    utc_h = str(time.gmtime().tm_hour)
    b = stats.get('by_hour', {}).get(utc_h)
    if b:
        n = b.get('w',0)+b.get('l',0)
        wr = b.get('w',0)/n if n else 0
        mult *= wr_to_mult(wr, n, min_n=15)
    # Per-side
    side_key = 'L' if side=='BUY' else 'S'
    b = stats.get('by_side', {}).get(side_key)
    if b:
        n = b.get('w',0)+b.get('l',0)
        wr = b.get('w',0)/n if n else 0
        mult *= wr_to_mult(wr, n, min_n=20)
    # Clamp 0.3-1.5
    return max(0.3, min(1.5, mult))

def log_trade(engine, coin, direction, entry, pnl, source, sl_pct=None):
    import csv
    try:
        os.makedirs('/var/data', exist_ok=True)
        exists = os.path.exists(TRADE_LOG)
        with open(TRADE_LOG, 'a', newline='') as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(['timestamp','engine','coin','direction','entry','pnl','source','sl_pct'])
            w.writerow([datetime.utcnow().isoformat(), engine, coin, direction, entry, pnl, source, sl_pct or ''])
    except Exception as e:
        pass  # don't crash on log failure

WALLET     = os.environ['HYPERLIQUID_ACCOUNT']
PRIV_KEY   = os.environ['HL_PRIVATE_KEY']
STATE_PATH = '/var/data/precog_state.json'
KILL_FILE  = '/var/data/KILL'

# ═══════════════════════════════════════════════════════
# WEBHOOK — receives DynaPro signals from TradingView
# ═══════════════════════════════════════════════════════
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'precog_dynapro_2026')
WEBHOOK_QUEUE = Queue()
# ═══════════════════════════════════════════════════════
# MT4 SIGNAL ROUTING — DynaPro webhook → Pepperstone EA
# ═══════════════════════════════════════════════════════
# MT4 PER-TICKER GATES — to be populated by grid optimizer
# Same approach as HL: per-ticker gate configs optimize WR from 53-65% → 85%+
# Load MT4 per-ticker gates from grid optimizer results
try:
    import json as _json
    with open(os.path.join(os.path.dirname(__file__), 'mt4_ticker_gates.json')) as _f:
        MT4_TICKER_GATES = _json.load(_f)
except Exception:
    MT4_TICKER_GATES = {}
# v4.9: structural zone confluence (OB/FVG/key levels via Yahoo candles)
try:
    import zones as _zones
    ZONES_ENABLED = True
except Exception as _e:
    _zones = None
    ZONES_ENABLED = False
# ============================================================
# v4.10: MT4 pullback gate (Yahoo 5m candles)
# Signal must be near 1h EMA20 with cooled RSI to pass.
# This filters away signals that fire mid-move (chase trades).
# ============================================================
_pb_cache = {}  # {ticker: (ts, candles_5m)}
_pb_ttl = 180  # 3min cache
MT4_PULLBACK_ENABLED = os.environ.get('MT4_PULLBACK_ENABLED', 'true').lower() == 'true'
PB_EMA = 20
PB_PROXIMITY = 0.015   # within 1.5% of 1h EMA20 (1h candles wider than 5m)
PB_RSI_HI = 60         # BUY: RSI < this
PB_RSI_LO = 40         # SELL: RSI > this

_YMAP_PB = {
    'XAUUSD':'GC=F','XAGUSD':'SI=F','XPTUSD':'PL=F','XPDUSD':'PA=F',
    'SPOTCRUDE':'CL=F','SPOTBRENT':'BZ=F','NATGAS':'NG=F','COPPER':'HG=F',
    'CORN':'ZC=F','WHEAT':'ZW=F','SOYBEANS':'ZS=F','SUGAR':'SB=F','COFFEE':'KC=F',
    'US30':'^DJI','US500':'^GSPC','NAS100':'^NDX','US2000':'^RUT',
    'GER40':'^GDAXI','UK100':'^FTSE','JPN225':'^N225','HK50':'^HSI',
    'VIX':'^VIX','USDX':'DX-Y.NYB',
}

def _fetch_pb_candles(clean_ticker):
    """Fetch 1h candles from Yahoo (7 days, always enough for EMA20).
    Returns list of (ts_ms, o, h, l, c) or None.
    NOTE: switched from 5m to 1h because Yahoo 5m is sparse over weekends/gaps,
    and EMA20 on 1h is what pullback_signal actually needs anyway.
    """
    ysym = _YMAP_PB.get(clean_ticker)
    if not ysym:
        if len(clean_ticker) == 6 and clean_ticker.isalpha():
            ysym = f"{clean_ticker}=X"
        else:
            return None
    now = time.time()
    cached = _pb_cache.get(clean_ticker)
    if cached and (now - cached[0] < _pb_ttl):
        return cached[1]
    try:
        import urllib.request as _ur
        end_ts = int(now)
        start_ts = end_ts - 86400 * 7  # 7 days — robust across weekends
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?period1={start_ts}&period2={end_ts}&interval=1h'
        req = _ur.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        data = _json.loads(_ur.urlopen(req, timeout=5).read())
        r = data['chart']['result'][0]
        ts_arr = r.get('timestamp', [])
        q = r.get('indicators',{}).get('quote',[{}])[0]
        candles = []
        for i, t in enumerate(ts_arr):
            c = q['close'][i] if i < len(q.get('close',[])) else None
            if c is None: continue
            o = q['open'][i] if q.get('open') and q['open'][i] is not None else c
            h = q['high'][i] if q.get('high') and q['high'][i] is not None else c
            l = q['low'][i]  if q.get('low')  and q['low'][i]  is not None else c
            candles.append((t*1000, o, h, l, c))
        if len(candles) >= 25:
            _pb_cache[clean_ticker] = (now, candles)
            return candles
        return None
    except Exception as _e:
        return None

def _mt4_pullback_check(clean_ticker, direction):
    """Returns (passed: bool, reason: str, meta: dict). Non-blocking on data fetch fail.
    Uses 1h candles directly (v4.11). Computes EMA20 + RSI14 on 1h close."""
    if not MT4_PULLBACK_ENABLED: return True, 'pullback_disabled', {}
    candles = _fetch_pb_candles(clean_ticker)
    if not candles or len(candles) < PB_EMA + 3:
        return True, f'no_candles (got {len(candles) if candles else 0})', {'candles': len(candles) if candles else 0}
    closes = [c[4] for c in candles]
    # 1H EMA20
    k = 2 / (PB_EMA + 1)
    ema = sum(closes[:PB_EMA]) / PB_EMA
    for cv in closes[PB_EMA:]:
        ema = cv*k + ema*(1-k)
    last_c = closes[-1]
    if ema <= 0: return True, 'bad_ema', {}
    dist = abs(last_c - ema) / ema
    # RSI(14) on 1h close
    gains=[]; losses=[]
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    pp = 14
    if len(gains) < pp: return True, 'insufficient_rsi', {'candles': len(candles)}
    ag = sum(gains[:pp])/pp; al = sum(losses[:pp])/pp
    for i in range(pp, len(gains)):
        ag = (ag*(pp-1)+gains[i])/pp
        al = (al*(pp-1)+losses[i])/pp
    rs = ag/al if al > 0 else 999
    rsi = 100 - 100/(1+rs)
    meta = {'dist_ema_pct': round(dist*100, 3), 'rsi1h': round(rsi, 1), 'ema1h': round(ema, 5), 'price': round(last_c, 5), 'candles': len(candles)}
    # Proximity check
    if dist > PB_PROXIMITY:
        return False, f'PB_FAR ({dist*100:.2f}% from EMA20, limit {PB_PROXIMITY*100:.1f}%)', meta
    # RSI cool check  
    if direction == 'BUY' and rsi >= PB_RSI_HI:
        return False, f'PB_RSI_HOT ({rsi:.0f} >= {PB_RSI_HI})', meta
    if direction == 'SELL' and rsi <= PB_RSI_LO:
        return False, f'PB_RSI_COLD ({rsi:.0f} <= {PB_RSI_LO})', meta
    return True, f'PB_OK (d={dist*100:.2f}% rsi={rsi:.0f})', meta

# ============================================================
# v4.10: OANDA fxOrderBook retail-sentiment fade gate
# Free data from OANDA fxlabs/positionbook CSV. Extreme retail
# positioning = contrarian signal.
# ============================================================
_oanda_cache = {}  # {pair: (ts, data)}
_oanda_ttl = 600  # 10min — data updates hourly
MT4_OANDA_ENABLED = os.environ.get('MT4_OANDA_ENABLED', 'true').lower() == 'true'
# v4.14: TradingView scanner symbol mapping (clean_ticker → (tv_symbol, endpoint))
# Endpoint determines which scanner API to hit (forex / cfd / america / global)
_TV_SYMBOL_MAP = {
    # Forex majors & crosses
    'EURUSD': ('FX_IDC:EURUSD','forex'), 'GBPUSD': ('FX_IDC:GBPUSD','forex'),
    'USDJPY': ('FX_IDC:USDJPY','forex'), 'USDCHF': ('FX_IDC:USDCHF','forex'),
    'USDCAD': ('FX_IDC:USDCAD','forex'), 'AUDUSD': ('FX_IDC:AUDUSD','forex'),
    'NZDUSD': ('FX_IDC:NZDUSD','forex'),
    'EURJPY': ('FX_IDC:EURJPY','forex'), 'GBPJPY': ('FX_IDC:GBPJPY','forex'),
    'EURGBP': ('FX_IDC:EURGBP','forex'), 'EURAUD': ('FX_IDC:EURAUD','forex'),
    'AUDJPY': ('FX_IDC:AUDJPY','forex'), 'CADJPY': ('FX_IDC:CADJPY','forex'),
    'CHFJPY': ('FX_IDC:CHFJPY','forex'),
    'AUDCAD': ('FX_IDC:AUDCAD','forex'), 'AUDCHF': ('FX_IDC:AUDCHF','forex'),
    'AUDNZD': ('FX_IDC:AUDNZD','forex'), 'CADCHF': ('FX_IDC:CADCHF','forex'),
    'EURCAD': ('FX_IDC:EURCAD','forex'), 'EURCHF': ('FX_IDC:EURCHF','forex'),
    'GBPAUD': ('FX_IDC:GBPAUD','forex'), 'GBPCHF': ('FX_IDC:GBPCHF','forex'),
    'GBPNZD': ('FX_IDC:GBPNZD','forex'), 'NZDCAD': ('FX_IDC:NZDCAD','forex'),
    'NZDJPY': ('FX_IDC:NZDJPY','forex'),
    # Metals
    'XAUUSD': ('OANDA:XAUUSD','cfd'),     'XAGUSD': ('TVC:SILVER','cfd'),
    'XPTUSD': ('TVC:PLATINUM','cfd'),     'XPDUSD': ('TVC:PALLADIUM','cfd'),
    # Energy
    'SPOTCRUDE': ('NYMEX:CL1!','global'), 'SPOTBRENT': ('ICEEUR:BRN1!','global'),
    'NATGAS': ('OANDA:NATGASUSD','cfd'),
    # Soft commodities & grains
    'COPPER': ('OANDA:XCUUSD','cfd'),
    'CORN': ('CBOT:ZC1!','global'),       'WHEAT': ('CBOT:ZW1!','global'),
    'SOYBEANS': ('CBOT:ZS1!','global'),
    'SUGAR': ('ICEUS:SB1!','global'),     'COFFEE': ('ICEUS:KC1!','global'),
    # Indices
    'US30': ('OANDA:US30USD','cfd'),      'US500': ('SP:SPX','cfd'),
    'NAS100': ('NASDAQ:NDX','america'),   'US2000': ('TVC:RUT','cfd'),
    'GER40': ('OANDA:DE30EUR','cfd'),     'UK100': ('TVC:UKX','cfd'),
    'JPN225': ('TVC:NI225','cfd'),        'HK50': ('OANDA:HK33HKD','cfd'),
    # Volatility & dollar index
    'VIX': ('CBOE:VIX','cfd'),            'USDX': ('TVC:DXY','cfd'),
}

OANDA_PAIRS = {
    'EURUSD':'EUR_USD','GBPUSD':'GBP_USD','USDJPY':'USD_JPY','USDCHF':'USD_CHF',
    'USDCAD':'USD_CAD','AUDUSD':'AUD_USD','NZDUSD':'NZD_USD',
    'EURJPY':'EUR_JPY','GBPJPY':'GBP_JPY','EURGBP':'EUR_GBP','EURAUD':'EUR_AUD',
    'AUDJPY':'AUD_JPY','CADJPY':'CAD_JPY','CHFJPY':'CHF_JPY',
    'AUDCAD':'AUD_CAD','AUDCHF':'AUD_CHF','AUDNZD':'AUD_NZD',
    'CADCHF':'CAD_CHF','EURCAD':'EUR_CAD','EURCHF':'EUR_CHF',
    'GBPAUD':'GBP_AUD','GBPCHF':'GBP_CHF','GBPNZD':'GBP_NZD',
    'NZDCAD':'NZD_CAD','NZDJPY':'NZD_JPY',
    'XAUUSD':'XAU_USD','XAGUSD':'XAG_USD',
    'SPOTCRUDE':'WTICO_USD','SPOTBRENT':'BCO_USD','NATGAS':'NATGAS_USD',
    'US30':'US30_USD','US500':'SPX500_USD','NAS100':'NAS100_USD',
    'UK100':'UK100_GBP','GER40':'DE30_EUR','JPN225':'JP225_USD',
}

def _fetch_oanda_sentiment(clean_ticker):
    """Fetch market sentiment. Returns tagged tuple or None:
    - ('tv', recommend_all) where recommend_all ∈ [-1, +1] (strong sell→strong buy)
    - (long_pct, short_pct) from MyFXBook/DailyFX retail positioning

    Sources tried in order:
    1. TradingView Scanner API (tech-indicator confluence) — works from cloud IPs
    2. MyFXBook community outlook (retail positioning) — often blocked on cloud
    3. DailyFX sentiment feed (retail positioning) — fallback

    Never blocks trades; returns None if all sources fail.
    """
    pair = OANDA_PAIRS.get(clean_ticker)
    if not pair: return None
    now = time.time()
    cached = _oanda_cache.get(pair)
    if cached and (now - cached[0] < _oanda_ttl):
        return cached[1]
    import urllib.request as _ur
    import re as _re

    # Source 1: TradingView Scanner (PRIMARY — works from cloud)
    tv_sym, tv_ep = _TV_SYMBOL_MAP.get(clean_ticker, (None, None))
    if tv_sym and tv_ep:
        try:
            url = f'https://scanner.tradingview.com/{tv_ep}/scan'
            payload = _json.dumps({
                "symbols":{"tickers":[tv_sym],"query":{"types":[]}},
                "columns":["Recommend.All"]
            }).encode()
            req = _ur.Request(url, data=payload, headers={
                'User-Agent':'Mozilla/5.0','Content-Type':'application/json'
            })
            resp = _ur.urlopen(req, timeout=4)
            data = _json.loads(resp.read())
            if data.get('totalCount', 0) > 0:
                rec_all = data['data'][0]['d'][0]
                if rec_all is not None:
                    # Clamp to [-1, +1]
                    rec_all = max(-1.0, min(1.0, float(rec_all)))
                    result = ('tv', rec_all)
                    _oanda_cache[pair] = (now, result)
                    return result
        except Exception:
            pass

    # Source 2: MyFXBook community outlook (retail positioning — often blocked on cloud)
    pair_url = pair.replace("_","")  # EUR_USD → EURUSD
    try:
        url = f'https://www.myfxbook.com/community/outlook/{pair_url}'
        req = _ur.Request(url, headers={
            'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Version/15.6.1 Safari/605.1.15',
            'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9',
            'Accept-Language':'en-US,en;q=0.5',
            'DNT':'1',
            'Connection':'keep-alive',
            'Upgrade-Insecure-Requests':'1',
        })
        resp = _ur.urlopen(req, timeout=5)
        html = resp.read().decode('utf-8', errors='ignore')
        m_short = _re.search(r'Short[^\d]*(\d+)\s*%', html)
        m_long  = _re.search(r'Long[^\d]*(\d+)\s*%', html)
        if m_short and m_long:
            long_pct = float(m_long.group(1))
            short_pct = float(m_short.group(1))
            if 0 < long_pct < 100 and 0 < short_pct < 100:
                result = (long_pct, short_pct)
                _oanda_cache[pair] = (now, result)
                return result
    except Exception:
        pass

    # Source 3: DailyFX retail positioning feed
    try:
        url2 = 'https://www.dailyfx.com/api/market-overview/sentiment'
        req = _ur.Request(url2, headers={
            'User-Agent':'Mozilla/5.0',
            'Accept':'application/json',
        })
        resp = _ur.urlopen(req, timeout=4)
        data = _json.loads(resp.read())
        for item in data.get('data', []):
            symbol = item.get('symbol','').replace('/','').upper()
            if symbol == pair_url:
                long_pct = item.get('longPercent') or item.get('long_pct')
                short_pct = item.get('shortPercent') or item.get('short_pct')
                if long_pct and short_pct:
                    result = (float(long_pct), float(short_pct))
                    _oanda_cache[pair] = (now, result)
                    return result
    except Exception:
        pass
    return None

_sent_log_throttle = {}  # {ticker: ts} — avoid log spam

def _mt4_sentiment_mult(clean_ticker, direction):
    """Returns size multiplier based on market sentiment.

    Handles two data formats from _fetch_oanda_sentiment:
    1. ('tv', recommend_all) — TradingView tech-indicator confluence ∈ [-1, +1]
       ALIGN with consensus → boost (confluence trade)
       COUNTER to consensus → reduce (fighting the tape)
    2. (long_pct, short_pct) — MyFXBook/DailyFX retail positioning
       Contrarian fade: extreme crowd long → don't BUY, don't SELL against extreme short

    Returns 1.0 if no data (never blocks).
    """
    if not MT4_OANDA_ENABLED: return 1.0
    data = _fetch_oanda_sentiment(clean_ticker)
    if not data:
        _now = time.time()
        if _now - _sent_log_throttle.get(clean_ticker, 0) > 60:
            _sent_log_throttle[clean_ticker] = _now
            log(f"SENT no_data for {clean_ticker} (all sources failed)")
        return 1.0

    # Format 1: TradingView tech consensus (CONFLUENCE logic — align with tape)
    if isinstance(data, tuple) and len(data) == 2 and data[0] == 'tv':
        rec = data[1]  # -1 strong sell ... +1 strong buy
        label = ('STRONG_BUY' if rec >= 0.5 else 'BUY' if rec >= 0.1
                 else 'STRONG_SELL' if rec <= -0.5 else 'SELL' if rec <= -0.1
                 else 'NEUTRAL')
        log(f"SENT TV {clean_ticker} {direction}: rec={rec:+.2f} ({label})")
        if direction == 'BUY':
            if rec >= 0.5:  return 1.5   # strong align with tape
            if rec >= 0.25: return 1.3
            if rec >= 0.1:  return 1.15
            if rec <= -0.5: return 0.5   # strong counter-trend
            if rec <= -0.25: return 0.7
            if rec <= -0.1: return 0.85
            return 1.0
        else:  # SELL
            if rec <= -0.5:  return 1.5
            if rec <= -0.25: return 1.3
            if rec <= -0.1:  return 1.15
            if rec >= 0.5:   return 0.5
            if rec >= 0.25:  return 0.7
            if rec >= 0.1:   return 0.85
            return 1.0

    # Format 2: Retail positioning % (CONTRARIAN fade at extremes)
    long_pct, short_pct = data
    log(f"SENT RETAIL {clean_ticker} {direction}: long={long_pct:.0f}% short={short_pct:.0f}%")
    if long_pct + short_pct == 0: return 1.0
    long_frac = long_pct / (long_pct + short_pct)
    if direction == 'BUY':
        if long_frac >= 0.80: return 0.5
        if long_frac >= 0.65: return 0.75
        if long_frac <= 0.30: return 1.3
        if long_frac <= 0.20: return 1.5
    else:
        if long_frac <= 0.20: return 0.5
        if long_frac <= 0.35: return 0.75
        if long_frac >= 0.70: return 1.3
        if long_frac >= 0.80: return 1.5
    return 1.0

MT4_QUEUE = []  # EA polls /mt4/signals every 10s
# v4.15: live PnL feedback from EA v5 trade-closed reports
MT4_CLOSED_RING = []
MT4_LIVE_STATS = {}
MT4_TICKET_META = {}
try:
    import os as _os_stats
    if _os_stats.path.exists('/var/data/mt4_stats.json'):
        with open('/var/data/mt4_stats.json') as _f:
            _saved = _json.load(_f)
            MT4_LIVE_STATS = _saved.get('stats', {})
except Exception: pass
MT4_BIAS = {'direction': '', 'ts': 0}

# --- MT4 queue persistence (HL-isolated; writes /var/data/mt4_queue.json) ---
MT4_QUEUE_FILE = '/var/data/mt4_queue.json'
MT4_STALE_SEC = 30   # v4.9: aggressive stale drop — signals older than 30s dropped

# ===== Webhook filter (HL-isolated) =====
MT4_FILTERS_ENABLED = os.environ.get('MT4_FILTERS_ENABLED', 'true').lower() == 'true'
MT4_COOLDOWN_SEC = 15 * 60  # 15min per-ticker cooldown
MT4_ATR_MIN_PCT = 0.08      # reject dead market
MT4_ATR_MAX_PCT = 2.50      # reject news spike
_mt4_last_signal = {}       # {clean_ticker: ts_seconds}
_mt4_atr_cache = {}         # {clean_ticker: (ts, atr_pct)}
_mt4_atr_cache_ttl = 600    # 10min TTL on ATR

def _mt4_atr_pct(clean_ticker):
    """Fetch 14-period ATR% via Yahoo. Returns None on failure (filter passes through)."""
    now = time.time()
    cached = _mt4_atr_cache.get(clean_ticker)
    if cached and (now - cached[0] < _mt4_atr_cache_ttl):
        return cached[1]
    YAHOO_MAP = {
        'XAUUSD':'GC=F','XAGUSD':'SI=F','XPTUSD':'PL=F','XPDUSD':'PA=F',
        'SPOTCRUDE':'CL=F','SPOTBRENT':'BZ=F','NATGAS':'NG=F',
        'COPPER':'HG=F','CORN':'ZC=F','WHEAT':'ZW=F','SOYBEANS':'ZS=F',
        'EURUSD':'EURUSD=X','GBPUSD':'GBPUSD=X','USDJPY':'JPY=X',
        'EURGBP':'EURGBP=X','GBPNZD':'GBPNZD=X','AUDCAD':'AUDCAD=X',
        'AUDUSD':'AUDUSD=X','USDCAD':'CAD=X','USDCHF':'CHF=X',
        'AUDCHF':'AUDCHF=X','AUDNZD':'AUDNZD=X','AUDJPY':'AUDJPY=X',
        'CADCHF':'CADCHF=X','CADJPY':'CADJPY=X','CHFJPY':'CHFJPY=X',
        'EURAUD':'EURAUD=X','EURCAD':'EURCAD=X','EURCHF':'EURCHF=X',
        'GBPAUD':'GBPAUD=X','GBPCHF':'GBPCHF=X','NZDUSD':'NZDUSD=X',
        'NZDCAD':'NZDCAD=X','NAS100':'^NDX','US30':'^DJI','US500':'^GSPC',
        'US2000':'^RUT','GER40':'^GDAXI','UK100':'^FTSE',
        'JPN225':'^N225','HK50':'^HSI','VIX':'^VIX',
    }
    ysym = YAHOO_MAP.get(clean_ticker)
    if not ysym:
        return None
    try:
        import urllib.request as _ur
        end_ts = int(now)
        start_ts = end_ts - 86400 * 3  # 3 days back
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?period1={start_ts}&period2={end_ts}&interval=1h'
        req = _ur.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        resp = _ur.urlopen(req, timeout=5)
        data = _json.loads(resp.read())
        result = data.get('chart',{}).get('result',[{}])[0]
        q = result.get('indicators',{}).get('quote',[{}])[0]
        highs = [h for h in q.get('high',[]) if h is not None]
        lows = [l for l in q.get('low',[]) if l is not None]
        closes = [c for c in q.get('close',[]) if c is not None]
        if len(closes) < 15:
            return None
        # ATR14 on most recent 14 bars
        trs = []
        for i in range(len(closes)-14, len(closes)):
            if i <= 0: continue
            tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
            trs.append(tr)
        if not trs:
            return None
        atr = sum(trs) / len(trs)
        atr_pct = (atr / closes[-1]) * 100
        _mt4_atr_cache[clean_ticker] = (now, atr_pct)
        return atr_pct
    except Exception as _e:
        return None

# v4.8: Full per-ticker gate pipeline (from grid optimization)
# Supports: invert, trail params, SL, session, VIX buckets, anchor correlation,
# RSI, counter-trend fade, time cut, hour block, VIX overlay (sentiment)

# === VIX sentiment cache ===
_vix_cache = {'ts': 0, 'value': None}
_vix_ttl = 300  # 5min

def _get_vix():
    now = time.time()
    if _vix_cache.get('value') is not None and (now - _vix_cache['ts'] < _vix_ttl):
        return _vix_cache['value']
    try:
        import urllib.request as _ur
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1h&range=1d'
        req = _ur.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        data = _json.loads(_ur.urlopen(req, timeout=5).read())
        r = data['chart']['result'][0]
        closes = [c for c in r['indicators']['quote'][0].get('close',[]) if c is not None]
        if not closes: return None
        v = closes[-1]
        _vix_cache['ts'] = now
        _vix_cache['value'] = v
        return v
    except Exception:
        return None

def _vix_regime(v):
    if v is None: return 'unknown'
    if v < 15: return 'complacent'
    if v < 25: return 'normal'
    if v < 35: return 'elevated'
    if v < 50: return 'panic'
    return 'crisis'

# === Anchor asset cache (for correlation align filter) ===
_anchor_cache = {}  # {symbol: (ts, [closes])}
_anchor_ttl = 600  # 10min

_ANCHOR_MAP = {
    # Ticker -> anchor symbol (Yahoo)
    'EURAUD': ['EURUSD=X','AUDUSD=X'],
    'GBPNZD': ['GBPUSD=X','NZDUSD=X'],
    'GER40': ['^GSPC','GC=F'],
    'US500': ['^NDX','^DJI'],
    'XAUUSD': ['SI=F','DX-Y.NYB'],
    'XAGUSD': ['GC=F','DX-Y.NYB'],
    'XPTUSD': ['GC=F','SI=F'],
    'XPDUSD': ['GC=F','SI=F'],
    'SPOTCRUDE': ['BZ=F','DX-Y.NYB'],
    'SPOTBRENT': ['CL=F','DX-Y.NYB'],
    'NATGAS': ['CL=F'],
    'COPPER': ['^GSPC','GC=F'],
    'CORN': ['ZW=F','ZS=F'],
    'WHEAT': ['ZC=F','ZS=F'],
    'SOYBEANS': ['ZW=F','ZC=F'],
    'NAS100': ['^GSPC','^DJI'],
    'US30': ['^GSPC','^NDX'],
    'US2000': ['^GSPC','^DJI'],
    'UK100': ['^GSPC','^GDAXI'],
    'JPN225': ['^GSPC','JPY=X'],
    'HK50': ['^GSPC','^N225'],
    'SUGAR': ['KC=F'],
    'COFFEE': ['SB=F'],
}

def _fetch_anchor_6h_change(ticker):
    """Returns % change of anchor asset over last 6 hours, None if unavailable."""
    anchors = _ANCHOR_MAP.get(ticker.upper(), [])
    if not anchors: return None
    now = time.time()
    for anc in anchors:
        cached = _anchor_cache.get(anc)
        if cached and (now - cached[0] < _anchor_ttl):
            closes = cached[1]
            if len(closes) >= 7:
                return (closes[-1] - closes[-7]) / closes[-7] * 100 if closes[-7] > 0 else None
            continue
        try:
            import urllib.request as _ur
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{anc}?interval=1h&range=1d'
            req = _ur.Request(url, headers={'User-Agent':'Mozilla/5.0'})
            data = _json.loads(_ur.urlopen(req, timeout=5).read())
            r = data['chart']['result'][0]
            closes = [c for c in r['indicators']['quote'][0].get('close',[]) if c is not None]
            if len(closes) >= 7:
                _anchor_cache[anc] = (now, closes)
                return (closes[-1] - closes[-7]) / closes[-7] * 100 if closes[-7] > 0 else None
        except Exception:
            continue
    return None

# === Per-ticker gate filter pipeline ===
def _pass_session(hour_utc, sf):
    if sf == 'all' or not sf: return True
    if sf == 'london_only': return 7 <= hour_utc < 12
    if sf == 'london_ny': return 7 <= hour_utc < 17
    if sf == 'london_ny_pm': return 7 <= hour_utc < 21
    if sf == 'ny_only': return 12 <= hour_utc < 17
    if sf == 'ny_pm_only': return 17 <= hour_utc < 21
    if sf == 'asia_only': return hour_utc < 7 or hour_utc >= 21
    if sf == 'skip_asia': return 7 <= hour_utc < 21
    return True

def _pass_vix(v, b):
    if v is None or b in (None, 'any','none'): return True
    if b == 'sub15': return v < 15
    if b == 'over15': return v > 15
    if b == 'over18' or b == 'over18_only': return v > 18
    if b == 'over20': return v > 20
    if b == 'over25': return v > 25
    if b == 'over30': return v > 30
    if b == '15to25' or b == 'normal_only': return 15 <= v <= 25
    if b == '15to20': return 15 <= v <= 20
    if b == '20to25': return 20 <= v <= 25
    if b == 'skip_high':
        regime = _vix_regime(v)
        return regime not in ('panic','crisis')
    if b == 'skip_low':
        return _vix_regime(v) != 'complacent'
    if b == 'only_elevated':
        return _vix_regime(v) in ('elevated','normal')
    return True

def _pass_anchor(ticker, direction, af):
    if not af or af == 'none': return True
    move = _fetch_anchor_6h_change(ticker)
    if move is None: return True  # fail open if anchor unavailable
    sig_bull = direction.upper() == 'BUY'
    if af in ('align_6h','align_3h'):
        return (sig_bull and move > 0) or (not sig_bull and move < 0)
    if af in ('counter_6h','counter_3h'):
        return (sig_bull and move < 0) or (not sig_bull and move > 0)
    return True

def _pass_rsi(r, b):
    if r is None or b in (None, 'any','none'): return True
    if b == 'rsi_under30': return r < 30
    if b == 'rsi_30_70': return 30 <= r <= 70
    if b == 'rsi_over70': return r > 70
    if b == 'rsi_under50': return r < 50
    if b == 'rsi_over50': return r > 50
    if b == 'rsi_40_60': return 40 <= r <= 60
    return True

def _pass_hour_block(hour_utc, hb):
    if not hb or hb == 'any': return True
    if hb == 'skip_dst_rollover': return not (21 <= hour_utc < 24)
    return True

def _mt4_filter_pass(clean_ticker, direction='BUY'):
    # v4.19: per-ticker kill switch — disabled tickers never trade
    _gate_check = MT4_TICKER_GATES.get(clean_ticker, {})
    if not _gate_check.get('enabled', True):
        return False, f"DISABLED ({_gate_check.get('disabled_reason','manual_kill')})"
    """v4.8 full per-ticker gate pipeline.
    Returns (passed: bool, reason: str). Reason is 'ok' on pass.
    Never drops — filters per-ticker using MT4_TICKER_GATES config.
    """
    if not MT4_FILTERS_ENABLED:
        return True, 'filters_disabled'
    t = clean_ticker.upper()
    gate = MT4_TICKER_GATES.get(t, {})
    # Disabled (VIX sentiment-only)
    if gate.get('enabled') is False:
        return False, 'DISABLED_GATE'
    now = time.time()
    import datetime as _dt
    hour_utc = _dt.datetime.utcnow().hour
    # Session
    sf = gate.get('session_filter', 'all')
    if not _pass_session(hour_utc, sf):
        return False, f'SESSION ({sf} h={hour_utc})'
    # Hour block
    hb = gate.get('hour_block', 'any')
    if not _pass_hour_block(hour_utc, hb):
        return False, f'HOUR_BLOCK ({hb})'
    # Per-ticker cooldown
    cooldown_sec = gate.get('cooldown_sec', 900)
    last = _mt4_last_signal.get(clean_ticker)
    if cooldown_sec > 0 and last and (now - last) < cooldown_sec:
        return False, f'COOLDOWN ({int((now-last)/60)}min)'
    # ATR
    atr_min = gate.get('atr_min', 0.0)
    atr_max = gate.get('atr_max', 999.0)
    atr = _mt4_atr_pct(clean_ticker)
    if atr is not None:
        if atr_min > 0 and atr < atr_min:
            return False, f'ATR_LOW ({atr:.2f}% < {atr_min})'
        if atr_max < 999 and atr > atr_max:
            return False, f'ATR_HIGH ({atr:.2f}% > {atr_max})'
    # VIX filter
    vf = gate.get('vix_filter', 'any')
    if vf not in ('any','none', None):
        vix = _get_vix()
        if not _pass_vix(vix, vf):
            return False, f'VIX_FILTER ({vf}, vix={vix})'
    # Anchor alignment
    af = gate.get('anchor_align', 'none')
    if af and af != 'none':
        if not _pass_anchor(t, direction, af):
            return False, f'ANCHOR_{af.upper()}'
    # RSI filter (requires ATR fetch to have populated; best-effort)
    # (RSI fetched separately, skipping for now — filter passes unless gate requires)
    return True, 'ok'

def _mt4_daily_pnl_pct():
    """v4.21: rolling today's total PnL % across all MT4 tickers.
    Reads MT4_CLOSED_RING, sums exit_pct for trades closed in last 24h.
    Used for daily drawdown kill switch.
    """
    import datetime
    now = time.time()
    cutoff_today_utc = now - (now % 86400)  # start of UTC day
    total = 0.0
    for r in MT4_CLOSED_RING:
        if r['ts'] >= cutoff_today_utc:
            total += float(r.get('exit_pct', 0))
    return total

MT4_DAILY_DD_LIMIT = -9999.0  # v4.22: DISABLED — user directive: no kill switches

def _mt4_live_wr_mult(clean_ticker):
    """v4.21: adaptive sizing from live WR. Reads last 20 trades from MT4_LIVE_STATS.
    Returns size multiplier:
      - WR >= 65% and PF >= 1.5 over 20+ trades → 1.3x (hot streak, scale up)
      - WR 55-64% or PF 1.1-1.5 → 1.1x (decent)
      - WR 45-54% or PF 0.9-1.1 → 1.0x (neutral)
      - WR 35-44% or PF 0.6-0.9 → 0.7x (cold, scale down)
      - WR < 35% or PF < 0.6 → 0.4x (very cold, barely size)
    With < 5 trades returns 1.0 (insufficient data).
    Trades counted: only last 20 entries in recent[] ring.
    """
    ss = MT4_LIVE_STATS.get(clean_ticker)
    if not ss or not ss.get('recent'): return 1.0
    recent = ss['recent'][-20:]
    n = len(recent)
    if n < 5: return 1.0  # insufficient sample
    wins = sum(1 for r in recent if r['pnl'] > 0)
    losses = n - wins
    wr = wins / n * 100.0
    gross_wins = sum(r['pnl'] for r in recent if r['pnl'] > 0)
    gross_losses = abs(sum(r['pnl'] for r in recent if r['pnl'] <= 0))
    pf = gross_wins / gross_losses if gross_losses > 0 else 9.0
    # Combined gating
    if wr >= 65 and pf >= 1.5: mult = 1.3
    elif wr >= 55 or pf >= 1.1: mult = 1.1
    elif wr >= 45 or pf >= 0.9: mult = 1.0
    elif wr >= 35 or pf >= 0.6: mult = 0.7
    else: mult = 0.4
    log(f"MT4 LIVE_WR {clean_ticker}: n={n} wr={wr:.0f}% pf={pf:.2f} mult={mult}")
    return mult

def _mt4_max_spread_for(clean_ticker):
    """Per-instrument-class max spread % for EA spread gate.
    Pepperstone typical spreads (points / base price * 100):
    - FX majors: 0.01-0.05%
    - FX crosses (NZDJPY, GBPCAD): 0.05-0.12%
    - Gold/Silver: 0.05-0.25%
    - Platinum/Palladium: 0.3-0.8% (wide due to low liquidity)
    - Oil: 0.15-0.35%
    - Indices: 0.05-0.30%
    - Exotics: 0.20-0.50%
    Return value is CEILING; EA rejects if live spread exceeds this.
    """
    # Tight majors
    if clean_ticker in {'EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','USDCHF','NZDUSD'}:
        return 0.05
    # FX crosses
    if clean_ticker in {'EURJPY','GBPJPY','EURGBP','EURAUD','AUDJPY','CADJPY','CHFJPY',
                        'AUDCAD','AUDCHF','AUDNZD','CADCHF','EURCAD','EURCHF',
                        'GBPAUD','GBPCHF','GBPNZD','NZDCAD','NZDJPY'}:
        return 0.12
    # Gold / Silver
    if clean_ticker in {'XAUUSD','XAGUSD'}:
        return 0.25
    # Platinum / Palladium (low liquidity, wide spreads normal)
    if clean_ticker in {'XPTUSD','XPDUSD'}:
        return 0.80
    # Oil
    if clean_ticker in {'SPOTCRUDE','SPOTBRENT'}:
        return 0.35
    # NatGas
    if clean_ticker in {'NATGAS'}:
        return 0.50
    # Major indices
    if clean_ticker in {'US30','US500','NAS100','GER40','UK100','JPN225'}:
        return 0.30
    # Smaller indices
    if clean_ticker in {'US2000','HK50'}:
        return 0.40
    # Soft commodities
    if clean_ticker in {'COPPER','CORN','WHEAT','SOYBEANS','SUGAR','COFFEE'}:
        return 0.50
    # Vol/dollar index
    if clean_ticker in {'VIX','USDX','EURX'}:
        return 0.50
    return 0.20  # default

def _mt4_vix_overlay_mult(clean_ticker):
    """VIX sentiment-based size multiplier (never blocks, only scales)."""
    gate = MT4_TICKER_GATES.get(clean_ticker.upper(), {})
    overlay = gate.get('vix_overlay')
    if not overlay:
        return 1.0
    vix = _get_vix()
    regime = _vix_regime(vix)
    if regime == 'complacent': return overlay.get('low_vix_mult', 1.0)
    if regime == 'normal': return overlay.get('normal_mult', 1.0)
    if regime == 'elevated': return overlay.get('elevated_mult', 1.0)
    if regime in ('panic','crisis'): return overlay.get('panic_mult', 0.5)
    return 1.0
# ===== end webhook filter =====

def _mt4_save():
    try:
        with open(MT4_QUEUE_FILE, 'w') as _f:
            _json.dump({'queue': MT4_QUEUE, 'bias': MT4_BIAS}, _f)
    except Exception as _e:
        pass  # never let disk IO break HL

def _mt4_load():
    global MT4_QUEUE, MT4_BIAS
    try:
        if os.path.exists(MT4_QUEUE_FILE):
            with open(MT4_QUEUE_FILE) as _f:
                _d = _json.load(_f)
            _q = _d.get('queue', [])
            _now = time.time()
            # drop stale on boot
            MT4_QUEUE[:] = [_s for _s in _q if (_now - _s.get('ts', 0)) < MT4_STALE_SEC]
            _b = _d.get('bias')
            if isinstance(_b, dict):
                MT4_BIAS.update(_b)
            try:
                log(f"MT4 QUEUE RESTORED: {len(MT4_QUEUE)} signals from disk")
            except Exception:
                pass
    except Exception as _e:
        try:
            log(f"MT4 QUEUE LOAD ERR: {_e}")
        except Exception:
            pass
# --- end MT4 persistence block ---


PEPPERSTONE_TICKERS = {
    'XAUUSD','XAGUSD','SPOTCRUDE','SPOTBRENT','NATGAS',
    'EURUSD','GBPUSD','USDJPY','EURGBP','GBPNZD',
    'AUDCAD','AUDUSD','USDCAD','USDCHF','AUDCHF',
    'AUDNZD','AUDJPY','CADCHF','CADJPY','CHFJPY',
    'EURAUD','EURCAD','EURCHF','GBPAUD','GBPCHF',
    'NZDUSD','NZDCAD','NAS100','US30','US500','US2000',
    'GER40','UK100','JPN225','HK50','XPTUSD','XPDUSD',
    'COPPER','CORN','WHEAT','SOYBEANS','COFFEE','SUGAR',
    'VIX','USDX','EURX'
}

TV_TO_MT4 = {
    'XAUUSD':'XAUUSD.a','XAGUSD':'XAGUSD.a','XPTUSD':'XPTUSD.a','XPDUSD':'XPDUSD.a',
    'SPOTCRUDE':'SpotCrude.a','SPOTBRENT':'SpotBrent.a','NATGAS':'NatGas.a',
    'EURUSD':'EURUSD.a','GBPUSD':'GBPUSD.a','USDJPY':'USDJPY.a',
    'EURGBP':'EURGBP.a','GBPNZD':'GBPNZD.a','AUDCAD':'AUDCAD.a',
    'AUDUSD':'AUDUSD.a','USDCAD':'USDCAD.a','USDCHF':'USDCHF.a',
    'AUDCHF':'AUDCHF.a','AUDNZD':'AUDNZD.a','AUDJPY':'AUDJPY.a',
    'CADCHF':'CADCHF.a','CADJPY':'CADJPY.a','CHFJPY':'CHFJPY.a',
    'EURAUD':'EURAUD.a','EURCAD':'EURCAD.a','EURCHF':'EURCHF.a',
    'GBPAUD':'GBPAUD.a','GBPCHF':'GBPCHF.a','NZDUSD':'NZDUSD.a',
    'NZDCAD':'NZDCAD.a','NAS100':'NAS100.a','US30':'US30.a',
    'US500':'US500.a','US2000':'US2000.a','GER40':'GER40.a',
    'UK100':'UK100.a','JPN225':'JPN225.a','HK50':'HK50.a',
    'COPPER':'Copper.a','CORN':'Corn.a','WHEAT':'Wheat.a',
    'SOYBEANS':'Soybeans.a','COFFEE':'Coffee.a','SUGAR':'Sugar.a',
    'VIX':'VIX.a','USDX':'USDX.a','EURX':'EURX.a'
}

def is_pepperstone(ticker):
    clean = ticker.upper().replace('PEPPERSTONE:','').replace('USDT','').replace('.P','')
    return clean in PEPPERSTONE_TICKERS

def get_mt4_symbol(ticker):
    clean = ticker.upper().replace('PEPPERSTONE:','').replace('USDT','').replace('.P','')
    return TV_TO_MT4.get(clean, clean + '.a')


# Map TradingView ticker → HL coin name
def tv_to_hl(ticker):
    """BTCUSD→BTC, SOLUSDT→SOL, BONKUSDT→kBONK, etc."""
    t = ticker.upper().replace('USDT.P','').replace('.P','').replace('USDT','').replace('USD','').replace('PERP','')
    # k-prefix for 1000x tokens
    remap = {'BONK':'kBONK','PEPE':'kPEPE','SHIB':'kSHIB','MATIC':'POL',
             '1000BONK':'kBONK','1000PEPE':'kPEPE','1000SHIB':'kSHIB'}
    return remap.get(t, t)

app = Flask(__name__)


_LANDING_HTML = None
def _load_landing():
    # Always re-read so deploys pick up immediately
    try:
        with open(os.path.join(os.path.dirname(__file__), 'landing.html'), 'r') as f:
            return f.read()
    except Exception as e:
        return f"<h1>landing load err: {e}</h1>"

@app.route('/', methods=['GET'])
@app.route('/landing', methods=['GET'])
def landing():
    resp = Response(_load_landing(), mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/stats/reset', methods=['GET'])
def stats_reset():
    if flask_request.args.get('k') != WEBHOOK_SECRET[:16]: return jsonify({'err':'unauthorized'}), 401
    state = load_state()
    state['stats'] = {'by_engine': {}, 'by_hour': {}, 'by_side': {}, 'by_coin': {},
                      'by_conf': {}, 'total_wins': 0, 'total_losses': 0, 'total_pnl': 0.0}
    save_state(state)
    return jsonify({'status':'stats reset'})

@app.route('/stats', methods=['GET'])
def stats_endpoint():
    """Live stats: per-engine, per-hour, per-side, per-coin, per-conf."""
    try:
        state = load_state()
        stats = state.get('stats', {})
        def summarize(bucket):
            out = {}
            for k, v in bucket.items():
                w = v.get('w',0); l = v.get('l',0); n = w + l
                wr = (w/n) if n else 0
                out[k] = {'n': n, 'wr': round(wr*100,1), 'pnl_pct': round(v.get('pnl',0),2)}
            return out
        return jsonify({
            'total_wins': stats.get('total_wins',0),
            'total_losses': stats.get('total_losses',0),
            'total_n': stats.get('total_wins',0) + stats.get('total_losses',0),
            'overall_wr': round(stats.get('total_wins',0) / max(1, stats.get('total_wins',0)+stats.get('total_losses',0)) * 100, 1),
            'total_pnl_pct': round(stats.get('total_pnl',0), 2),
            'by_engine': summarize(stats.get('by_engine', {})),
            'by_hour':   summarize(stats.get('by_hour', {})),
            'by_side':   summarize(stats.get('by_side', {})),
            'by_coin':   summarize(stats.get('by_coin', {})),
            'by_conf':   summarize(stats.get('by_conf', {})),
        })
    except Exception as e:
        return jsonify({'err': str(e)})

@app.route('/conf/test/<coin>', methods=['GET'])
def conf_test(coin):
    """Test confidence scoring on current coin state (no trade fired)."""
    try:
        candles = fetch(coin.upper())
        if len(candles) < 50:
            return jsonify({'err': f'insufficient candles: {len(candles)}'})
        btc = btc_correlation.get_state()
        btc_d = btc.get('btc_dir', 0)
        buy_score, buy_brk = confidence.score(candles, [], coin.upper(), 'BUY', btc_d)
        sell_score, sell_brk = confidence.score(candles, [], coin.upper(), 'SELL', btc_d)
        return jsonify({
            'coin': coin.upper(),
            'n_candles': len(candles),
            'btc_dir': btc_d,
            'btc_move_15m': btc.get('btc_move', 0),
            'btc_move_1h': btc.get('btc_1h_move', 0),
            'BUY':  {'score': buy_score,  'mult': confidence.size_multiplier(buy_score),  'breakdown': buy_brk},
            'SELL': {'score': sell_score, 'mult': confidence.size_multiplier(sell_score), 'breakdown': sell_brk},
        })
    except Exception as e:
        return jsonify({'err': str(e)})

@app.route('/engines', methods=['GET'])
def engines_status():
    """Live engine + guard + venue state."""
    try:
        btc = btc_correlation.get_state()
        btc_fresh = (time.time() - btc.get('ts',0)) < 120 if btc.get('ts') else False
    except Exception: btc_fresh = False
    try:
        venues = orderbook_ws.get_venue_status()
    except Exception: venues = {}
    def v_ok(name):
        age = venues.get(name)
        return age is not None and age < 60
    return jsonify({
        'signal_engines': {
            'PIVOT': True,  # always core
            'PULLBACK': True,
            'WALL_BNC': v_ok('by') or v_ok('bn'),
            'LIQ_CSCD': True,
            'CVD_DIV': True,
        },
        'guards': {
            'V3_TREND': True,
            'ATR_MIN': True,
            'BTC_CORR': btc_fresh,
            'FUNDING': True,
            'CHASE': True,
            'SPOOF': True,
            'NEWS': True,
            'POS_CAPS': True,
            'DD_BRK': True,
        },
        'sizing': {'CONF_SIZE': True},
        'venues': {
            'BYBIT':    v_ok('by'),
            'BINANCE':  v_ok('bn'),
            'OKX':      v_ok('okx'),
            'COINBASE': v_ok('cb'),
            'BITGET':   v_ok('bg'),
            'KRAKEN':   v_ok('kr'),
        },
        'venue_ages': venues,
    })

@app.route('/orderbook/<coin>', methods=['GET'])
def orderbook_depth(coin):
    try:
        agg = orderbook_ws.get_aggregated_depth(coin.upper()) if hasattr(orderbook_ws,'get_aggregated_depth') else None
        if not agg:
            # Fallback: build from _DEPTH
            from orderbook_ws import _DEPTH, _LOCK
            with _LOCK:
                d = _DEPTH.get(coin.upper(), {})
                bids_raw = d.get('bids', {})
                asks_raw = d.get('asks', {})
                mid = d.get('mid', 0)
            # _DEPTH uses venue_px keys
            bids = {}; asks = {}
            for k,v in bids_raw.items():
                if isinstance(v, tuple): px, sz = v; bids[px] = bids.get(px,0)+sz
            for k,v in asks_raw.items():
                if isinstance(v, tuple): px, sz = v; asks[px] = asks.get(px,0)+sz
            agg = {'bids':bids,'asks':asks,'mid':mid,'venue_count':0}
        mid = agg.get('mid', 0)
        # Build depth levels within 2% of mid, bucketed
        if not mid: return jsonify({'mid':0,'bids':[],'asks':[]})
        bids = sorted([(p,s) for p,s in agg['bids'].items() if p > mid*0.97 and p <= mid], reverse=True)
        asks = sorted([(p,s) for p,s in agg['asks'].items() if p < mid*1.03 and p >= mid])
        # Bucket into 40 levels
        import math
        def bucket(orders, N=40):
            if not orders: return []
            out = []
            for px, sz in orders:
                usd = px * sz
                out.append({'price': px, 'size': sz, 'usd': usd})
            return out[:N]
        return jsonify({'coin':coin.upper(),'mid':mid,
                        'bids':bucket(bids,40),
                        'asks':bucket(asks,40),
                        'venues':agg.get('venue_count',0)})
    except Exception as e:
        return jsonify({'err':str(e)})

@app.route('/signals', methods=['GET'])
def signals_feed():
    with _SIGNAL_LOG_LOCK:
        items = list(_SIGNAL_LOG)[-30:][::-1]
    return jsonify({'items': items})

@app.route('/whales', methods=['GET'])
def whales_feed():
    try:
        from collections import deque
        items = []
        if hasattr(whale_filter, '_WHALES'):
            now = time.time()
            with whale_filter._LOCK:
                for coin, dq in whale_filter._WHALES.items():
                    for ts, side, usd in list(dq)[-5:]:
                        if now - ts < 300:
                            items.append({'coin':coin,'side':side,'usd':usd,'ts':ts})
        items.sort(key=lambda x: x['ts'], reverse=True)
        return jsonify({'items': items[:20]})
    except Exception as e:
        return jsonify({'items': [], 'err': str(e)})

@app.route('/news', methods=['GET'])
def news_feed():
    try:
        items = news_filter.get_recent_items(limit=10) if hasattr(news_filter, 'get_recent_items') else []
    except Exception:
        items = []
    state = news_filter.get_state() if hasattr(news_filter, 'get_state') else {}
    return jsonify({'items': items, 'state': state})

@app.route('/health', methods=['GET'])
def health():
    eq = 0
    try: eq = get_balance()
    except Exception: pass
    cur_regime = None
    try:
        import regime_detector
        cur_regime = regime_detector.get_regime()
    except Exception: pass
    return jsonify({'status':'ok','version':'v8.28','equity':eq,
                    'queue_size':WEBHOOK_QUEUE.qsize(),
                    'mt4_queue':len(MT4_QUEUE),
                    'coins':len(COINS),
                    'risk':INITIAL_RISK_PCT,
                    'trail':TRAIL_PCT,
                    'gates_loaded':len(TICKER_GATES),
                    'regime':cur_regime,
                    'recent_logs':LOG_BUFFER[-20:]})

@app.route('/regime', methods=['GET'])
def regime_status():
    """Return current regime detector state + per-coin coverage."""
    try:
        import regime_detector
        import regime_configs
        return jsonify({
            'detector': regime_detector.status(),
            'config_coverage': regime_configs.coverage_stats(),
            'total_coins_with_regime_configs': len(regime_configs.REGIME_CONFIGS),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/monitor', methods=['GET'])
def monitor_status():
    """Live monitoring: rolling 50-trade WR/expectancy/avg$win/loss + alerts."""
    try:
        import monitor
        return jsonify(monitor.status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/alerts', methods=['GET'])
def get_alerts():
    """All pending alerts. Optional ?severity=CRITICAL|WARN filter."""
    try:
        import monitor
        from flask import request
        sev = request.args.get('severity')
        return jsonify({'alerts': monitor.get_alerts(sev)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

LOG_BUFFER = []


@app.route('/trades', methods=['GET'])
def get_trades():
    """Return trade log CSV as JSON for analysis."""
    try:
        import csv
        trades = []
        with open(TRADE_LOG) as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(row)
        wins = sum(1 for t in trades if t.get('pnl','0') not in ('0','') and float(t['pnl']) > 0)
        losses = sum(1 for t in trades if t.get('pnl','0') not in ('0','') and float(t['pnl']) < 0)
        total_pnl = sum(float(t['pnl']) for t in trades if t.get('pnl','0') not in ('0',''))
        return jsonify({'trades': trades[-50:], 'total': len(trades), 'wins': wins, 'losses': losses, 'total_pnl': round(total_pnl, 4)})
    except Exception as e:
        return jsonify({'error': str(e), 'trades': []})

@app.route('/reset', methods=['GET'])
def reset_cb():
    """Reset circuit breaker and consecutive losses."""
    state = load_state()
    state['cb_pause_until'] = 0
    state['consec_losses'] = 0
    save_state(state)
    log("CIRCUIT BREAKER RESET via /reset endpoint")
    return jsonify({'status':'reset','cb_pause_until':0,'consec_losses':0})

@app.route('/closeall', methods=['GET'])
def close_all_positions():
    """Force close ALL — requires ?secret="""
    if flask_request.args.get('secret') != WEBHOOK_SECRET: return jsonify({'err':'unauthorized'}), 401
    state = load_state()
    positions = get_all_positions_live()
    closed = []
    for coin, pos in positions.items():
        try:
            pnl = close(coin)
            closed.append({'coin':coin,'pnl':pnl})
            state['positions'].pop(coin, None)
        except Exception as e:
            closed.append({'coin':coin,'error':str(e)})
    state['consec_losses'] = 0
    state['cb_pause_until'] = 0
    save_state(state)
    log(f"FORCE CLOSE ALL: {len(closed)} positions closed")
    return jsonify({'status':'closed_all','positions':closed})

@app.route('/close/<coin>', methods=['GET', 'POST'])
def close_one_position(coin):
    """Force close a single coin. Requires ?secret="""
    if flask_request.args.get('secret') != WEBHOOK_SECRET: return jsonify({'err':'unauthorized'}), 401
    coin = coin.upper()
    try:
        pnl = close(coin)
        state = load_state()
        state['positions'].pop(coin, None)
        save_state(state)
        return jsonify({'status':'closed','coin':coin,'pnl':pnl})
    except Exception as e:
        return jsonify({'status':'error','coin':coin,'error':str(e)}), 500

@app.route('/transfer', methods=['POST'])
def transfer_funds():
    """Transfer USDC internally on HL. POST {amount, to_wallet}"""
    try:
        data = flask_request.get_json(force=True, silent=True)
        if not data or 'amount' not in data:
            return jsonify({'error': 'POST {amount, to_wallet} required'}), 400
        amount = float(data['amount'])
        to_wallet = data.get('to_wallet', WALLET)
        log(f"TRANSFER REQUEST: {amount} USDC to {to_wallet}")
        result = exchange.usd_transfer(amount, to_wallet)
        log(f"TRANSFER RESULT: {result}")
        return jsonify({'status': 'transferred', 'amount': amount, 'to': to_wallet, 'result': str(result)}), 200
    except Exception as e:
        log(f"TRANSFER ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive DynaPro signal from TradingView.
    Expected JSON: {"ticker":"BTCUSD","action":"buy|sell|exit_buy|exit_sell","price":12345.67}
    Optional: {"secret":"...","tf":"15"} 
    Also accepts plain text: 'buy BTCUSD 12345.67' format.
    """
    # Parse flexibly — TV sends various formats
    raw_body = flask_request.get_data(as_text=True)
    log(f"WEBHOOK RAW: content_type={flask_request.content_type} body={raw_body[:300]}")
    
    data = None
    try:
        data = flask_request.get_json(force=True, silent=True)
    except Exception: pass
    
    if not data:
        text = raw_body.strip()
        
        # DynaPro pattern: "Double Top Pattern Detected | timeframe : 15 | ENSUSDT"
        if '|' in text:
            parts = [p.strip() for p in text.split('|')]
            ticker_part = parts[-1] if len(parts) >= 2 else ''
            pt = parts[0].lower()
            bearish = any(b in pt for b in ['double top','head and shoulders','rising wedge','descending triangle','bearish','evening star','shooting star','dark cloud','hanging man','three black'])
            bullish = any(b in pt for b in ['double bottom','inverted h&s','inverse head','falling wedge','ascending triangle','bullish','morning star','hammer','piercing','three white'])
            if (bearish or bullish) and ticker_part:
                data = {'action': 'sell' if bearish else 'buy', 'ticker': ticker_part}
            else:
                log(f"WEBHOOK PATTERN SKIP: {text[:100]}")
                return jsonify({'status':'received','type':'pattern'}), 200
        
        # "long entry" / "short entry" — broadcast to ALL Pepperstone tickers
        elif text.lower() in ('long entry','short entry','long exit','short exit'):
            direction = 'BUY' if 'long' in text.lower() else 'SELL'
            MT4_BIAS['direction'] = direction
            MT4_BIAS['ts'] = time.time()
            mt4_count = 0
            for tv_sym, mt4_sym in TV_TO_MT4.items():
                MT4_QUEUE.append({'symbol': mt4_sym, 'direction': direction, 'price': 0, 'ts': time.time()})
                mt4_count += 1
            if len(MT4_QUEUE) > 200: MT4_QUEUE[:] = MT4_QUEUE[-200:]
            _mt4_save()
            log(f"MT4 BROADCAST: {direction} → {mt4_count} tickers (from '{text}')")
            return jsonify({'status':'broadcast','direction':direction,'count':mt4_count}), 200
        
        else:
            parts = text.replace('\n',' ').split()
            if len(parts) >= 2:
                first = parts[0].lower()
                if first in ('long','short'):
                    data = {'action': 'buy' if first=='long' else 'sell', 'ticker': parts[-1]}
                else:
                    data = {'action': parts[0].lower(), 'ticker': parts[1]}
                if len(parts) >= 3:
                    try: data['price'] = float(parts[-1])
                    except Exception: pass
    
    if not data:
        # Last resort — just log and accept, don't 400
        log(f"WEBHOOK UNPARSEABLE: {raw_body[:200]}")
        return jsonify({'status':'received','parsed':False}), 200
    
    # If no ticker, try to extract from raw body
    if 'ticker' not in data or not data['ticker']:
        # Search for anything that looks like a ticker symbol
        import re as _re
        m = _re.search(r'([A-Z]{2,}(?:USDT|USD)?(?:\.P)?)', raw_body)
        if m: data['ticker'] = m.group(1)
    
    if 'action' not in data or not data.get('action'):
        # Infer from body text
        lower = raw_body.lower()
        if 'long' in lower or 'buy' in lower: data['action'] = 'buy'
        elif 'short' in lower or 'sell' in lower: data['action'] = 'sell'
    
    if not data.get('ticker') or not data.get('action'):
        # No ticker — log and skip (Trend Buy/Sell alerts include tickers)
        action_text = str(data.get('action','')).lower()
        direction = None
        if 'long' in action_text or 'buy' in action_text: direction = 'BUY'
        elif 'short' in action_text or 'sell' in action_text: direction = 'SELL'
        if direction:
            MT4_BIAS['direction'] = direction
            MT4_BIAS['ts'] = time.time()
            log(f"MT4 BIAS: {direction} (condition alert, no ticker)")
        return jsonify({'status':'bias_only','direction':direction or ''}), 200

    # Optional secret check
    if WEBHOOK_SECRET and data.get('secret') and data['secret'] != WEBHOOK_SECRET:
        return jsonify({'error':'bad secret'}), 403

    coin = tv_to_hl(data['ticker'])
    action_raw = str(data.get('action','')).lower().replace(' ','_')
    price = data.get('price', 0)

    # Normalize action from DynaPro's various alert texts
    if action_raw in ('buy','sell','exit_buy','exit_sell'):
        action = action_raw
    elif 'long_entry' in action_raw or 'long entry' in str(data.get('action','')).lower():
        action = 'buy'
    elif 'short_entry' in action_raw or 'short entry' in str(data.get('action','')).lower():
        action = 'sell'
    elif 'long_exit' in action_raw or 'exit_buy' in action_raw:
        action = 'exit_buy'
    elif 'short_exit' in action_raw or 'exit_sell' in action_raw:
        action = 'exit_sell'
    else:
        # Check for pattern names in action field
        act = str(data.get('action','')).lower()
        bearish = any(b in act for b in ['double top','head and shoulders','rising wedge','descending triangle','evening star','shooting star','dark cloud','hanging man','three black'])
        bullish = any(b in act for b in ['double bottom','inverted h&s','inverse head','falling wedge','ascending triangle','morning star','hammer','piercing','three white'])
        if bearish: action = 'sell'
        elif bullish: action = 'buy'
        else:
            log(f"WEBHOOK UNKNOWN ACTION: {data.get('action','')[:100]} — skipped")
            return jsonify({'status':'received','unknown_action':True}), 200

    signal = {'coin': coin, 'action': action, 'price': price, 'ts': time.time(), 'source': 'dynapro'}
    
    # DEDUP REMOVED — was blocking legitimate re-entries
    
    # Route: Pepperstone tickers → MT4, crypto tickers → HL
    raw_ticker = data.get('ticker','').upper().replace('PEPPERSTONE:','')
    if is_pepperstone(raw_ticker):
        mt4_sym = get_mt4_symbol(raw_ticker)
        clean = raw_ticker.upper().replace('PEPPERSTONE:','').replace('.A','')
        gate = MT4_TICKER_GATES.get(clean, {})
        direction = action.upper()
        # FILTER: v4.8 per-ticker gate pipeline (direction passed for anchor-align)
        _passed, _reason = _mt4_filter_pass(clean, direction)
        if not _passed:
            log(f"MT4 FILTERED {clean} {direction}: {_reason}")
            return jsonify({'status':'filtered','symbol':clean,'reason':_reason}), 200
        # Inversion BEFORE pullback check so pullback sees the actual direction we'll trade
        if gate.get('invert', False) or gate.get('inverted', False):
            direction = 'SELL' if direction == 'BUY' else 'BUY'
            log(f"MT4 INVERTED {clean}: {action.upper()} → {direction}")
        # v4.10: pullback gate (must be near 1h EMA20, RSI cooled)
        _pb_ok, _pb_reason, _pb_meta = _mt4_pullback_check(clean, direction)
        if not _pb_ok:
            log(f"MT4 FILTERED {clean} {direction}: {_pb_reason} meta={_pb_meta}")
            return jsonify({'status':'filtered','symbol':clean,'reason':_pb_reason,'meta':_pb_meta}), 200
        _mt4_last_signal[clean] = time.time()
        log(f"MT4 PULLBACK {clean} {direction}: {_pb_reason}")
        # VIX sentiment size multiplier (scales, never blocks)
        size_mult = _mt4_vix_overlay_mult(clean)
        # v4.10: OANDA retail sentiment multiplier (contrarian fade at extremes)
        sent_mult = _mt4_sentiment_mult(clean, direction)
        # v4.9: zone confluence boost/reduce
        zone_boost = 1.0
        zone_info = {}
        if ZONES_ENABLED and _zones:
            try:
                zone_info = _zones.zone_confluence(clean, direction, price)
                zone_boost = zone_info.get('size_boost', 1.0)
                if zone_info.get('aligned') == 'contradicted':
                    log(f"MT4 ZONE CONTRA {clean} {direction} @ {price}: {zone_info.get('zones_hit',[])[:3]} — size×{zone_boost}")
                elif zone_info.get('aligned') == 'aligned':
                    log(f"MT4 ZONE ALIGN {clean} {direction} @ {price}: {zone_info.get('zones_hit',[])[:3]} — size×{zone_boost}")
            except Exception as _ze:
                log(f"MT4 zone err {clean}: {_ze}")
        live_wr_mult = _mt4_live_wr_mult(clean)
        final_mult = round(size_mult * zone_boost * sent_mult * live_wr_mult, 2)
        rec = {
            'symbol': mt4_sym,
            'direction': direction,
            'price': price,
            'ts': time.time(),
            'trail_activate': gate.get('trail_activate', 0.4),
            'trail_distance': gate.get('trail_distance', 0.2),
            'sl_pct': gate.get('sl_pct', 1.4),
            'time_cut_hours': gate.get('time_cut_hours'),
            'size_mult': final_mult,
            'vix_mult': round(size_mult, 2),
            'zone_boost': round(zone_boost, 2),
            'zone_status': zone_info.get('aligned') if zone_info else None,
            'sent_mult': round(sent_mult, 2),
            'live_wr_mult': round(live_wr_mult, 2),
            'pullback_meta': _pb_meta,
            'max_spread_pct': _mt4_max_spread_for(clean),
            'tp_pct': gate.get('tp_pct', round(gate.get('sl_pct', 1.0) * 2.0, 2)),
            'max_slip_pct': 0.3,  # EA rejects market fallback if slip > this
        }
        MT4_QUEUE.append(rec)
        if len(MT4_QUEUE) > 200: MT4_QUEUE[:] = MT4_QUEUE[-200:]
        _mt4_save()
        log(f"MT4 QUEUED: {direction} {mt4_sym} @ {price} trail={rec['trail_activate']}/{rec['trail_distance']} sl={rec['sl_pct']} vix×{size_mult} zone×{zone_boost} sent×{sent_mult} = {rec['size_mult']} pb={_pb_meta} slip_max={rec['max_slip_pct']}%")
        log_trade('MT4', clean, direction, price, 0, 'webhook')
        return jsonify({'status':'mt4_queued','symbol':mt4_sym,'action':direction}), 200

    # Per-ticker gate for webhook signals (non-blocking — don't fetch candles in webhook handler)
    try:
        wh_coin = signal.get('coin','').upper()
        gate = TICKER_GATES.get(wh_coin, {})
        # Quick gate checks that don't need candles (body/cloud need candles, skip here)
        # Full gate check happens in the main loop when signal executes
    except Exception as e:
        log(f"webhook gate err: {e}")

    WEBHOOK_QUEUE.put(signal)
    log(f"WEBHOOK: {action} {coin} @ {price} (queued, size={WEBHOOK_QUEUE.qsize()})")
    return jsonify({'status':'queued','coin':coin,'action':action}), 200

@app.route('/signal', methods=['POST'])
def signal_alias():
    """Alias for /webhook — backwards compatible with old cyber-psycho webhook URL."""
    return webhook()


@app.route('/mt4/signals', methods=['GET'])
def mt4_signals():
    """EA polls this every 10s. Returns one signal, removes from queue. Drops stale."""
    global MT4_QUEUE
    _now = time.time()
    # drop stale signals (older than MT4_STALE_SEC)
    while MT4_QUEUE and (_now - MT4_QUEUE[0].get('ts', 0)) >= MT4_STALE_SEC:
        _drop = MT4_QUEUE.pop(0)
        log(f"MT4 STALE DROP: {_drop.get('direction','')} {_drop.get('symbol','')} age={int(_now - _drop.get('ts',0))}s")
    if MT4_QUEUE:
        sig = MT4_QUEUE.pop(0)
        _mt4_save()
        log(f"MT4 SERVED: {sig['direction']} {sig['symbol']}")
        return jsonify(sig)
    return ('', 204)  # v4.16: empty body when no signal — EA's StringLen(body)<5 check bails cleanly

@app.route('/mt4/status', methods=['GET'])
def mt4_status():
    bias_age = time.time() - MT4_BIAS.get('ts', 0)
    bias_active = bias_age < 300  # 5min validity
    return jsonify({
        'queue_size':len(MT4_QUEUE),'queue':MT4_QUEUE[:5],
        'bias': MT4_BIAS.get('direction','') if bias_active else '',
        'bias_age_sec': round(bias_age)
    })

# FLATTEN BROADCAST: server sets a flag, EA polls, closes all magic-matched + deletes pendings, acks
MT4_FLATTEN_FLAG = {'pending': False, 'ts': 0, 'reason': ''}

@app.route('/mt4/flatten', methods=['POST', 'GET'])
def mt4_flatten_set():
    """Arm flatten broadcast. EA will act on next poll."""
    global MT4_QUEUE, MT4_FLATTEN_FLAG
    reason = flask_request.args.get('reason', 'user_request')
    # Clear the server queue too
    cleared = len(MT4_QUEUE)
    MT4_QUEUE.clear()
    _mt4_save()
    MT4_FLATTEN_FLAG = {'pending': True, 'ts': time.time(), 'reason': reason}
    log(f"MT4 FLATTEN ARMED: reason={reason}, cleared_queue={cleared}")
    return jsonify({'status': 'armed', 'reason': reason, 'queue_cleared': cleared})

@app.route('/mt4/flatten/check', methods=['GET'])
def mt4_flatten_check():
    """EA polls. Returns flag + timestamp. EA decides to act."""
    return jsonify(MT4_FLATTEN_FLAG)

@app.route('/mt4/flatten/ack', methods=['POST', 'GET'])
def mt4_flatten_ack():
    """EA acks after flatten complete. Clears flag."""
    global MT4_FLATTEN_FLAG
    closed = flask_request.args.get('closed', '0')
    deleted = flask_request.args.get('deleted', '0')
    log(f"MT4 FLATTEN ACK: closed={closed}, deleted={deleted}")
    MT4_FLATTEN_FLAG = {'pending': False, 'ts': time.time(), 'reason': ''}
    return jsonify({'status': 'acked'})

@app.route('/mt4/trade-opened', methods=['POST'])
def mt4_trade_opened():
    """EA v5.1 reports OrderSend success so server tracks direction per ticket."""
    try:
        d = flask_request.get_json(force=True, silent=True) or {}
        ticket = int(d.get('ticket', 0))
        symbol = (d.get('symbol') or '').replace('.a', '').upper()
        side = (d.get('side') or '').upper()
        entry = float(d.get('entry', 0))
        lots = float(d.get('lots', 0))
        if ticket <= 0: return jsonify({'ok': False, 'err':'no_ticket'}), 200
        MT4_TICKET_META[ticket] = {
            'direction': side, 'entry_ts': time.time(),
            'symbol': symbol, 'entry': entry, 'lots': lots,
        }
        cutoff = time.time() - 86400
        stale = [t for t,m in MT4_TICKET_META.items() if m['entry_ts'] < cutoff]
        for t in stale: MT4_TICKET_META.pop(t, None)
        log(f"MT4 OPEN #{ticket} {side} {symbol} @ {entry} lots={lots}")
        return jsonify({'ok': True})
    except Exception as e:
        log(f"MT4 trade-opened err: {e}")
        return jsonify({'ok': False, 'err': str(e)}), 200

@app.route('/mt4/trade-closed', methods=['POST'])
def mt4_trade_closed():
    """EA v5 reports every trade exit. Records PnL, rolls stats, queues retest if TRAIL."""
    try:
        raw_body = flask_request.get_data(as_text=True)
        d = flask_request.get_json(force=True, silent=True) or {}
        log(f"MT4 trade-closed RAW body={raw_body[:300]} parsed={d}")
        ticket = int(d.get('ticket', 0))
        symbol = (d.get('symbol') or '').replace('.a', '').upper()
        exit_type = (d.get('exit_type') or '').upper()
        entry = float(d.get('entry', 0))
        peak_pct = float(d.get('peak_pct', 0))
        exit_pct = float(d.get('exit_pct', 0))
        if entry <= 0:
            return jsonify({'ok': False, 'err': 'no_entry'}), 200

        # v4.15b: handle OPEN events piggybacked on /mt4/trade-closed (saves MT4 whitelist slot)
        if exit_type == 'OPEN':
            side = (d.get('side') or '').upper()
            lots = float(d.get('lots', 0))
            MT4_TICKET_META[ticket] = {
                'direction': side, 'entry_ts': time.time(),
                'symbol': symbol, 'entry': entry, 'lots': lots,
            }
            cutoff = time.time() - 86400
            stale = [t for t,m in MT4_TICKET_META.items() if m['entry_ts'] < cutoff]
            for t in stale: MT4_TICKET_META.pop(t, None)
            log(f"MT4 OPEN #{ticket} {side} {symbol} @ {entry} lots={lots}")
            return jsonify({'ok': True, 'event': 'open'})

        rec_exit = {
            'ts': time.time(), 'symbol': symbol, 'ticket': ticket,
            'exit_type': exit_type, 'entry': entry,
            'peak_pct': round(peak_pct, 3), 'exit_pct': round(exit_pct, 3),
            'win': exit_pct > 0,
        }
        MT4_CLOSED_RING.append(rec_exit)
        if len(MT4_CLOSED_RING) > 500:
            MT4_CLOSED_RING[:] = MT4_CLOSED_RING[-500:]

        ss = MT4_LIVE_STATS.setdefault(symbol, {'wins':0,'losses':0,'sum_pnl':0.0,'trades':0,'recent':[]})
        ss['trades'] += 1
        ss['sum_pnl'] += exit_pct
        if exit_pct > 0: ss['wins'] += 1
        else: ss['losses'] += 1
        ss['recent'].append({'ts':rec_exit['ts'],'pnl':exit_pct,'exit_type':exit_type,'peak':peak_pct})
        if len(ss['recent']) > 50:
            ss['recent'] = ss['recent'][-50:]

        try:
            with open('/var/data/mt4_stats.json','w') as f:
                _json.dump({'stats': MT4_LIVE_STATS, 'ring_len': len(MT4_CLOSED_RING)}, f, default=str)
        except Exception: pass

        outcome = 'WIN' if exit_pct > 0 else 'LOSS'
        wr50 = sum(1 for r in ss['recent'] if r['pnl']>0) / max(1, len(ss['recent'])) * 100
        log(f"MT4 CLOSE {symbol} #{ticket} {exit_type} peak={peak_pct:+.2f}% exit={exit_pct:+.2f}% {outcome} [n={ss['trades']} wr50={wr50:.0f}% totPnL={ss['sum_pnl']:+.2f}%]")

        if exit_type != 'TRAIL' or peak_pct < 0.3:
            return jsonify({'ok': True, 'recorded': True, 'retest': False})

        side = MT4_TICKET_META.get(ticket, {}).get('direction')
        if not side:
            return jsonify({'ok': True, 'recorded': True, 'retest': False, 'note':'no_side'})

        if side == 'BUY':
            peak_price = entry * (1 + peak_pct / 100.0)
            retest = peak_price - (peak_price - entry) * 0.382
        else:
            peak_price = entry * (1 - peak_pct / 100.0)
            retest = peak_price + (entry - peak_price) * 0.382

        broker_sym = symbol + '.a'
        rec = {
            'symbol': broker_sym, 'direction': side, 'price': round(retest, 5),
            'type': 'LIMIT', 'ts': int(time.time() * 1000), 'ttl_sec': 1800,
            'is_retest': True, 'origin_ticket': ticket,
            'origin_entry': entry, 'origin_peak_pct': peak_pct,
        }
        global MT4_LATEST_SIGNAL
        MT4_LATEST_SIGNAL = rec
        log(f"MT4 RETEST QUEUED: {side} {broker_sym} retest={retest:.5f} (peak was {peak_pct:.2f}% from entry {entry})")
        return jsonify({'ok': True, 'recorded': True, 'retest': True, 'retest_price': retest, 'ttl_sec': 1800})
    except Exception as e:
        log(f"MT4 trade-closed err: {e}")
        return jsonify({'ok': False, 'err': str(e)}), 200

@app.route('/mt4/stats', methods=['GET'])
def mt4_stats():
    """Per-ticker live WR/PnL dashboard."""
    out = {}
    for sym, ss in MT4_LIVE_STATS.items():
        recent = ss.get('recent', [])
        wr_all = ss['wins'] / max(1, ss['trades']) * 100
        wr50 = sum(1 for r in recent if r['pnl']>0) / max(1, len(recent)) * 100
        avg_pnl = ss['sum_pnl'] / max(1, ss['trades'])
        wins_pnl = sum(r['pnl'] for r in recent if r['pnl']>0)
        losses_pnl = sum(r['pnl'] for r in recent if r['pnl']<=0)
        pf = (wins_pnl / abs(losses_pnl)) if losses_pnl else 99.0
        out[sym] = {
            'trades': ss['trades'], 'wr_all_pct': round(wr_all, 1),
            'wr_last50_pct': round(wr50, 1), 'avg_pnl_pct': round(avg_pnl, 3),
            'total_pnl_pct': round(ss['sum_pnl'], 2), 'profit_factor': round(pf, 2),
        }
    sorted_out = dict(sorted(out.items(), key=lambda x: -x[1]['total_pnl_pct']))
    return jsonify({'tickers': sorted_out, 'ring_len': len(MT4_CLOSED_RING),
                    'total_closed': sum(s['trades'] for s in MT4_LIVE_STATS.values())})

@app.route('/mt4/stats/reset', methods=['POST'])
def mt4_stats_reset():
    """Wipe MT4 live stats. Intended for wiping test data before real trading begins."""
    global MT4_LIVE_STATS, MT4_CLOSED_RING, MT4_TICKET_META
    MT4_LIVE_STATS = {}
    MT4_CLOSED_RING = []
    MT4_TICKET_META = {}
    try:
        with open('/var/data/mt4_stats.json','w') as f:
            _json.dump({'stats': {}, 'ring_len': 0}, f)
    except Exception: pass
    log("MT4 STATS RESET")
    return jsonify({'ok': True, 'wiped': True})

@app.route('/mt4/stats/summary', methods=['GET'])
def mt4_stats_summary():
    total = sum(s['trades'] for s in MT4_LIVE_STATS.values())
    if total == 0:
        return jsonify({'trades':0,'msg':'no closures yet'})
    wins = sum(s['wins'] for s in MT4_LIVE_STATS.values())
    tot_pnl = sum(s['sum_pnl'] for s in MT4_LIVE_STATS.values())
    return jsonify({
        'trades': total, 'wins': wins, 'losses': total - wins,
        'wr_pct': round(wins / total * 100, 1),
        'total_pnl_pct_sum': round(tot_pnl, 2),
        'avg_pnl_pct': round(tot_pnl/total, 3),
        'tickers_traded': len(MT4_LIVE_STATS),
    })


COINS = [
    'SOL','LINK','UNI','ENS','AAVE','POL','SAND','APT','MON','COMP',
    'AERO','LIT','SPX','kPEPE','kBONK','kSHIB','MORPHO','JUP','XRP',
    'SUSHI','ADA','WLD','PUMP','PENGU','FARTCOIN',
    'AIXBT','AVAX','PENDLE','TAO','WIF',
    'BTC','BNB','DOT','ATOM','SUI','LDO','INJ','UMA','ALGO',
    'BLUR','VVV','APE','OP','TON','TIA','LTC','MOODENG',
    'AR','GALA','VIRTUAL',
    # Tuner-passed candidates (14d OOS with V3+ATR, WR>=65%, PnL>1%):
    'RESOLV', 'HEMI', 'STABLE', 'BABY', 'TST', 'YZY', 'PROMPT', 'DOOD', 'FOGO', 'NXPC', 'INIT', 'APEX', 'WLFI',  # batch 2
    'MAVIA', 'HMSTR', 'ZEREBRO', 'BLAST', 'BOME', 'MANTA', 'CHILLGUY', 'RSR', 'MELANIA', 'SCR', 'BIO', 'TNSR', 'MINA', 'NOT', 'BRETT', 'ME', 'IOTA', 'DYM', 'ORDI', 'POPCAT', 'SAGA', 'FIL', 'REZ', 'BANANA', 'kNEIRO', 'GMT', 'NEO', 'MAV',
    # Tier 3 expansion (+50)
    'RENDER','RUNE','STX','CAKE','ETC','MKR','ZEC','NEO','IMX',
    'MINA','ICP','GMX',
    'FXS','DYDX','SNX','CRV','COMP','ILV',
    'TURBO','MEW','GOAT','PNUT','KAS','MEME','NEIROETH',
    'HBAR','TRX','MANTA','HMSTR','SEI','ZK'

]

CHASE_GATE_COINS = {'BTC','BNB','DOT','ATOM','SUI','LDO','INJ','UMA','ALGO',
                    'BLUR','VVV','APE','OP','TON','TIA','LTC','MOODENG',
                    'AR','GALA','VIRTUAL'}
CHASE_LOOKBACK = 20


# ═══════════════════════════════════════════════════════
# PER-TICKER GATES — grid-optimized for 90%+ WR
# Each ticker has: gate_buy, gate_sell, cloud, body, lookback
# ═══════════════════════════════════════════════════════
# json already imported at top
_gates_path = os.path.join(os.path.dirname(__file__), 'ticker_gates.json')
if os.path.exists(_gates_path):
    TICKER_GATES = json.load(open(_gates_path))
    print(f"Loaded {len(TICKER_GATES)} per-ticker gate configs", flush=True)
else:
    TICKER_GATES = {}
    print("WARNING: ticker_gates.json not found, running without per-ticker gates", flush=True)

# V3 trend gate (4H EMA9 direction) — applied first in apply_ticker_gate
_HTF_CACHE = {}
HTF_CACHE_SEC = 900  # 15 min — 4h bars close every 4h, 15m cache fine

def fetch_htf(coin, interval='4h', bars=30):
    now = time.time()
    k = f"{coin}_{interval}"
    c = _HTF_CACHE.get(k)
    if c and now - c['ts'] < HTF_CACHE_SEC:
        return c['data']
    sec_map = {'1h':3600,'4h':14400,'15m':900,'5m':300}
    sec = sec_map.get(interval, 14400)
    end = int(time.time()*1000)
    start = end - bars*sec*1000
    try:
        d = info.candles_snapshot(coin, interval, start, end)
        result = [(int(x['t']), float(x['o']), float(x['h']), float(x['l']), float(x['c']), float(x['v'])) for x in d]
        _HTF_CACHE[k] = {'data': result, 'ts': now}
        return result
    except Exception as e:
        if '429' in str(e) and c: return c['data']
        log(f"htf err {coin} {interval}: {e}")
        return []

V3_ENABLED = False  # OOS 14d: V3 ON +108% gain, V3 OFF +173% (+65pp). Regime-aware system already handles trend context per-coin per-regime; V3 was double-filtering and blocking valid signals.
V3_HTF = '4h'
V3_EMA = 9

V3_BUFFER = 0.01  # 2% — only block extreme trend — only block when clearly in opposite trend

def trend_gate(coin, side):
    """V3: block BUY if 4H close < 4H EMA9 * (1-buffer), SELL if above EMA * (1+buffer)."""
    if not V3_ENABLED: return True
    htf = fetch_htf(coin, V3_HTF, V3_EMA * 3 + 5)
    if len(htf) < V3_EMA + 2: return True
    closes = [b[4] for b in htf]
    k = 2/(V3_EMA+1)
    ema = sum(closes[:V3_EMA])/V3_EMA
    for c in closes[V3_EMA:]:
        ema = c*k + ema*(1-k)
    last = closes[-1]
    if side == 'BUY' and last < ema * (1 - V3_BUFFER): return False
    if side == 'SELL' and last > ema * (1 + V3_BUFFER): return False
    return True

USE_GRID_GATE = False  # overfit layer disabled; V3 + ATR-min do the filtering

def apply_ticker_gate(coin, side, price, candles):
    """V3 trend + ATR-min filter. Returns True if passes."""
    # EMERGENCY: directional imbalance check — if 10+ shorts already open and BTC up, block new shorts
    try:
        if side == 'SELL':
            lp = get_all_positions_live()
            shorts = sum(1 for k,v in lp.items() if v.get('size',0) < 0)
            if shorts >= 10:
                # Check BTC 15min move
                btc_state = btc_correlation.get_state()
                if btc_state.get('btc_move', 0) > 0.002 or btc_state.get('btc_dir', 0) > 0:
                    log(f"{coin} SELL BLOCKED: {shorts} shorts open + BTC up")
                    return False
    except Exception: pass
    if not trend_gate(coin, side):
        log(f"{coin} {side} BLOCKED by V3 trend")
        return False
    if candles and len(candles) >= 15:
        trs = []
        for j in range(1, min(15, len(candles))):
            h,l,c = candles[-j][2], candles[-j][3], candles[-j][4]
            pc = candles[-j-1][4]
            trs.append(max(h-l, abs(h-pc), abs(l-pc)))
        if trs:
            atr_val = sum(trs)/len(trs)
            last_c = candles[-1][4]
            if last_c>0 and atr_val/last_c < 0.001:
                log(f"{coin} {side} BLOCKED by ATR-min ({atr_val/last_c*100:.2f}%)")
                return False
    # Funding filter — block expensive-carry trades
    if not funding_filter.allow_side(coin, side):
        log(f"{coin} {side} BLOCKED by funding rate")
        return False
    # BTC correlation — block alt trades against strong BTC direction
    if not btc_correlation.allow_alt_trade(coin, side):
        log(f"{coin} {side} BLOCKED by BTC correlation")
        return False
    if not USE_GRID_GATE:
        return True
    key = coin.upper().replace('.P','')
    # Try: exact, +USDT, strip k prefix +USDT (kBONK→BONKUSDT, kPEPE→PEPEUSDT)
    gate = TICKER_GATES.get(key) or TICKER_GATES.get(key + 'USDT')
    if not gate and key.startswith('K'):
        gate = TICKER_GATES.get(key[1:] + 'USDT')
    if not gate:
        log(f"{coin} NO GATE CONFIG — signal passes ungated")
        return True
    
    glb = gate.get('glb', 20)
    
    # Chase gate buy
    if gate.get('gb') and side == 'BUY' and candles and len(candles) > glb:
        window = candles[-glb:]
        hi = max(c[2] for c in window)
        if price > hi:
            return False
    
    # Chase gate sell
    if gate.get('gs') and side == 'SELL' and candles and len(candles) > glb:
        window = candles[-glb:]
        lo = min(c[3] for c in window)
        if price < lo:
            return False
    
    # Cloud filter
    if gate.get('cloud') and candles and len(candles) >= 50:
        closes = [c[4] for c in candles]
        k = 2/51; ema50 = sum(closes[:50])/50
        for j in range(50, len(closes)):
            ema50 = closes[j]*k + ema50*(1-k)
        k2 = 2/21; ema20 = sum(closes[:20])/20
        for j in range(20, len(closes)):
            ema20 = closes[j]*k2 + ema20*(1-k2)
        if side == 'BUY' and ema20 < ema50:
            return False
        if side == 'SELL' and ema20 > ema50:
            return False
    
    # Body filter
    if gate.get('body', 0) > 0 and candles and len(candles) > 0:
        last = candles[-1]
        br = last[2] - last[3]
        if br > 0 and abs(last[4] - last[1]) / br < gate['body']:
            return False
    
    return True

GRID = {'sens':1, 'rsi':10, 'wick':1, 'ext':1, 'block':1, 'vol':1, 'cd':3}  # tuner-overridden below

def derive(s):
    return {'rsi_hi': 50 + s['rsi']*3, 'rsi_lo': 50 - s['rsi']*3,
            'pivot_lb': max(2, 9 - s['sens']), 'cd': s['cd']}
SP = derive(GRID); BP = derive(GRID)
# TUNER WINNER OVERRIDE — plb=36 rsi=70/35
SP['pivot_lb'] = 15  # OOS: plb=15 lifts PnL +5%, matches trail 0.8% winner
BP['pivot_lb'] = 15
SP['rsi_hi'] = 70  # tight: quality over quantity in chop
BP['rsi_lo'] = 35


INITIAL_RISK_PCT = 0.02  # halved: 2x position slots      # 4% — aggressive (tuner-validated 68% WR)
SCALED_RISK_PCT  = 0.005
SCALE_DOWN_AT    = 50000
LEV = 10
LOOP_SEC = 2  # tight outer loop (Bybit WS push)
USE_ISOLATED_MARGIN = True

TP_MULTIPLIER = 1.0  # Set to 1.0 — TPs now OOS-tuned PER COIN (no global multiplier needed).
                     # Per-coin 15m OOS optimization: PROMPT 10%, ETH 10%, ALT 6%, ASTER 6%, etc.
                     # Prior value 2.0 was bandaid before per-coin tuning existed.
MAX_POSITIONS = 80  # was 40 — with 5/3/3/3 risk we can support more concurrent
MAX_SAME_SIDE = 40  # was 15 — let regime-aware system pick directions
MAX_TOTAL_RISK = 0.92    # 8% reserve
STOP_LOSS_PCT = 0.02      # 2% — tuner winner config
BTC_VOL_THRESHOLD = 0.03

MAX_HOLD_SEC = 99999 * 3600  # max hold disabled — OOS showed forced exits cost performance
CB_CONSEC_LOSSES = 999  # disabled per user principle
CB_PAUSE_SEC = 600  # 10min (was 60min — too long, cloud exit was triggering it)
FUNDING_CUT_RATIO = 0.50

TRAIL_PCT = 0.015          # OOS winner: +250% vs +40% at 0.3%
TRAIL_TIGHTEN_AFTER_SEC = 7200  # 2h: tighten trail to 0.9% (OOS +77% PnL vs static)
TRAIL_TIGHTEN_PCT = 0.009          # OOS winner: +250% vs +40% at 0.3%
MAKER_FALLBACK_SEC = 10
MAKER_OFFSET = 0.0015  # OOS winner: +21.22%/day  # 0.1% entry split — OOS +127% PnL (better avg entry)

def _init_hl_with_retry(max_attempts=8):
    """Retry Info() init with exponential backoff — Hyperliquid 429s on cold deploys."""
    import time as _t
    for attempt in range(max_attempts):
        try:
            return Info(constants.MAINNET_API_URL, skip_ws=True)
        except Exception as e:
            msg = str(e)
            if '429' in msg or 'rate' in msg.lower():
                wait = min(60, 3 * (2 ** attempt))
                print(f"[HL init] 429 rate-limited, retry {attempt+1}/{max_attempts} in {wait}s", flush=True)
                _t.sleep(wait)
                continue
            raise
    raise RuntimeError("Hyperliquid Info() init failed after retries")

def _init_exchange_with_retry(account, max_attempts=8):
    """Retry Exchange() init with exponential backoff — HL 429s on cold deploys."""
    import time as _t
    for attempt in range(max_attempts):
        try:
            return Exchange(account, constants.MAINNET_API_URL, account_address=WALLET)
        except Exception as e:
            msg = str(e)
            if '429' in msg or 'rate' in msg.lower():
                wait = min(60, 3 * (2 ** attempt))
                print(f"[HL exch init] 429 rate-limited, retry {attempt+1}/{max_attempts} in {wait}s", flush=True)
                _t.sleep(wait)
                continue
            raise
    raise RuntimeError("Hyperliquid Exchange() init failed after retries")

info = _init_hl_with_retry()
account = Account.from_key(PRIV_KEY)
exchange = _init_exchange_with_retry(account)

_META_CACHE = None
def _get_sz_decimals(coin):
    """Perps: price <= 5 sig figs AND <= (MAX_DECIMALS - szDecimals) decimals. MAX_DECIMALS=6 for perps."""
    global _META_CACHE
    if _META_CACHE is None:
        try:
            m = info.meta()
            _META_CACHE = {u['name']: int(u.get('szDecimals',0)) for u in m['universe']}
        except Exception: _META_CACHE = {}
    return _META_CACHE.get(coin, 2)

def round_price(coin, px):
    """HL-compliant price rounding: max 5 sig figs AND max (6 - szDecimals) decimals."""
    szD = _get_sz_decimals(coin)
    max_dec = max(0, 6 - szD)
    # First: 5 significant figures
    if px > 0:
        import math
        sig_scale = 10 ** (5 - int(math.floor(math.log10(abs(px)))) - 1)
        px_sig = round(px * sig_scale) / sig_scale
    else: px_sig = px
    # Then: max_dec decimal places
    return round(px_sig, max_dec)

def round_size(coin, sz):
    szD = _get_sz_decimals(coin)
    return round(sz, szD)

def log(m):
    msg = f"[{datetime.utcnow().isoformat()}] {m}"
    print(msg, flush=True)
    LOG_BUFFER.append(msg)
    if len(LOG_BUFFER) > 200: LOG_BUFFER.pop(0)

def current_risk_pct(equity):
    return SCALED_RISK_PCT if equity >= SCALE_DOWN_AT else INITIAL_RISK_PCT

# ═══════════════════════════════════════════════════════
# STATE — atomic write, rich position tracking (FIX #4)
# ═══════════════════════════════════════════════════════
def load_state():
    default = {'positions':{}, 'cooldowns':{}, 'consec_losses':0, 'cb_pause_until':0, 'last_pnl_close':None, 'cd_format':'ts'}
    try:
        # Try primary path, fall back to backup
        path = STATE_PATH if os.path.exists(STATE_PATH) else STATE_PATH + '.bak'
        with open(path) as f:
            loaded = json.load(f)
        if loaded.get('cd_format') != 'ts':
            loaded['cooldowns'] = {}
            loaded['cd_format'] = 'ts'
        for k,v in default.items():
            if k not in loaded: loaded[k]=v
        # Auto-scrub bogus stats (any bucket pnl > 100% is impossible, drop it)
        s = loaded.get('stats', {})
        if s:
            for bucket_name in ['by_engine','by_hour','by_side','by_coin','by_conf']:
                bucket = s.get(bucket_name, {})
                for k,v in list(bucket.items()):
                    if abs(v.get('pnl',0)) > 100:
                        bucket.pop(k, None)
            if abs(s.get('total_pnl',0)) > 200:
                s['total_pnl'] = 0; s['total_wins'] = 0; s['total_losses'] = 0
        return loaded
    except Exception: return default

def save_state(s):
    """Atomic write with backup for deploy resilience."""
    os.makedirs('/var/data', exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    with open(tmp,'w') as f: json.dump(s,f)
    os.replace(tmp, STATE_PATH)
    # Backup copy survives if primary is lost on deploy
    try:
        import shutil; shutil.copy2(STATE_PATH, STATE_PATH + '.bak')
    except Exception: pass

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

_CANDLE_CACHE = {}  # {coin: {'data': [...], 'ts': float}}
CANDLE_CACHE_SEC = 120  # 2 min cache — covers both BOS and MR scans in same cycle

def fetch(coin, n_bars=100, retries=3):
    """Bybit WS candles FIRST (no rate limit), HL REST only as fallback."""
    now = time.time()
    # Try Bybit WS candle buffer first
    try:
        if bybit_ws.has_coin(coin):
            by_candles = bybit_ws.get_candles(coin, limit=n_bars+50)
            if len(by_candles) >= n_bars:
                return by_candles[-n_bars:]
    except Exception:
        pass
    # Cached HL REST fallback
    cached = _CANDLE_CACHE.get(coin)
    if cached and now - cached['ts'] < CANDLE_CACHE_SEC:
        return cached['data']
    end=int(time.time()*1000); start=end-n_bars*5*60*1000
    for attempt in range(retries):
        try:
            d=info.candles_snapshot(coin,'5m',start,end)
            result = [(int(c['t']),float(c['o']),float(c['h']),float(c['l']),float(c['c']),float(c['v'])) for c in d]
            _CANDLE_CACHE[coin] = {'data': result, 'ts': time.time()}
            return result
        except Exception as e:
            es = str(e)
            if '429' in es and attempt < retries-1:
                time.sleep(1.5 + random.random()*1.5)
                continue
            log(f"candle err {coin}: {e}"); return []
    return []

SCAN_BARS = 12  # scan last 12 bars to catch signals after warmup
CD_MS = 30 * 60 * 1000  # 30 min cooldown — prevents rapid signal re-fire + opposite-exit storm

def chase_gate_ok(side, price, candles, i):
    """Reject entries chasing extended moves.
    Returns True if entry is allowed, False if it should be skipped.
    Only called for coins in CHASE_GATE_COINS."""
    if i < CHASE_LOOKBACK: return True  # not enough history yet
    window = candles[max(0, i-CHASE_LOOKBACK):i]
    if not window: return True
    hi20 = max(c[2] for c in window)
    lo20 = min(c[3] for c in window)
    if hi20 <= lo20: return True
    if side == 'BUY' and price > hi20:
        return False  # chasing upside breakout
    if side == 'SELL' and price < lo20:
        return False  # chasing downside breakdown
    return True


# Trend-pullback signal engine (OOS: n=279 WR=84.9% PnL=+105.83% PF=9.83 on 14d)
# HL-specific 5m-based constants (distinct from MT4 1h PB_* above)
HL_PB_EMA = 20
HL_PB_RSI_HI = 55
HL_PB_RSI_LO = 45
HL_PB_PROXIMITY = 0.003  # within 0.3% of 1H EMA20 derived from 5m resampled

def pullback_signal(coin, candles5, last_pb_buy_ts, last_pb_sell_ts):
    """Returns (side, bar_ts) or (None, None). Entry: 5m near 1H EMA20 + cooled RSI + 4H trend aligned."""
    if len(candles5) < 150: return None, None
    # Resample last 150 5m bars to 1h (groups of 12)
    n1h = len(candles5) // 12
    if n1h < HL_PB_EMA + 3: return None, None
    c1h = []
    for i in range(n1h):
        g = candles5[i*12:(i+1)*12]
        c1h.append(g[-1][4])
    # 1H EMA20
    k = 2/(HL_PB_EMA+1)
    ema1h = sum(c1h[:HL_PB_EMA])/HL_PB_EMA
    for cv in c1h[HL_PB_EMA:]:
        ema1h = cv*k + ema1h*(1-k)
    last_c = candles5[-1][4]
    if ema1h<=0: return None, None
    dist = abs(last_c - ema1h) / ema1h
    if dist > HL_PB_PROXIMITY: return None, None
    # RSI(14) on 5m
    closes = [b[4] for b in candles5]
    gains=[]; losses=[]
    for i in range(1,len(closes)):
        d=closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    p=14
    ag=sum(gains[:p])/p; al=sum(losses[:p])/p
    for i in range(p,len(gains)):
        ag=(ag*(p-1)+gains[i])/p; al=(al*(p-1)+losses[i])/p
    rs = ag/al if al>0 else 999
    r_last = 100-100/(1+rs)
    bar_ts = candles5[-1][0]
    # 4H trend — delegate to trend_gate (V3 already implements)
    trend_up = trend_gate(coin, 'SELL') == False  # if V3 blocks SELL, trend is up
    trend_dn = trend_gate(coin, 'BUY')  == False  # if V3 blocks BUY, trend is down
    buy_ok  = trend_up and r_last < HL_PB_RSI_HI and (bar_ts - last_pb_buy_ts) > CD_MS
    sell_ok = trend_dn and r_last > HL_PB_RSI_LO and (bar_ts - last_pb_sell_ts) > CD_MS
    if buy_ok:  return 'BUY', bar_ts
    if sell_ok: return 'SELL', bar_ts
    return None, None

def signal(candles, last_sell_ts, last_buy_ts, coin=None):
    """Scan last SCAN_BARS closed bars. Cooldown tracked by bar timestamp.
    Applies chase_gate for coins in CHASE_GATE_COINS."""
    if len(candles)<60: return None, None
    h=[c[2] for c in candles]; l=[c[3] for c in candles]; cl=[c[4] for c in candles]
    N=len(cl); r14=rsi_calc(cl,14)
    LB = SP['pivot_lb']
    apply_gate = coin in CHASE_GATE_COINS
    for i in range(max(LB, N-SCAN_BARS), N):
        if r14[i] is None: continue
        br = h[i]-l[i]
        if br <= 0: continue
        bar_ts = candles[i][0]
        is_pivot_high = h[i] == max(h[max(0,i-LB):i+1])
        is_pivot_low  = l[i] == min(l[max(0,i-LB):i+1])
        sell_ok = is_pivot_high and r14[i] > SP['rsi_hi'] and (bar_ts - last_sell_ts) > CD_MS
        buy_ok  = is_pivot_low  and r14[i] < BP['rsi_lo'] and (bar_ts - last_buy_ts)  > CD_MS
        if apply_gate:
            if sell_ok and not chase_gate_ok('SELL', cl[i], candles, i):
                sell_ok = False
            if buy_ok and not chase_gate_ok('BUY', cl[i], candles, i):
                buy_ok = False
        if sell_ok: return 'SELL', bar_ts
        if buy_ok:  return 'BUY',  bar_ts
    return None, None

def bb_signal(candles, coin=None, last_buy_ts=0, last_sell_ts=0):
    """Bollinger Band rejection signal. Mirrors OOS tuner logic.
    BUY: low breaks lower BB (2 SD), close back above lower BB, RSI near oversold
    SELL: high breaks upper BB (2 SD), close back below upper BB, RSI near overbought
    Returns (side, bar_ts) or (None, None).
    Enforces CD_MS cooldown from last_buy_ts/last_sell_ts to prevent signal storms."""
    if len(candles) < 40: return None, None
    h = [c[2] for c in candles]; l = [c[3] for c in candles]; cl = [c[4] for c in candles]
    N = len(cl); BB_P = 20
    r14 = rsi_calc(cl, 14)
    # Per-coin RL/RH if available, else globals
    RL = BP['rsi_lo']; RH = SP['rsi_hi']
    try:
        if coin and percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
            cfg = percoin_configs.get_config(coin)
            if cfg:
                RL = cfg.get('RL', RL); RH = cfg.get('RH', RH)
    except Exception: pass
    for i in range(max(BB_P+5, N-SCAN_BARS), N):
        if r14[i] is None: continue
        window = cl[i-BB_P:i]
        mean = sum(window)/BB_P
        var = sum((x-mean)**2 for x in window)/BB_P
        sd = var**0.5
        if sd <= 0: continue
        upper = mean + 2*sd; lower = mean - 2*sd
        bar_ts = candles[i][0]
        # BUY: pierced lower BB, closed back above, RSI in oversold zone
        if l[i] <= lower and cl[i] > lower and r14[i] < RL + 5 and (bar_ts - last_buy_ts) > CD_MS:
            return 'BUY', bar_ts
        # SELL: pierced upper BB, closed back below, RSI in overbought zone
        if h[i] >= upper and cl[i] < upper and r14[i] > RH - 5 and (bar_ts - last_sell_ts) > CD_MS:
            return 'SELL', bar_ts
    return None, None

def ib_signal(candles, coin=None, last_buy_ts=0, last_sell_ts=0):
    """Inside Bar breakout signal. Two consecutive inside bars, then breakout.
    BUY: close breaks above prior inner bar high
    SELL: close breaks below prior inner bar low
    Returns (side, bar_ts) or (None, None).
    Enforces CD_MS cooldown from last_buy_ts/last_sell_ts to prevent signal storms."""
    if len(candles) < 10: return None, None
    h = [c[2] for c in candles]; l = [c[3] for c in candles]; cl = [c[4] for c in candles]
    N = len(cl)
    for i in range(max(5, N-SCAN_BARS), N):
        if i < 4: continue
        inside1 = h[i-1] < h[i-2] and l[i-1] > l[i-2]
        inside2 = h[i-2] < h[i-3] and l[i-2] > l[i-3]
        if not (inside1 and inside2): continue
        bar_ts = candles[i][0]
        if cl[i] > h[i-1] and (bar_ts - last_buy_ts) > CD_MS: return 'BUY', bar_ts
        if cl[i] < l[i-1] and (bar_ts - last_sell_ts) > CD_MS: return 'SELL', bar_ts
    return None, None

def pass_per_coin_filter(coin, side, candles, i):
    """Apply per-coin ema200/adx25/adx20 filter from percoin_configs.
    Returns True if signal passes, False otherwise.
    For non-elite coins or coins without filter configured, always returns True."""
    try:
        if not (percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin)):
            return True
        cfg = percoin_configs.get_config(coin)
        if not cfg: return True
        flt = cfg.get('flt', 'none')
        if flt == 'none': return True
        cl = [c[4] for c in candles]
        h = [c[2] for c in candles]
        l = [c[3] for c in candles]
        N = len(cl)
        if 'ema200' in flt and N >= 200:
            k = 2/201
            e = sum(cl[:200])/200
            for j in range(200, i+1): e = cl[j]*k + e*(1-k)
            if side == 'BUY' and cl[i] < e: return False
            if side == 'SELL' and cl[i] > e: return False
        if 'ema50' in flt and N >= 50:
            k = 2/51
            e = sum(cl[:50])/50
            for j in range(50, i+1): e = cl[j]*k + e*(1-k)
            if side == 'BUY' and cl[i] < e: return False
            if side == 'SELL' and cl[i] > e: return False
        if 'adx' in flt and N >= 28:
            # Minimum ADX threshold (14-period Wilder's)
            threshold = 25 if 'adx25' in flt else 20
            P = 14
            # Compute recent ADX at index i
            tr_s = []; pdm_s = []; ndm_s = []
            for j in range(1, i+1):
                tr = max(h[j]-l[j], abs(h[j]-cl[j-1]), abs(l[j]-cl[j-1]))
                up = h[j]-h[j-1]; dn = l[j-1]-l[j]
                pdm = up if (up > dn and up > 0) else 0
                ndm = dn if (dn > up and dn > 0) else 0
                tr_s.append(tr); pdm_s.append(pdm); ndm_s.append(ndm)
            if len(tr_s) < 2*P: return False
            atr = sum(tr_s[:P])/P; spdm = sum(pdm_s[:P]); sndm = sum(ndm_s[:P])
            for j in range(P, len(tr_s)):
                atr = (atr*(P-1) + tr_s[j])/P
                spdm = spdm - spdm/P + pdm_s[j]
                sndm = sndm - sndm/P + ndm_s[j]
            pdi = 100*spdm/atr if atr>0 else 0
            ndi = 100*sndm/atr if atr>0 else 0
            dx = 100*abs(pdi-ndi)/(pdi+ndi) if (pdi+ndi)>0 else 0
            # Single-bar ADX approximation — compare DX vs threshold directly (noisy but fast)
            if dx < threshold: return False
        if 'conv' in flt and N >= 50:
            # CONVICTION STACK: adx_low + ema21_far + deep_os
            # OOS 15m: lifts Exp from $1.86 → $2.39 (+29%), WR 48.5% → 49.1%
            # Trade count drops 62% but every trade is higher quality
            # 1. ADX_LOW: only trade when ADX < 30 (mean-reversion regime)
            P = 14
            tr_s = []; pdm_s = []; ndm_s = []
            for j in range(max(1, i-60), i+1):
                tr = max(h[j]-l[j], abs(h[j]-cl[j-1]), abs(l[j]-cl[j-1]))
                up = h[j]-h[j-1]; dn = l[j-1]-l[j]
                pdm = up if (up > dn and up > 0) else 0
                ndm = dn if (dn > up and dn > 0) else 0
                tr_s.append(tr); pdm_s.append(pdm); ndm_s.append(ndm)
            if len(tr_s) >= 2*P:
                atr = sum(tr_s[:P])/P; spdm = sum(pdm_s[:P]); sndm = sum(ndm_s[:P])
                for j in range(P, len(tr_s)):
                    atr = (atr*(P-1) + tr_s[j])/P
                    spdm = spdm - spdm/P + pdm_s[j]
                    sndm = sndm - sndm/P + ndm_s[j]
                pdi = 100*spdm/atr if atr>0 else 0
                ndi = 100*sndm/atr if atr>0 else 0
                dx = 100*abs(pdi-ndi)/(pdi+ndi) if (pdi+ndi)>0 else 0
                if dx >= 30: return False  # ADX too high, trend regime — skip mean-rev
            # 2. EMA21_FAR: require >1% distance from 21 EMA
            k21 = 2/22
            if N >= 21:
                e21 = sum(cl[:21])/21
                for j in range(21, i+1): e21 = cl[j]*k21 + e21*(1-k21)
                if cl[i] > 0 and abs(cl[i]-e21)/cl[i] < 0.01: return False
            # 3. DEEP_OS: price must be outside 1.5σ band (not just touching 2σ)
            BB_P = 20
            if N >= BB_P:
                window = cl[i-BB_P:i]
                mu = sum(window)/BB_P
                var = sum((x-mu)**2 for x in window)/BB_P
                sd = var**0.5
                if sd > 0:
                    if side == 'BUY' and cl[i] > mu - 1.5*sd: return False
                    if side == 'SELL' and cl[i] < mu + 1.5*sd: return False
        return True
    except Exception:
        return True  # fail-open to avoid blocking trades on filter bugs

# ═══════════════════════════════════════════════════════
# HL INTERFACE
# ═══════════════════════════════════════════════════════
def get_balance():
    try: return float(_cached_user_state()['marginSummary']['accountValue'])
    except Exception: return 0

def get_total_margin():
    try: return float(_cached_user_state()['marginSummary'].get('totalMarginUsed', 0))
    except Exception: return 0

# ═══════════════════════════════════════════════════════
# API CACHE — reduces HL API calls from 100+/cycle to ~3/cycle
# ═══════════════════════════════════════════════════════
_cache = {'mids': None, 'mids_ts': 0, 'state': None, 'state_ts': 0}
CACHE_TTL = 5  # seconds

def _cached_mids():
    now = time.time()
    if _cache['mids'] is None or now - _cache['mids_ts'] > CACHE_TTL:
        try:
            _cache['mids'] = info.all_mids()
            _cache['mids_ts'] = now
        except Exception: pass
    return _cache['mids'] or {}

def _cached_user_state():
    now = time.time()
    if _cache['state'] is None or now - _cache['state_ts'] > CACHE_TTL:
        try:
            _cache['state'] = info.user_state(WALLET)
            _cache['state_ts'] = now
        except Exception: pass
    return _cache['state'] or {}

def get_mid(coin):
    try: return float(_cached_mids()[coin])
    except Exception: return None

_POSITIONS_CACHE = {'data': {}, 'ts': 0}
_SIGNAL_LOG = []  # ring buffer
_SIGNAL_LOG_LOCK = threading.Lock()
def log_signal(coin, kind, side=None):
    import datetime
    with _SIGNAL_LOG_LOCK:
        _SIGNAL_LOG.append({'coin':coin,'kind':kind,'side':side,
                            'ts': datetime.datetime.utcnow().strftime('%H:%M:%S')})
        if len(_SIGNAL_LOG) > 50: del _SIGNAL_LOG[:len(_SIGNAL_LOG)-50]

def get_all_positions_live(force=False):
    """Cached — refreshes once per tick (5s). Force=True for critical ops."""
    now = time.time()
    if not force and now - _POSITIONS_CACHE['ts'] < 4:
        return _POSITIONS_CACHE['data']
    """Returns dict of coin -> {size, entry, pnl, mark} for all actual positions on HL."""
    out={}
    try:
        for p in _cached_user_state().get('assetPositions',[]):
            pos=p['position']
            sz=float(pos.get('szi',0))
            if sz!=0:
                out[pos['coin']] = {
                    'size':sz,
                    'entry':float(pos['entryPx']),
                    'pnl':float(pos['unrealizedPnl']),
                    'mark':float(pos.get('positionValue',0)) / abs(sz) if sz else 0,
                    'lev':int(pos.get('leverage',{}).get('value',10)),
                    'upnl':float(pos['unrealizedPnl']),
                }
    except Exception as e:
        log(f"positions fetch err: {e}")
    _POSITIONS_CACHE['data'] = out
    _POSITIONS_CACHE['ts'] = time.time()
    return out

_FUNDING_CACHE = {'data': {}, 'ts': 0}
def get_funding_rate(coin):
    now = __import__('time').time()
    if now - _FUNDING_CACHE['ts'] < 900:  # cache 15 min
        return _FUNDING_CACHE['data'].get(coin, 0)
    """Fetch current funding rate for a coin (per hour). Negative = shorts pay, positive = longs pay."""
    try:
        meta = info.meta_and_asset_ctxs()
        asset_ctxs = meta[1]
        universe = meta[0]['universe']
        for i, u in enumerate(universe):
            if u['name']==coin and i<len(asset_ctxs):
                return float(asset_ctxs[i].get('funding', 0))
    except Exception: pass
    return 0

def calc_size(equity, px, risk_pct, risk_mult=1.0, coin=None, side='BUY'):
    # Per-coin leverage (BTC/ETH 20x, alts 3-10x)
    actual_lev = leverage_map.get_max(coin, default=LEV) if coin else LEV
    # News risk multiplier
    try: news_mult = news_filter.get_risk_mult()
    except Exception: news_mult = 1.0
    try: news_dir = news_filter.get_state().get('direction_bias', 0)
    except Exception: news_dir = 0
    # News + orderbook composite boost
    try: confluence = wall_confluence.composite_boost(coin, side, px, news_dir) if coin else 1.0
    except Exception: confluence = 1.0
    # Session scaler (London/NY 1.0x, Asia 0.7x)
    try: session_mult = session_scaler.get_mult()
    except Exception: session_mult = 1.0
    confluence *= session_mult
    try: whale_mult = whale_filter.confluence_boost(coin, side) if coin else 1.0
    except Exception: whale_mult = 1.0
    confluence *= whale_mult
    # CVD confluence: aligned buy/sell pressure boost
    try:
        cvd_sig = cvd_ws.cvd_signal(coin) if coin else None
        if cvd_sig == side: confluence *= 1.3
        elif cvd_sig and cvd_sig != side: confluence *= 0.7
    except Exception: pass
    # OI confluence: position-adding on our side = trend continuation
    try:
        if coin:
            # Simple price direction from recent candles not available here — use side as intent
            oi_delta = oi_tracker.get_delta(coin) if coin else 0
            if oi_delta > 0.02:  # OI rising >2%
                confluence *= 1.2
    except Exception: pass
    # Risk ladder override
    try: tier_risk = risk_ladder.get_risk()
    except Exception: tier_risk = risk_pct
    raw = equity * tier_risk * risk_mult * news_mult * confluence * actual_lev / px
    if raw>=100: return round(raw,0)
    if raw>=10:  return round(raw,1)
    if raw>=1:   return round(raw,2)
    if raw>=0.1: return round(raw,3)
    return round(raw,4)

def set_isolated_leverage(coin):
    """Set isolated margin + per-coin tier leverage before opening.
    Uses TIER_SIZING from percoin_configs (not global LEV).
    HL caps to coin's max leverage automatically."""
    try:
        # Get tier leverage (15 PURE / 12 NINETY_99 / 10 EIGHTY_89 / 10 SEVENTY_79)
        tier_lev, _ = percoin_configs.get_sizing(coin)
        # HL update_leverage(leverage, coin, is_cross). is_cross MUST be False for isolated.
        exchange.update_leverage(tier_lev, coin, is_cross=False)
    except Exception as e:
        log(f"lev set err {coin}: {e}")
        # Fallback: try global LEV if tier lookup fails
        try: exchange.update_leverage(LEV, coin, is_cross=False)
        except: pass

# ═══════════════════════════════════════════════════════
# TIER-PRIORITY BUMP — free margin from lower-tier positions for higher-tier signals
# ═══════════════════════════════════════════════════════
TIER_PRIO = {'PURE': 4, 'NINETY_99': 3, 'EIGHTY_89': 2, 'SEVENTY_79': 1}

def try_tier_bump(incoming_coin, state, live_positions):
    """If margin would reject incoming trade, bump the lowest-priority active positions below incoming tier.
    Only called when elite_mode and incoming coin is in whitelist.
    Returns (freed_margin_estimate_usd, count_bumped). Closes positions as side effect.

    Safety:
    - Only bumps coins with STRICTLY LOWER tier than incoming (PURE never bumped)
    - Stops bumping once enough margin freed
    - Never bumps more than 3 positions per signal (cascade guard)
    - Fail-safe on close() error — stops bumping, returns what was freed
    """
    if not percoin_configs.ELITE_MODE or not percoin_configs.is_elite(incoming_coin):
        return 0, 0
    incoming_tier = percoin_configs.get_tier(incoming_coin)
    if not incoming_tier: return 0, 0
    incoming_prio = TIER_PRIO.get(incoming_tier, 0)

    # Check current margin state
    try:
        us = _cached_user_state()
        total_margin = float(us['marginSummary'].get('totalMarginUsed', 0))
        account_value = float(us['marginSummary'].get('accountValue', 0))
        withdrawable = float(us.get('withdrawable', 0))
    except Exception:
        return 0, 0

    # Rough size of what we want to open (use risk_pct × equity as margin proxy)
    try:
        risk_pct = current_risk_pct(account_value)
        cfg = percoin_configs.get_config(incoming_coin) or {}
        # Use tier target risk matching new TIER_SIZING (5/3/3/3 — see percoin_configs.py)
        target_risk = {'PURE': 0.05, 'NINETY_99': 0.03, 'EIGHTY_89': 0.03, 'SEVENTY_79': 0.03}.get(incoming_tier, 0.03)
        needed_margin = account_value * target_risk
    except Exception:
        needed_margin = account_value * 0.15

    # If we have >= needed margin available, no bump needed
    if withdrawable >= needed_margin * 1.05:
        return 0, 0

    # Find bump candidates: active positions with lower tier than incoming
    candidates = []
    for coin, lp in live_positions.items():
        if coin == incoming_coin: continue
        sz = lp.get('size', 0)
        if sz == 0: continue
        tier = percoin_configs.get_tier(coin)
        if not tier: continue
        prio = TIER_PRIO.get(tier, 0)
        if prio >= incoming_prio: continue  # same or higher, skip
        # Estimate margin freed by closing this position
        entry = lp.get('entry', 0)
        if not entry: continue
        notional = abs(sz) * entry
        margin_used = notional / 5  # assume 5x avg lev post-resolver
        pnl = lp.get('pnl', 0)
        # Prefer bumping losers first (rank: lowest prio first, then most negative pnl)
        candidates.append((prio, pnl, margin_used, coin, tier))

    if not candidates:
        return 0, 0

    candidates.sort(key=lambda x: (x[0], x[1]))  # lowest tier first, then worst pnl first

    freed = 0
    count = 0
    MAX_BUMPS = 3
    for prio, pnl, margin, coin, tier in candidates:
        if count >= MAX_BUMPS: break
        if freed + withdrawable >= needed_margin * 1.05: break
        try:
            close(coin, state_ref=state)
            log(f"TIER-BUMP closed {coin} (tier={tier} pnl=${pnl:+.3f}) to free ~${margin:.0f} for incoming {incoming_coin} ({incoming_tier})")
            state.get('positions', {}).pop(coin, None)
            freed += margin
            count += 1
        except Exception as e:
            log(f"tier-bump close err {coin}: {e}")
            break

    if count > 0:
        log(f"TIER-BUMP: freed ~${freed:.0f} by closing {count} lower-tier positions for {incoming_coin}")
    return freed, count

def place(coin, is_buy, size):
    """HL-compliant price rounding + maker/taker handling."""
    px = get_mid(coin)
    if not px: return None
    set_isolated_leverage(coin)
    size = round_size(coin, size)
    if size <= 0:
        log(f"{coin} size rounded to 0 — skip"); return None

    # Bybit-lead limit: capture HL lag using Bybit's current price
    side = 'BUY' if is_buy else 'SELL'
    edge = bybit_lead.compute_edge_price(coin, side, px)
    if edge:
        maker_px = round_price(coin, edge)
    else:
        maker_px = round_price(coin, px * (1 - MAKER_OFFSET) if is_buy else px * (1 + MAKER_OFFSET))
    try:
        r = exchange.order(coin, is_buy, size, maker_px, {'limit':{'tif':'Alo'}}, reduce_only=False)
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in status:
            log(f"MAKER {coin} rejected: {status['error']} @ {maker_px}")
        elif 'resting' in status or 'filled' in status:
            log(f"MAKER {coin} {'BUY' if is_buy else 'SELL'} {size}@{maker_px}: {status}")
            oid = status.get('resting',{}).get('oid') or status.get('filled',{}).get('oid')
            if 'filled' in status: return maker_px
            for wait_s in range(MAKER_FALLBACK_SEC):
                time.sleep(1)
                state_now = info.user_state(WALLET)
                has_pos = any(p['position'].get('coin')==coin and float(p['position'].get('szi',0))!=0
                              for p in state_now.get('assetPositions',[]))
                if has_pos:
                    log(f"MAKER fill {coin} after {wait_s+1}s"); return maker_px
            try:
                exchange.cancel(coin, oid)
                log(f"MAKER unfilled {coin}, canceling oid={oid} -> TAKER fallback")
            except Exception as ce:
                log(f"cancel err {coin}: {ce}")
    except Exception as e:
        log(f"maker place err {coin}: {e}")

    # TAKER fallback (Ioc) — refresh price in case market moved
    px = get_mid(coin) or px
    slip_px = round_price(coin, px * (1.005 if is_buy else 0.995))
    try:
        r = exchange.order(coin, is_buy, size, slip_px, {'limit':{'tif':'Ioc'}}, reduce_only=False)
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in status:
            log(f"TAKER {coin} rejected: {status['error']} @ {slip_px}"); return None
        log(f"TAKER {coin} {'BUY' if is_buy else 'SELL'} {size}@{slip_px}: {status}")
        return px
    except Exception as e:
        log(f"taker err {coin}: {e}"); return None

def cancel_trigger_orders(coin):
    """Cancel any native SL/TP trigger orders for a coin — prevents orphaned stops."""
    try:
        open_orders = info.open_orders(WALLET)
        for o in open_orders:
            if o.get('coin') == coin:
                oid = o.get('oid')
                if oid:
                    exchange.cancel(coin, oid)
                    log(f"{coin} cancelled orphaned order {oid}")
    except Exception as e:
        log(f"{coin} cancel triggers err: {e}")

def close(coin, state_ref=None):
    """Returns realized pnl_pct for logging (FIX #11)."""
    live = get_all_positions_live(force=True).get(coin)
    if not live: return None
    is_buy=live['size']<0; size=abs(live['size']); px=get_mid(coin)
    if not px: return None
    size = round_size(coin, size)
    slip = round_price(coin, px * (1.005 if is_buy else 0.995))
    try:
        r=exchange.order(coin,is_buy,size,slip,{'limit':{'tif':'Ioc'}},reduce_only=True)
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in status:
            log(f"CLOSE {coin} FAILED: {status['error']}"); return None
        entry = live['entry']
        pct = ((px-entry)/entry*100) if live['size']>0 else ((entry-px)/entry*100)
        pnl_usd = live['pnl']
        log(f"CLOSE {coin} {size}@{slip} | entry={entry} exit={px} | {pct:+.2f}% | ${pnl_usd:+.3f}")
        log_trade('HL', coin, 'CLOSE', px, pnl_usd, 'close')
        cancel_trigger_orders(coin)  # Kill orphaned SL orders
        # Monitor hook
        try:
            import monitor
            monitor.record_close(coin, pct/100, pnl_usd, 0, 'close')
        except Exception: pass
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
def place_native_sl(coin, is_long, entry, size):
    """Place HL native stop-loss order — executes server-side, no tick delay.
    Uses per-coin SL from percoin_configs if available (OOS-tuned), else global fallback."""
    try:
        # Per-coin SL from OOS tuning (5% default — validated WR)
        sl_pct = STOP_LOSS_PCT  # global fallback 2%
        try:
            if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
                cfg = percoin_configs.get_config(coin)
                if cfg and 'SL' in cfg:
                    sl_pct = cfg['SL']  # OOS-validated per-coin SL
        except Exception: pass
        entry = float(entry); size = float(size)
        trigger_px = entry * (1 - sl_pct) if is_long else entry * (1 + sl_pct)
        trigger_px = float(round_price(coin, trigger_px))
        # Limit price: aggressive to ensure fill (2% past trigger for slippage room)
        limit_px = float(round_price(coin, trigger_px * (0.98 if not is_long else 1.02)))
        sl_size = float(round_size(coin, size))
        sl_side = not is_long
        r = exchange.order(coin, sl_side, sl_size, limit_px,
                       {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "sl"}},
                       reduce_only=True)
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in status:
            log(f"{coin} NATIVE SL REJECTED: {status['error']}")
        else:
            log(f"{coin} NATIVE SL placed @ {trigger_px} (sl_pct={sl_pct*100:.1f}%)")
    except Exception as e:
        log(f"{coin} native SL err: {e}")

def place_native_tp(coin, is_long, entry, size):
    """Place HL native take-profit order using per-coin TP from OOS tuning."""
    try:
        if not (percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin)):
            return  # no per-coin TP for non-elite
        cfg = percoin_configs.get_config(coin)
        if not cfg or 'TP' not in cfg: return
        tp_pct = cfg['TP']
        entry = float(entry); size = float(size)
        trigger_px = entry * (1 + tp_pct) if is_long else entry * (1 - tp_pct)
        trigger_px = float(round_price(coin, trigger_px))
        # Limit: slightly worse to ensure fill
        limit_px = float(round_price(coin, trigger_px * (0.998 if is_long else 1.002)))
        tp_size = float(round_size(coin, size))
        tp_side = not is_long
        r = exchange.order(coin, tp_side, tp_size, limit_px,
                       {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "tp"}},
                       reduce_only=True)
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in status:
            log(f"{coin} NATIVE TP REJECTED: {status['error']}")
        else:
            log(f"{coin} NATIVE TP placed @ {trigger_px} (tp_pct={tp_pct*100:.2f}%)")
    except Exception as e:
        log(f"{coin} native TP err: {e}")

def process(coin, state, equity, live_positions, risk_mult=1.0):
    if coin_disabled(coin, state): return
    candles=fetch(coin)
    last_s=state['cooldowns'].get(coin+'_sell', 0)
    last_b=state['cooldowns'].get(coin+'_buy',  0)
    sig, bar_ts = signal(candles, last_s, last_b, coin=coin)
    signal_engine = 'PIVOT' if sig else None

    # PER-COIN FILTER: for ELITE coins, apply tuned sigs whitelist + filter
    # Each coin's allowed sigs list and filter (ema200/adx25/etc) comes from OOS tuning
    if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
        elite_cfg_check = percoin_configs.get_config(coin)
        allowed = set(elite_cfg_check.get('sigs', [])) if elite_cfg_check else set()
        # If PIVOT signal fired but coin doesn't allow PV, drop it
        if sig and 'PV' not in allowed:
            # silent — per-coin filter intentionally blocks wrong sig engine
            sig = None; signal_engine = None
        # Try BB_REJ if allowed and nothing fired
        if not sig and 'BB' in allowed:
            try:
                sig, bar_ts = bb_signal(candles, coin=coin, last_buy_ts=last_b, last_sell_ts=last_s)
                if sig: signal_engine = 'BB_REJ'
            except Exception as e:
                log(f"bb_signal err {coin}: {e}")
        # Try INSIDE_BAR if allowed and nothing fired
        if not sig and 'IB' in allowed:
            try:
                sig, bar_ts = ib_signal(candles, coin=coin, last_buy_ts=last_b, last_sell_ts=last_s)
                if sig: signal_engine = 'INSIDE_BAR'
            except Exception as e:
                log(f"ib_signal err {coin}: {e}")
        # Apply per-coin filter (ema200/adx20/adx25 as configured)
        if sig:
            # Find bar index for bar_ts
            idx = len(candles) - 1
            for j in range(len(candles)-1, -1, -1):
                if candles[j][0] == bar_ts: idx = j; break
            if not pass_per_coin_filter(coin, sig, candles, idx):
                flt = elite_cfg_check.get('flt', 'none') if elite_cfg_check else 'none'
                log(f"{coin} {sig} {signal_engine} FILTERED — failed {flt}")
                sig = None; signal_engine = None
    elif percoin_configs.ELITE_MODE and not percoin_configs.is_elite(coin):
        # Coin not in 139-whitelist: hard-block. Do not trade.
        if sig:
            log(f"{coin} {sig} BLOCKED — not in 139-coin elite whitelist")
        sig = None; signal_engine = None

    # Opposite-signal exit: if we hold OPPOSITE-SIDE position AND it's profitable, close it first.
    # Skip OPP-EXIT on losing positions — let native SL handle them.
    # This prevents signal-storm from locking in small losses when signals oscillate mid-bar.
    if sig and coin in state.get('positions', {}):
        pos = state['positions'][coin]
        if pos and ((pos.get('side')=='L' and sig=='SELL') or (pos.get('side')=='S' and sig=='BUY')):
            # Check current PnL — only flip if we're in profit
            try:
                live = live_positions.get(coin, {})
                cur_pnl = live.get('pnl', 0) if live else 0
                if cur_pnl > 0.10:  # only flip if at least $0.10 in profit
                    close(coin)
                    log(f"{coin} OPP-EXIT on {sig} signal (locking +${cur_pnl:.3f})")
                else:
                    # Don't flip a losing or flat position — let SL work
                    log(f"{coin} OPP-EXIT skipped: pos at ${cur_pnl:+.3f} not profitable enough to flip")
                    sig = None; signal_engine = None
            except Exception as e:
                log(f"opp-exit err {coin}: {e}")
                sig = None; signal_engine = None
    # Secondary: pullback engine (OOS 84.9% WR / PF 9.83)
    if not sig:
        try:
            pb_s = state['cooldowns'].get(coin+'_pb_sell', 0)
            pb_b = state['cooldowns'].get(coin+'_pb_buy', 0)
            sig, bar_ts = pullback_signal(coin, candles, pb_b, pb_s)
            if sig:
                signal_engine = 'PULLBACK'
                key = coin + ('_pb_buy' if sig=='BUY' else '_pb_sell')
                state['cooldowns'][key] = bar_ts
        except Exception as e:
            log(f"pullback err {coin}: {e}")
    # Tertiary: wall-bounce retest engine (requires verified OB + V3 alignment)
    if not sig:
        try:
            # Infer V3 direction from trend_gate checks
            v3_dir = 0
            if trend_gate(coin, 'BUY') and not trend_gate(coin, 'SELL'): v3_dir = 1
            elif trend_gate(coin, 'SELL') and not trend_gate(coin, 'BUY'): v3_dir = -1
            cur_px = get_mid(coin)
            wb_side, wb_wall = wall_bounce.check(coin, cur_px, v3_dir)
            if wb_side:
                sig = wb_side; bar_ts = int(time.time()*1000); signal_engine = 'WALL_BNC'
                state.setdefault('wall_entries', {})[coin] = {
                    'side': wb_side, 'wall_price': wb_wall['price'],
                    'wall_usd': wb_wall['usd'], 'entry_ts': time.time()}
                log(f"WALL-BOUNCE {coin} {wb_side} @ wall ${wb_wall['usd']/1000:.0f}k p={wb_wall['price']}")
        except Exception as e:
            log(f"wall_bounce err {coin}: {e}")
    # Quaternary: liquidation cascade fade
    if not sig:
        try:
            casc = liquidation_ws.get_cascade(coin, max_age_sec=180)
            if casc:
                sig = casc['fade_direction']; bar_ts = int(time.time()*1000); signal_engine = 'LIQ_CSCD'
                log(f"LIQ-CASCADE {coin} fade {sig} (${casc['total_usd']/1e6:.1f}M liqs)")
        except Exception as e:
            log(f"liq cascade err {coin}: {e}")
    # Quinary: spoof detection fade
    if not sig:
        try:
            sp = spoof_detection.get_spoof_signal(coin)
            if sp:
                sig = sp['direction']; bar_ts = int(time.time()*1000); signal_engine = 'SPOOF'
                spoof_detection.mark_fired(coin)
                log(f"SPOOF-FADE {coin} {sig} (wall ${sp['original_wall']/1000:.0f}k→${sp['remaining']/1000:.0f}k)")
        except Exception as e:
            log(f"spoof err {coin}: {e}")
    cur=state['positions'].get(coin)
    live=live_positions.get(coin)

    # Position management: SL, trail, funding checks
    if cur and live:

        mark = get_mid(coin)
        if mark and cur.get('entry'):
            entry = cur['entry']
            side = cur['side']
            fav = (mark - entry) / entry if side == 'L' else (entry - mark) / entry

            # Per-coin hard stop — uses OOS-validated SL from percoin_configs if available
            sl_pct = STOP_LOSS_PCT  # global 2% fallback
            try:
                if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
                    _cfg = percoin_configs.get_config(coin)
                    if _cfg and 'SL' in _cfg:
                        sl_pct = _cfg['SL']  # OOS-validated per-coin SL (typically 5%)
            except Exception: pass

            if fav <= -sl_pct:
                prev_pos = dict(cur)
                pnl_pct = close(coin)
                if pnl_pct is not None:
                    record_close(prev_pos, coin, pnl_pct, state)
                    state['consec_losses'] += 1
                    state['last_pnl_close'] = pnl_pct
                log(f"{coin} STOP LOSS {fav*100:.2f}% (limit -{sl_pct*100:.1f}%)")
                state['positions'].pop(coin, None)
                return

            # PER-COIN TP-LOCK — once TP reached, it becomes the new SL floor.
            # Price can run ABOVE TP freely, but if it retraces BELOW TP it exits with that locked profit.
            # This lets winners ride while guaranteeing minimum TP gain once reached.
            # TP_MULTIPLIER widens TPs to hit $5+ avg win target (OOS validates ×2 = $25 avg win @ $75 margin).
            tp_pct = None
            try:
                if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
                    _cfg = percoin_configs.get_config(coin)
                    if _cfg and 'TP' in _cfg:
                        tp_pct = _cfg['TP'] * TP_MULTIPLIER
            except Exception: pass

            # HWM tracking for trail
            hwm = cur.get('hwm', fav)
            if fav > hwm:
                hwm = fav
                cur['hwm'] = hwm

            # TP-LOCK state: once TP touched, mark the position as locked
            tp_locked = cur.get('tp_locked', False)
            if tp_pct is not None and not tp_locked and fav >= tp_pct:
                cur['tp_locked'] = True
                tp_locked = True
                log(f"{coin} TP-LOCK armed at +{fav*100:.2f}% (TP={tp_pct*100:.2f}%). Floor locked.")

            # If TP-locked, exit when price retraces back to TP level
            if tp_locked and tp_pct is not None and fav < tp_pct:
                prev_pos = dict(cur)
                pnl_pct = close(coin)
                if pnl_pct is not None:
                    record_close(prev_pos, coin, pnl_pct, state)
                    state['consec_losses'] = 0
                    state['last_pnl_close'] = pnl_pct
                log(f"{coin} TP-LOCK EXIT +{fav*100:.2f}% (peak +{hwm*100:.2f}%, TP floor {tp_pct*100:.2f}%)")
                state['positions'].pop(coin, None)
                return

            # TRAIL (secondary): only active AFTER tp-lock, to capture runs past TP
            # Uses tighter 0.8% trail to not give back too much.
            # Before TP-lock: no trail exit (let it work toward TP or SL).
            # After TP-lock: 0.8% trail from peak on top of locked TP floor.
            if tp_locked and hwm > (tp_pct + TRAIL_PCT):
                age = time.time() - (cur.get('opened_at') or time.time())
                trl = TRAIL_TIGHTEN_PCT if age > TRAIL_TIGHTEN_AFTER_SEC else TRAIL_PCT
                if (hwm - fav) >= trl:
                    prev_pos = dict(cur)
                    pnl_pct = close(coin)
                    if pnl_pct is not None:
                        record_close(prev_pos, coin, pnl_pct, state)
                        state['consec_losses'] = 0
                        state['last_pnl_close'] = pnl_pct
                    log(f"{coin} TRAIL EXIT +{fav*100:.2f}% (peak +{hwm*100:.2f}%, trail {trl*100:.2f}%, post-TP-lock)")
                    state['positions'].pop(coin, None)
                    return

    # 4h max hold check
    if cur and cur.get('opened_at'):
        age = time.time() - cur['opened_at']
        if age > MAX_HOLD_SEC:
            log(f"{coin} MAX HOLD exceeded ({age/3600:.1f}h) — force close (does NOT count as loss)")
            pnl_pct = close(coin)
            if pnl_pct is not None:
                # MAX HOLD closes never trigger circuit breaker
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
        # Before hard-skipping: try to close positions to make room
        if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
            incoming_tier = percoin_configs.get_tier(coin)
            DUST_USD = 0.10
            # Tier priority for closing: SEVENTY_79 → EIGHTY_89 → NINETY_99 → PURE
            # Higher-tier incoming signals can claim lower-tier positions.
            # PURE: never close anything (100% WR needs room but don't sacrifice 90%+ edge)
            # Rule: can close a tier STRICTLY LOWER than incoming tier, OR dust of any tier
            TIER_RANK = {'PURE': 4, 'NINETY_99': 3, 'EIGHTY_89': 2, 'SEVENTY_79': 1}
            incoming_rank = TIER_RANK.get(incoming_tier, 0)
            # Categorize candidates
            # dust_cands: list of (abs_pnl_usd, k, notional, pnl, tier) — can always close
            # profit_by_tier: dict of tier -> list of (pnl_usd, k, notional) — tier-ranked sacrifice
            dust_cands = []
            profit_by_tier = {'SEVENTY_79': [], 'EIGHTY_89': [], 'NINETY_99': [], 'PURE': []}
            for k, lp in live_positions.items():
                if k == coin: continue
                sz = lp.get('size', 0); entry = lp.get('entry', 0)
                if sz == 0 or not entry: continue
                pos_tier = percoin_configs.get_tier(k) or 'NONE'
                pos_pnl = lp.get('pnl', 0)  # USD, from HL state (more reliable than get_mid which 429s)
                notional = abs(sz) * entry
                # DUST: |pnl| ≤ $0.10 — always close regardless of tier
                if abs(pos_pnl) <= DUST_USD:
                    dust_cands.append((abs(pos_pnl), k, notional, pos_pnl, pos_tier))
                # PROFIT ≥ $0.10: tier-ranked
                elif pos_pnl > DUST_USD and pos_tier in profit_by_tier:
                    profit_by_tier[pos_tier].append((pos_pnl, k, notional))
            
            closed_one = False
            # Phase 1: sweep ALL dust (no edge sacrificed)
            for _, k, notional, pos_pnl, ptier in sorted(dust_cands):
                try:
                    pnl = close(k); state['positions'].pop(k, None)
                    log(f"DUST-CLOSE {k} ({ptier}) pnl=${pos_pnl:+.3f} (for {incoming_tier} {coin} {sig}, freed ${notional:.0f})")
                    if pnl is not None: state['last_pnl_close'] = pnl
                    closed_one = True
                except Exception as e:
                    log(f"dust-close err {k}: {e}")
            # Check if room now
            total_locked = get_total_margin()
            # Phase 2: tier-ranked profit close (only if STILL tight)
            if (total_locked + proposed)/equity > MAX_TOTAL_RISK:
                # Close in order: lowest-tier first, within tier smallest profit first (cheapest to give up)
                # Only close tiers STRICTLY LOWER than incoming
                close_order = ['SEVENTY_79', 'EIGHTY_89', 'NINETY_99']  # PURE never closed for margin
                for ptier in close_order:
                    if TIER_RANK[ptier] >= incoming_rank: break  # can't sacrifice equal or higher tier
                    if (total_locked + proposed)/equity <= MAX_TOTAL_RISK: break
                    cands = sorted(profit_by_tier[ptier])  # ascending: smallest profit first (cheapest sacrifice)
                    for pos_pnl, k, notional in cands:
                        try:
                            pnl = close(k); state['positions'].pop(k, None)
                            log(f"MARGIN-CLOSE {k} ({ptier}) +${pos_pnl:.3f} (for {incoming_tier} {coin} {sig}, freed ${notional:.0f})")
                            if pnl is not None: state['last_pnl_close'] = pnl
                            closed_one = True
                            total_locked = get_total_margin()
                            if (total_locked + proposed)/equity <= MAX_TOTAL_RISK: break
                        except Exception as e:
                            log(f"margin-close err {k}: {e}")
            # Final check
            if (total_locked + proposed)/equity > MAX_TOTAL_RISK:
                log(f"{coin} {sig} SKIP (margin still {total_locked:.0f}+{proposed:.0f} > {MAX_TOTAL_RISK*100:.0f}% after close attempt)")
                return
        else:
            log(f"{coin} {sig} SKIP (margin {total_locked:.0f}+{proposed:.0f} > {MAX_TOTAL_RISK*100:.0f}%)")
            return

    # Per-ticker gate check — uses candles already fetched above (no extra API call)
    # SKIPPED for elite-whitelisted coins: regime-aware system already validates per-coin
    # OOS: V3 OFF + no ticker_gate = +65pp gain over V3 ON
    if not (percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin)):
        try:
            px_for_gate = get_mid(coin) or 0
            if not apply_ticker_gate(coin, sig, px_for_gate, candles):
                log(f"{coin} {sig} GATED")
                return
        except Exception as e:
            log(f"{coin} gate check err: {e}")

    # Signal persistence: DISABLED temporarily (blocking all live signals, OOS +15% but requires market movement)
    # if not signal_persistence.check(coin, sig, bar_ts): return

    # Confidence scoring: 0-100 → sizing multiplier (0.5x / 1.0x / 1.5x / 2.0x)
    # OOS: every score tier profitable, use as SIZING not filter. Every signal trades.
    try:
        btc_state = btc_correlation.get_state()
        btc_d = btc_state.get('btc_dir', 0)
        conf_score, conf_breakdown = confidence.score(candles, [], coin, sig, btc_d)
        size_mult = confidence.size_multiplier(conf_score)
        # Adaptive risk: per-coin × per-hour × per-side rolling WR multipliers
        adapt = adaptive_mult(coin, sig, state)
        risk_mult = risk_mult * size_mult * adapt
        log(f"{coin} CONF={conf_score} conf_mult={size_mult} adapt={adapt:.2f} final_mult={risk_mult:.2f} {conf_breakdown}")
    except Exception as e:
        log(f"{coin} conf err: {e}")
        conf_score = 0

    log_signal(coin, "SIGNAL", sig); log(f"{coin} SIGNAL: {sig} engine={signal_engine} risk={int(risk_pct*100)}% mult={risk_mult:.2f} conf={conf_score}")

    now = time.time()
    if sig == 'SELL':
        state['cooldowns'][coin+'_sell'] = bar_ts
        if live and live['size']>0:
            prev_pos = state.get('positions', {}).get(coin, {})
            pnl_pct = close(coin)
            if pnl_pct is not None:
                record_close(prev_pos, coin, pnl_pct, state)
                if pnl_pct < 0: state['consec_losses'] += 1; risk_ladder.record_trade(False)
                else: state['consec_losses'] = 0; risk_ladder.record_trade(True)
                state['last_pnl_close'] = pnl_pct
        if not live or live['size']>0:
            px = get_mid(coin)
            if px:
                # Tier-priority bump: if margin might reject, close lower-tier positions first
                try_tier_bump(coin, state, live_positions)
                fill_px = place(coin, False, calc_size(equity, px, risk_pct, risk_mult, coin=coin, side=sig))
                if fill_px:
                    sz = calc_size(equity, px, risk_pct, risk_mult, coin=coin, side=sig)
                    place_native_sl(coin, False, fill_px, sz)
                    log_trade('HL', coin, 'SELL', fill_px, 0, 'precog_signal')
                    state['positions'][coin] = {'side':'S', 'opened_at':now, 'entry':fill_px,
                                                'stage':'initial', 'peak':fill_px,
                                                'engine':signal_engine, 'conf':conf_score,
                                                'utc_h': time.gmtime(now).tm_hour}
    else:
        state['cooldowns'][coin+'_buy'] = bar_ts
        if live and live['size']<0:
            prev_pos = state.get('positions', {}).get(coin, {})
            pnl_pct = close(coin)
            if pnl_pct is not None:
                record_close(prev_pos, coin, pnl_pct, state)
                if pnl_pct < 0: state['consec_losses'] += 1; risk_ladder.record_trade(False)
                else: state['consec_losses'] = 0; risk_ladder.record_trade(True)
                state['last_pnl_close'] = pnl_pct
        if not live or live['size']<0:
            px = get_mid(coin)
            if px:
                # Tier-priority bump: if margin might reject, close lower-tier positions first
                try_tier_bump(coin, state, live_positions)
                fill_px = place(coin, True, calc_size(equity, px, risk_pct, risk_mult, coin=coin, side=sig))
                if fill_px:
                    sz = calc_size(equity, px, risk_pct, risk_mult, coin=coin, side=sig)
                    place_native_sl(coin, True, fill_px, sz)
                    log_trade('HL', coin, 'BUY', fill_px, 0, 'precog_signal')
                    state['positions'][coin] = {'side':'L', 'opened_at':now, 'entry':fill_px,
                                                'stage':'initial', 'peak':fill_px,
                                                'engine':signal_engine, 'conf':conf_score,
                                                'utc_h': time.gmtime(now).tm_hour}

# ═══════════════════════════════════════════════════════

# MAIN LOOP
# ═══════════════════════════════════════════════════════
state = {'consec_losses': 0, 'cooldowns': {}, 'coin_hist': {}, 'coin_kill': {}}

def main():
    global state
    log(f"PreCog v8.28 | {WALLET} | risk={INITIAL_RISK_PCT} trail={TRAIL_PCT} V3={V3_HTF}/{V3_EMA}")
    try: bybit_ws.start()
    except Exception as e: log(f"bybit_ws err: {e}")
    try: orderbook_ws.start()
    except Exception as e: log(f"orderbook_ws err: {e}")
    try: liquidation_ws.start()
    except Exception as e: log(f"liq_ws err: {e}")
    try: whale_filter.start()
    except Exception as e: log(f"whale_filter err: {e}")
    try: cvd_ws.start()
    except Exception as e: log(f"cvd_ws err: {e}")
    try: oi_tracker.start()
    except Exception as e: log(f"oi_tracker err: {e}")
    try: threading.Timer(60.0, funding_arb.refresh).start()
    except Exception as e: log(f"funding_arb err: {e}")
    # Funding refresh deferred — first tick runs it after 30s delay
    threading.Timer(30.0, lambda: funding_filter.refresh_all(COINS)).start()
    try: news_filter.start()
    except Exception as e: log(f"news err: {e}")
    try: leverage_map.refresh(info)
    except Exception as e: log(f"lev refresh err: {e}")
    log(f"Universe ({len(COINS)}): {COINS}")
    log(f"Chase-gate ({len(CHASE_GATE_COINS)}): {sorted(CHASE_GATE_COINS)}")
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
            # ACCOUNT DRAWDOWN BREAKER — flatten if equity drops 15% from session high
            session_hwm = state.get('session_hwm', equity)
            if equity > session_hwm:
                state['session_hwm'] = equity
                session_hwm = equity
            # Monitor health check (non-blocking)
            try:
                import monitor
                live_pos = get_all_positions_live() or {}
                monitor.check_health(equity, session_hwm, live_pos)
            except Exception: pass
            dd = (session_hwm - equity) / session_hwm if session_hwm > 0 else 0
            if dd >= 0.15:
                log(f"!!! ACCOUNT DRAWDOWN {dd*100:.1f}% (hwm=${session_hwm:.2f} now=${equity:.2f}) — FLATTENING ALL")
                flatten_all('DRAWDOWN')
                state['cb_pause_until'] = time.time() + CB_PAUSE_SEC
                state['session_hwm'] = equity  # reset hwm after flatten
                save_state(state)
                time.sleep(30)
                continue
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
                    log(f"RECONCILE: phantom {k} cleared (may be liquidation or native SL)")
                    state['positions'].pop(k)
            # Track live-only positions (HL has it, state doesn't)
            for k in live_positions:
                if k not in state['positions']:
                    side = 'L' if live_positions[k]['size']>0 else 'S'
                    entry_px = live_positions[k]['entry']
                    state['positions'][k] = {'side':side, 'opened_at':now - 3600, 'entry':entry_px,
                                             'stage':'initial', 'peak':entry_px}
                    log(f"RECONCILE: adopting existing {k} {side} (opened_at set to -1h as safety)")

            # DUST-SWEEP: close STALE positions (>30min old) with |PnL| <= $0.10 to free margin.
            # Rationale: dust-sweep was killing fresh trades before they could develop edge.
            # Only sweep positions that have had time to work and are going nowhere.
            # Exception: don't sweep PURE tier positions (100% WR — let them work toward TP)
            DUST_THRESHOLD = 0.10  # $0.10
            DUST_MIN_AGE_SEC = 1800  # 30 min — fresh positions get time to reach TP
            now_ts = time.time()
            swept = 0
            for k in list(live_positions.keys()):
                try:
                    lp = live_positions[k]
                    sz = lp.get('size', 0)
                    entry = lp.get('entry', 0)
                    if sz == 0 or not entry: continue
                    pos_tier = percoin_configs.get_tier(k) if percoin_configs.ELITE_MODE else None
                    if pos_tier == 'PURE': continue  # don't sweep 100% WR coins
                    # Age gate — don't kill fresh trades (let them hit TP)
                    pos_state = state.get('positions', {}).get(k, {})
                    opened_at = pos_state.get('opened_at', now_ts)
                    age_sec = now_ts - opened_at
                    if age_sec < DUST_MIN_AGE_SEC: continue  # too fresh, let it work
                    # Use HL's reported PnL directly (no get_mid 429 issues)
                    unrealized_usd = lp.get('pnl', 0)
                    if abs(unrealized_usd) <= DUST_THRESHOLD:
                        notional = abs(sz) * entry
                        try:
                            pnl = close(k)
                            log(f"DUST-SWEEP {k} ({pos_tier or 'NONE'}) pnl=${unrealized_usd:+.3f} age={age_sec/60:.0f}min notional=${notional:.0f} (freeing margin)")
                            state['positions'].pop(k, None)
                            if pnl is not None:
                                state['last_pnl_close'] = pnl
                                if pnl > 0: state['consec_losses'] = 0
                            swept += 1
                        except Exception as e:
                            log(f"dust-sweep err {k}: {e}")
                except Exception as e:
                    log(f"dust-sweep scan err {k}: {e}")
            if swept: log(f"DUST-SWEEP: closed {swept} stale positions (|PnL|<=${DUST_THRESHOLD:.2f}, age>={DUST_MIN_AGE_SEC/60:.0f}min)")

            # Wall-as-TP check — if mark crosses verified resistance/support, signal exit
            for k, lp in live_positions.items():
                try:
                    side_long = lp['size']>0
                    wall_side = 'ask' if side_long else 'bid'
                    wall = orderbook_ws.get_nearest_wall(k, wall_side)
                    if not wall: continue
                    cp = get_mid(k)
                    if not cp: continue
                    # LONG reaches ask wall (resistance) OR SHORT reaches bid wall (support)
                    if side_long and cp >= wall['price'] * 1.002:  # 0.2% past wall, not just touching
                        log(f"WALL-TP {k} LONG reached ask wall ${wall['usd']/1000:.0f}k @ {wall['price']}")
                        close(k)
                    elif not side_long and cp <= wall['price'] * 0.998:  # 0.2% past wall
                        log(f"WALL-TP {k} SHORT reached bid wall ${wall['usd']/1000:.0f}k @ {wall['price']}")
                        close(k)
                except Exception as e:
                    pass

            # Wall-break auto-exit
            wall_ents = state.get('wall_entries', {})
            for wcoin, wdata in list(wall_ents.items()):
                if wcoin not in live_positions:
                    wall_ents.pop(wcoin); continue
                try:
                    cp = get_mid(wcoin)
                    if wall_bounce.wall_broken(wcoin, wdata['side'], wdata['wall_price'], cp):
                        log(f"WALL-BROKEN {wcoin} {wdata['side']} — exiting")
                        close(wcoin)
                        wall_ents.pop(wcoin)
                except Exception as e:
                    log(f"wall-break check err {wcoin}: {e}")

            # Profit-lock @ 3.0%/2.0% (user override — OOS -25% vs no_plock, but best plock config)
            for k, lp in live_positions.items():
                try:
                    side = 'BUY' if lp['size']>0 else 'SELL'
                    entry = lp['entry']
                    cur_px = get_mid(k) or entry
                    cur_sl = state.get('sl_overrides', {}).get(k)
                    new_sl = profit_lock.compute_new_sl(entry, cur_px, side, cur_sl)
                    if new_sl is not None and not state.get('scaled_out', {}).get(k):
                        try:
                            half_sz = round_size(k, abs(lp['size']) / 2)
                            if half_sz > 0:
                                side_long = lp['size']>0
                                exchange.order(k, not side_long, half_sz,
                                               cur_px * (1.005 if not side_long else 0.995),
                                               {'limit':{'tif':'Ioc'}}, reduce_only=True)
                                state.setdefault('scaled_out', {})[k] = True
                                log(f"SCALE-OUT 50% {k} {side} @ {cur_px:.6f}")
                        except Exception as e:
                            log(f"scale-out err {k}: {e}")
                        state.setdefault('sl_overrides', {})[k] = new_sl
                        log(f"PROFIT-LOCK {k} {side}: SL→{new_sl:.6f}")
                except Exception as e:
                    log(f"profit-lock err {k}: {e}")

            # Spoof scan per open position + near-wall coins
            for k in list(live_positions.keys()):
                try: spoof_detection.scan_walls(k, get_mid(k))
                except Exception: pass

            # Hourly funding refresh (both funding_filter and funding_arb)
            fund_age = time.time() - getattr(main, '_funding_ts', 0)
            if fund_age > 3600:
                try: funding_arb.refresh()
                except Exception: pass
                try: funding_filter.refresh_all(COINS); main._funding_ts = time.time()
                except Exception as e: log(f"funding refresh err: {e}")

            # BTC vol throttle
            risk_mult = 1.0
            # BTC vol throttle — cached, fetch only every 15 min
            btc_vol_age = now - getattr(main, '_btc_vol_ts', 0)
            if btc_vol_age > 900:  # 15 min
                try:
                    btc_c = fetch('BTC')
                    if len(btc_c) >= 12:
                        recent = btc_c[-12:]
                        hi = max(c[2] for c in recent); lo = min(c[3] for c in recent)
                        main._btc_vol = (hi-lo)/lo
                    main._btc_vol_ts = now
                except Exception as e:
                    log(f"btc vol err: {e}")
            btc_range = getattr(main, '_btc_vol', 0)
            if btc_range > BTC_VOL_THRESHOLD:
                risk_mult = 0.5
                log(f"BTC vol {btc_range*100:.1f}% — risk halved")

            cur_risk = current_risk_pct(equity)
            log(f"--- tick eq=${equity:.2f} risk={cur_risk*100:.2f}% mult={risk_mult} pos={len(live_positions)} cL={state['consec_losses']} ---")
            # Publish cached state for /dash
            try:
                pos_list = []
                for k, v in live_positions.items():
                    side_long = v['size'] > 0
                    entry = v['entry']
                    # TP target: nearest wall (if any) or trail-projected target
                    tp_target = None
                    try:
                        wall = orderbook_ws.get_nearest_wall(k, 'ask' if side_long else 'bid')
                        if wall: tp_target = wall['price']
                    except Exception: pass
                    if not tp_target:
                        # Fallback: entry * (1 + 3*trail) as rough target
                        tp_target = entry * (1.024 if side_long else 0.976)
                    pos_list.append({
                        'coin': k,
                        'side': 'L' if side_long else 'S',
                        'size': abs(v['size']),
                        'entry': entry,
                        'upnl': v.get('upnl', v.get('pnl', 0)),
                        'lev': v.get('lev', 10),
                        'tp': tp_target,
                        'mark': v.get('mark', 0),
                    })
                main._cached_account = {'equity': equity, 'ts': time.time(), 'positions': pos_list}
            except Exception as e: log(f"cache err: {e}")

            # WEBHOOK QUEUE — process DynaPro signals first (higher priority)
            wh_count = 0
            while not WEBHOOK_QUEUE.empty() and wh_count < 10:
                try:
                    sig = WEBHOOK_QUEUE.get_nowait()
                    coin = sig['coin']; action = sig['action']
                    live = live_positions.get(coin)
                    risk_pct = current_risk_pct(equity)

                    if action in ('exit_buy', 'exit_sell'):
                        # Close existing position
                        if live:
                            pnl_pct = close(coin)
                            if pnl_pct is not None:
                                if pnl_pct < 0: state['consec_losses'] += 1; update_coin_wr(coin, False, state); risk_ladder.record_trade(False)
                                else: state['consec_losses'] = 0; update_coin_wr(coin, True, state); risk_ladder.record_trade(True)
                            state['positions'].pop(coin, None)
                            log(f"WEBHOOK CLOSE {coin} ({action}) pnl={pnl_pct}")
                    elif action in ('buy', 'sell'):
                        # Close opposite position if exists, then open new
                        if live:
                            is_opposite = (action == 'buy' and live['size'] < 0) or (action == 'sell' and live['size'] > 0)
                            if is_opposite:
                                close(coin)
                                state['positions'].pop(coin, None)
                            elif (action == 'buy' and live['size'] > 0) or (action == 'sell' and live['size'] < 0):
                                log(f"WEBHOOK {coin} {action} — already positioned, skip")
                                wh_count += 1; continue
                        if len(live_positions) < MAX_POSITIONS:
                            # BYBIT WS lead price for entry trigger (fallback to HL mid)
                            by_px, by_age = bybit_ws.get(coin)
                            hl_px = get_mid(coin)
                            px = by_px if (by_px and by_age is not None and by_age < 3000) else hl_px
                            if px:
                                is_buy = (action == 'buy')
                                side_str = 'BUY' if is_buy else 'SELL'
                                # GATE — webhook must clear same filter as internal signal
                                candles_for_gate = fetch(coin)
                                if not apply_ticker_gate(coin, side_str, px, candles_for_gate):
                                    log(f"WEBHOOK {coin} {side_str} GATED (trend/ticker filter)")
                                    wh_count += 1; continue
                                sz = calc_size(equity, px, risk_pct, risk_mult, coin=coin, side=sig)
                                fill = place(coin, is_buy, sz)
                                if fill:
                                    place_native_sl(coin, is_buy, fill, sz)
                                    state['positions'][coin] = {
                                        'side': 'L' if is_buy else 'S',
                                        'opened_at': time.time(),
                                        'entry': fill,
                                        'stage': 'initial', 'peak': fill,
                                        'source': 'dynapro'
                                    }
                                    log(f"WEBHOOK OPEN {coin} {side_str} @ {fill} (px_src={'bybit_ws' if px==by_px else 'hl_mid'}, age={by_age}ms)")
                                    log_trade('HL', coin, side_str, fill, 0, 'webhook')
                    wh_count += 1
                except Exception as e:
                    log(f"webhook process err: {e}"); break

            # PRECOG scan — parallel 8 workers (Bybit WS candles = no rate limit)
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(process, c, state, equity, live_positions, risk_mult): c for c in COINS}
                for f in as_completed(futs):
                    try: f.result()
                    except Exception as e: log(f"err {futs[f]}: {e}")

            save_state(state)
            log(f"--- tick complete ---")
        except Exception as e:
            log(f"tick err: {e}\n{traceback.format_exc()}")
        time.sleep(LOOP_SEC)


@app.route('/tuner/update', methods=['POST'])
def tuner_update():
    try:
        import json
        data = flask_request.get_json(force=True, silent=True) or {}
        # Store to web disk
        try:
            os.makedirs('/var/data', exist_ok=True)
            with open('/var/data/tuner_results.json','w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            with open('/tmp/tuner_results.json','w') as f:
                json.dump(data, f, indent=2)
        # Also log summary to buffer
        top = data.get('top',[])
        if top:
            t0 = top[0]
            log(f"TUNER {data.get('phase','?')} {data.get('completed','?')}/{data.get('total','?')} | top: n={t0.get('n')} WR={t0.get('wr',0):.1f}% pnl={t0.get('pnl',0):+.1f}%")
        return jsonify({'ok':True})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@app.route('/tuner/status', methods=['GET'])
def tuner_status():
    try:
        import json
        for p in ['/var/data/tuner_results.json','/tmp/tuner_results.json']:
            if os.path.exists(p):
                d=json.load(open(p))
                return jsonify({'phase':d.get('phase'),'completed':d.get('completed'),
                                'total':d.get('total'),'elapsed_sec':d.get('elapsed_sec'),
                                'top3':d.get('top',[])[:3]})
        return jsonify({'status':'no_results_yet'})
    except Exception as e:
        return jsonify({'error':str(e)})

@app.route('/tuner/top', methods=['GET'])
def tuner_top():
    try:
        import json
        for p in ['/var/data/tuner_results.json','/tmp/tuner_results.json']:
            if os.path.exists(p):
                return jsonify(json.load(open(p)))
        return jsonify({'status':'no_results_yet'})
    except Exception as e:
        return jsonify({'error':str(e)})

@app.route('/tuner/log', methods=['GET'])
def tuner_log():
    try:
        for p in ['/var/data/tuner.log','/tmp/tuner.log']:
            if os.path.exists(p):
                with open(p) as f:
                    lines=f.readlines()[-200:]
                return jsonify({'log': ''.join(lines)})
        return jsonify({'status':'no_log'})
    except Exception as e:
        return jsonify({'error':str(e)})


@app.route('/dash', methods=['GET'])
def dash_json():
    # Use cached account state from main tick to avoid HL 429 on dash hits
    cached = getattr(main, '_cached_account', {})
    eq = cached.get('equity', 0)
    positions = cached.get('positions', [])
    if not cached or time.time() - cached.get('ts', 0) > 30:
        try:
            cs = info.user_state(WALLET)
            eq = float(cs.get('marginSummary',{}).get('accountValue',0))
            positions = []
            for p in cs.get('assetPositions',[]):
                pp=p['position']; sz=float(pp['szi'])
                positions.append({'coin':pp['coin'],'side':'L' if sz>0 else 'S','size':abs(sz),
                                  'entry':float(pp['entryPx']),'upnl':float(pp['unrealizedPnl']),
                                  'lev':int(pp['leverage']['value'])})
        except Exception as e:
            pass
    try: news = news_filter.get_state()
    except Exception: news = {}
    try: ladder = risk_ladder.get_state()
    except Exception: ladder = {}
    try: ob_stat = orderbook_ws.status()
    except Exception: ob_stat = {}
    try: lev_cache = leverage_map.get_cache()
    except Exception: lev_cache = {}
    try: liq_stat = liquidation_ws.status()
    except Exception: liq_stat = {}
    try: wall_entries = state.get('wall_entries', {})
    except Exception: wall_entries = {}

    coin_hist = state.get('coin_hist', {})
    coin_kill = state.get('coin_kill', {})
    coin_wr = {}
    for coin, h in coin_hist.items():
        if len(h) >= 5: coin_wr[coin] = round(sum(h)/len(h)*100, 1)
    killed = {c:v.get('until',0) for c,v in coin_kill.items() if time.time() < v.get('until',0)}
    return jsonify({
        'equity': eq, 'version': 'v8.28',
        'positions': positions, 'n_positions': len(positions),
        'universe_size': len(COINS),
        'news': news, 'risk_ladder': ladder,
        'orderbook': ob_stat, 'leverage_cache_size': len(lev_cache),
        'liquidation': liq_stat, 'wall_entries': len(wall_entries),
        'btc_corr': btc_correlation.get_state(),
        'funding_cached': len(funding_filter._CACHE) if hasattr(funding_filter, '_CACHE') else 0,
        'spoof': spoof_detection.status(),
        'session': {'name': session_scaler.session_name(), 'mult': session_scaler.get_mult()},
        'whale': whale_filter.status(),
        'cvd': cvd_ws.status(),
        'oi': oi_tracker.status(),
        'funding_arb': funding_arb.status(),
        'coin_wr': coin_wr, 'killed_coins': killed,
        'consec_losses': state.get('consec_losses', 0),
    })

@app.route('/dash/html', methods=['GET'])
def dash_html():
    return """<!DOCTYPE html><html><head><title>PreCog Live</title>
<style>body{font-family:monospace;background:#0b0b0b;color:#ccc;padding:20px;max-width:1400px;margin:auto}
h2{color:#0f0;border-bottom:1px solid #333;padding-bottom:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.card{background:#111;padding:12px;border:1px solid #222;border-radius:4px}
.kv{display:flex;justify-content:space-between;padding:2px 0}
.k{color:#888} .v{color:#fff}
.pos{background:#0a1a0a}.neg{background:#1a0a0a}
table{width:100%;border-collapse:collapse}
td,th{padding:4px 8px;text-align:left;border-bottom:1px solid #222}
.red{color:#f55}.green{color:#5f5}.yellow{color:#ff5}
</style></head><body>
<h2>PreCog Live Dashboard</h2>
<div id="root">loading...</div>
<script>
async function refresh(){
  const r = await fetch('/dash'); const d = await r.json();
  const fmt = (n,d=2) => Number(n).toFixed(d);
  const news = d.news || {};
  const rl = d.risk_ladder || {};
  const ob = d.orderbook || {};
  const pos_rows = (d.positions||[]).map(p=>`<tr><td>${p.coin}</td><td class="${p.side=='L'?'green':'red'}">${p.side}</td><td>${p.size}</td><td>${fmt(p.entry,4)}</td><td class="${p.upnl>=0?'green':'red'}">${fmt(p.upnl,2)}</td><td>${p.lev}x</td></tr>`).join('');
  const wr_rows = Object.entries(d.coin_wr||{}).sort((a,b)=>b[1]-a[1]).slice(0,30).map(([c,w])=>`<tr><td>${c}</td><td class="${w>=60?'green':w>=45?'yellow':'red'}">${w}%</td></tr>`).join('');
  const killed = Object.keys(d.killed_coins||{});
  const news_list = (news.last_events||[]).slice(0,8).map(e=>`<div class="kv"><span class="k">[${e.src}]</span><span class="v">${e.title} (${e.mag}/${e.dir>0?'↑':e.dir<0?'↓':'?'})</span></div>`).join('');
  document.getElementById('root').innerHTML = `
  <div class="grid">
    <div class="card"><h3>Account</h3>
      <div class="kv"><span class="k">Equity</span><span class="v">$${fmt(d.equity)}</span></div>
      <div class="kv"><span class="k">Positions</span><span class="v">${d.n_positions}/${30}</span></div>
      <div class="kv"><span class="k">Universe</span><span class="v">${d.universe_size} coins</span></div>
      <div class="kv"><span class="k">Consec losses</span><span class="v">${d.consec_losses}</span></div>
    </div>
    <div class="card"><h3>Risk Ladder</h3>
      <div class="kv"><span class="k">Tier</span><span class="v">${rl.tier||0}</span></div>
      <div class="kv"><span class="k">Risk</span><span class="v">${fmt((rl.risk||0)*100,2)}%</span></div>
      <div class="kv"><span class="k">Trades logged</span><span class="v">${rl.trades_logged||0}</span></div>
      <div class="kv"><span class="k">WR (100)</span><span class="v">${fmt((rl.rolling_wr_100||0)*100,1)}%</span></div>
      <div class="kv"><span class="k">WR (50)</span><span class="v">${fmt((rl.rolling_wr_50||0)*100,1)}%</span></div>
    </div>
    <div class="card"><h3>News / Regime</h3>
      <div class="kv"><span class="k">Blackout</span><span class="v ${news.blackout?'red':'green'}">${news.blackout?'YES':'clear'}</span></div>
      <div class="kv"><span class="k">Risk mult</span><span class="v">${news.risk_mult||1}x</span></div>
      <div class="kv"><span class="k">Direction bias</span><span class="v">${news.direction_bias||0}</span></div>
    </div>
    <div class="card"><h3>Orderbook WS</h3>
      <div class="kv"><span class="k">Feeds</span><span class="v">${ob.depth_feeds||0}</span></div>
      <div class="kv"><span class="k">Verified walls</span><span class="v">${ob.tracked_walls||0}</span></div>
      <div class="kv"><span class="k">Coins w/ walls</span><span class="v">${ob.verified_coins||0}</span></div>
    </div>
  </div>
  <h2>Open Positions</h2>
  <table><tr><th>Coin</th><th>Side</th><th>Size</th><th>Entry</th><th>uPnL</th><th>Lev</th></tr>${pos_rows||'<tr><td colspan=6>none</td></tr>'}</table>
  <h2>Per-Coin WR (top 30)</h2>
  <table><tr><th>Coin</th><th>WR</th></tr>${wr_rows}</table>
  ${killed.length?`<h2>Killed coins (12h)</h2><div>${killed.join(', ')}</div>`:''}
  <h2>Recent news (${news.last_events?.length||0})</h2>${news_list}`;
}
refresh(); setInterval(refresh, 10000);
</script></body></html>"""

if __name__ == '__main__':
    # Run precog signal loop in background thread
    t = threading.Thread(target=main, daemon=True)
    t.start()
    # Run latency arbitrage module in background thread
    # LA KILLED — was burning 60 API calls/sec with 0 trades, causing 429s
    # Run Flask webhook server in main thread (Render expects port 10000)
    port = int(os.environ.get('PORT', 10000))
    _mt4_load()  # restore MT4 queue from disk across deploys
    log(f"Webhook server starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
