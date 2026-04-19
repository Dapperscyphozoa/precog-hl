#!/usr/bin/env python3
"""PreCog v8.12 — 50-coin universe + 48 MT4 tickers

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
from flask import Flask, request as flask_request, jsonify
from gates import run_gates
import bybit_ws

# ═══════════════════════════════════════════════════════
# TRADE LOG — persistent CSV for real WR tracking
# ═══════════════════════════════════════════════════════
TRADE_LOG = '/var/data/trades.csv'
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
except:
    MT4_TICKER_GATES = {}
MT4_QUEUE = []  # EA polls /mt4/signals every 10s
MT4_BIAS = {'direction': '', 'ts': 0}

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

@app.route('/health', methods=['GET'])
def health():
    eq = 0
    try: eq = get_balance()
    except: pass
    return jsonify({'status':'ok','version':'v8.12.0','equity':eq,
                    'queue_size':WEBHOOK_QUEUE.qsize(),
                    'mt4_queue':len(MT4_QUEUE),
                    'coins':len(COINS),
                    'risk':INITIAL_RISK_PCT,
                    'trail':TRAIL_PCT,
                    'gates_loaded':len(TICKER_GATES),
                    'recent_logs':LOG_BUFFER[-20:]})

LOG_BUFFER = []  # ring buffer for last 100 log lines


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
    """Force close ALL open positions and clear state."""
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
    except: pass
    
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
                    except: pass
    
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
        # EURGBP inversion — mean-reverting pair, flip signal
        if gate.get('inverted'):
            direction = 'SELL' if direction == 'BUY' else 'BUY'
            log(f"MT4 INVERTED {clean}: {action.upper()} → {direction}")
        MT4_QUEUE.append({'symbol': mt4_sym, 'direction': direction, 'price': price, 'ts': time.time()})
        if len(MT4_QUEUE) > 20: MT4_QUEUE.pop(0)
        log(f"MT4 QUEUED: {direction} {mt4_sym} @ {price}")
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
    """EA polls this every 10s. Returns one signal, removes from queue."""
    global MT4_QUEUE
    if MT4_QUEUE:
        sig = MT4_QUEUE.pop(0)
        log(f"MT4 SERVED: {sig['direction']} {sig['symbol']}")
        return jsonify(sig)
    return jsonify({'symbol':'','direction':'','price':0})

@app.route('/mt4/status', methods=['GET'])
def mt4_status():
    bias_age = time.time() - MT4_BIAS.get('ts', 0)
    bias_active = bias_age < 300  # 5min validity
    return jsonify({
        'queue_size':len(MT4_QUEUE),'queue':MT4_QUEUE[:5],
        'bias': MT4_BIAS.get('direction','') if bias_active else '',
        'bias_age_sec': round(bias_age)
    })

COINS = [
    'SOL','LINK','UNI','ENS','AAVE','POL','SAND','APT','MON','COMP',
    'AERO','LIT','SPX','kPEPE','kBONK','kSHIB','MORPHO','JUP','XRP',
    'SUSHI','ADA','WLD','PUMP','PENGU','FARTCOIN',
    'AIXBT','AVAX','PENDLE','TAO','WIF',
    'BTC','BNB','DOT','ATOM','SUI','LDO','INJ','UMA','ALGO',
    'BLUR','VVV','APE','OP','TON','TIA','LTC','MOODENG',
    'AR','GALA','VIRTUAL',
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

# ═══════════════════════════════════════════════════════
# V3 TREND GATE — 4H EMA9 direction alignment
# OOS 7d: 100% WR at 4h/EMA9/no-buffer (6 trades, +3.14% PnL)
# ═══════════════════════════════════════════════════════
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

V3_ENABLED = True
V3_HTF = '4h'
V3_EMA = 9

def trend_gate(coin, side):
    """V3: block BUY if 4H close < 4H EMA9, SELL if above. Returns True if passes."""
    if not V3_ENABLED: return True
    htf = fetch_htf(coin, V3_HTF, V3_EMA * 3 + 5)
    if len(htf) < V3_EMA + 2: return True
    closes = [b[4] for b in htf]
    k = 2/(V3_EMA+1)
    ema = sum(closes[:V3_EMA])/V3_EMA
    for c in closes[V3_EMA:]:
        ema = c*k + ema*(1-k)
    last = closes[-1]
    if side == 'BUY' and last < ema: return False
    if side == 'SELL' and last > ema: return False
    return True

def apply_ticker_gate(coin, side, price, candles):
    """Apply per-ticker optimized gates + V3 trend gate. Returns True if signal passes."""
    if not trend_gate(coin, side):
        return False
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

GRID = {'sens':1, 'rsi':10, 'wick':1, 'ext':1, 'block':1, 'vol':1, 'cd':3}

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

INITIAL_RISK_PCT = 0.00025   # 0.025% — ultra-defensive
SCALED_RISK_PCT  = 0.005
SCALE_DOWN_AT    = 50000
LEV = 10
LOOP_SEC = 5  # continuous processing — no 5min sleep between cycles
USE_ISOLATED_MARGIN = True

MAX_POSITIONS = 20       # was 8 — LA dead, no margin reserve needed
MAX_SAME_SIDE = 12       # was 6 — with MAX_POS 20, side cap was blocking 14 trades
MAX_TOTAL_RISK = 0.80    # reserve 20% margin
STOP_LOSS_PCT = 0.01      # 1.0% price × 10x = 10% of trade. Keeps 92% of winners (MAE backtested)
BTC_VOL_THRESHOLD = 0.03

MAX_HOLD_SEC = 4 * 3600
CB_CONSEC_LOSSES = 5
CB_PAUSE_SEC = 600  # 10min (was 60min — too long, cloud exit was triggering it)
FUNDING_CUT_RATIO = 0.50  # was 0.20 — don't cut small winners prematurely

TRAIL_PCT = 0.007          # 0.7% trail — BT winner: +424% vs +138% at 0.4%
MAKER_FALLBACK_SEC = 10
MAKER_OFFSET = 0.0003  # 0.03% better than mid — buy lower, sell higher

info = Info(constants.MAINNET_API_URL, skip_ws=True)
account = Account.from_key(PRIV_KEY)
exchange = Exchange(account, constants.MAINNET_API_URL, account_address=WALLET)

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
        return loaded
    except: return default

def save_state(s):
    """Atomic write with backup for deploy resilience."""
    os.makedirs('/var/data', exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    with open(tmp,'w') as f: json.dump(s,f)
    os.replace(tmp, STATE_PATH)
    # Backup copy survives if primary is lost on deploy
    try:
        import shutil; shutil.copy2(STATE_PATH, STATE_PATH + '.bak')
    except: pass

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
    now = time.time()
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

# ═══════════════════════════════════════════════════════

# SIGNAL — cooldown by timestamp, scan last K bars
# ═══════════════════════════════════════════════════════
SCAN_BARS = 3  # check last SCAN_BARS closed bars each tick
CD_MS = 3 * 5 * 60 * 1000  # cd=3 bars of 5m = 15 min

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

def signal(candles, last_sell_ts, last_buy_ts, coin=None):
    """Scan last SCAN_BARS closed bars. Cooldown tracked by bar timestamp.
    Applies chase_gate for coins in CHASE_GATE_COINS."""
    if len(candles)<100: return None, None
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

# ═══════════════════════════════════════════════════════
# HL INTERFACE
# ═══════════════════════════════════════════════════════
def get_balance():
    try: return float(_cached_user_state()['marginSummary']['accountValue'])
    except: return 0

def get_total_margin():
    try: return float(_cached_user_state()['marginSummary'].get('totalMarginUsed', 0))
    except: return 0

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
        except: pass
    return _cache['mids'] or {}

def _cached_user_state():
    now = time.time()
    if _cache['state'] is None or now - _cache['state_ts'] > CACHE_TTL:
        try:
            _cache['state'] = info.user_state(WALLET)
            _cache['state_ts'] = now
        except: pass
    return _cache['state'] or {}

def get_mid(coin):
    try: return float(_cached_mids()[coin])
    except: return None

_POSITIONS_CACHE = {'data': {}, 'ts': 0}

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
                    'mark':float(pos.get('positionValue',0)) / abs(sz) if sz else 0
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
    """HL-compliant price rounding + maker/taker handling."""
    px = get_mid(coin)
    if not px: return None
    set_isolated_leverage(coin)
    size = round_size(coin, size)
    if size <= 0:
        log(f"{coin} size rounded to 0 — skip"); return None

    # SMART LIMIT — offset price in our favor for better entry
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

def close(coin):
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
    """Place HL native stop-loss order — executes server-side, no tick delay."""
    try:
        entry = float(entry); size = float(size)
        trigger_px = entry * (1 - STOP_LOSS_PCT) if is_long else entry * (1 + STOP_LOSS_PCT)
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
            log(f"{coin} NATIVE SL placed @ {trigger_px} (limit {limit_px})")
    except Exception as e:
        log(f"{coin} native SL err: {e}")

def process(coin, state, equity, live_positions, risk_mult=1.0):
    candles=fetch(coin)
    last_s=state['cooldowns'].get(coin+'_sell', 0)
    last_b=state['cooldowns'].get(coin+'_buy',  0)
    sig, bar_ts = signal(candles, last_s, last_b, coin=coin)
    cur=state['positions'].get(coin)
    live=live_positions.get(coin)

    # Position management: SL, trail, funding checks
    if cur and live:

        mark = get_mid(coin)
        if mark and cur.get('entry'):
            entry = cur['entry']
            side = cur['side']
            fav = (mark - entry) / entry if side == 'L' else (entry - mark) / entry

            # 2% HARD STOP LOSS — wide enough to survive noise, cuts real losers
            if fav <= -STOP_LOSS_PCT:
                pnl_pct = close(coin)
                if pnl_pct is not None:
                    state['consec_losses'] += 1
                    state['last_pnl_close'] = pnl_pct
                log(f"{coin} STOP LOSS {fav*100:.2f}% (limit -{STOP_LOSS_PCT*100:.1f}%)")
                state['positions'].pop(coin, None)
                return

            # TRAILING STOP — lock gains, but only exit if still in meaningful profit
            hwm = cur.get('hwm', fav)
            if fav > hwm:
                hwm = fav
                cur['hwm'] = hwm
            
            # Trail: peaked above trail threshold AND retraced trail amount AND still +0.2% profit
            if hwm > TRAIL_PCT and (hwm - fav) >= TRAIL_PCT and fav >= TRAIL_PCT * 0.5:
                pnl_pct = close(coin)
                if pnl_pct is not None:
                    state['consec_losses'] = 0
                    state['last_pnl_close'] = pnl_pct
                log(f"{coin} TRAIL EXIT +{fav*100:.2f}% (peak +{hwm*100:.2f}%, trail {TRAIL_PCT*100:.1f}%)")
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
        log(f"{coin} {sig} SKIP (margin {total_locked:.0f}+{proposed:.0f} > {MAX_TOTAL_RISK*100:.0f}%)")
        return

    # Per-ticker gate check — uses candles already fetched above (no extra API call)
    try:
        px_for_gate = get_mid(coin) or 0
        if not apply_ticker_gate(coin, sig, px_for_gate, candles):
            log(f"{coin} {sig} GATED by per-ticker filter")
            return
    except Exception as e:
        log(f"{coin} gate check err: {e}")

    log(f"{coin} SIGNAL: {sig} (risk={int(risk_pct*100)}% mult={risk_mult})")

    now = time.time()
    if sig == 'SELL':
        state['cooldowns'][coin+'_sell'] = bar_ts
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
                    sz = calc_size(equity, px, risk_pct, risk_mult)
                    place_native_sl(coin, False, fill_px, sz)
                    log_trade('HL', coin, 'SELL', fill_px, 0, 'precog_signal')
                    state['positions'][coin] = {'side':'S', 'opened_at':now, 'entry':fill_px,
                                                'stage':'initial', 'peak':fill_px}
    else:
        state['cooldowns'][coin+'_buy'] = bar_ts
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
                    sz = calc_size(equity, px, risk_pct, risk_mult)
                    place_native_sl(coin, True, fill_px, sz)
                    log_trade('HL', coin, 'BUY', fill_px, 0, 'precog_signal')
                    state['positions'][coin] = {'side':'L', 'opened_at':now, 'entry':fill_px,
                                                'stage':'initial', 'peak':fill_px}

# ═══════════════════════════════════════════════════════

# MAIN LOOP
# ═══════════════════════════════════════════════════════
def main():
    log(f"PreCog v8.13 | wallet={WALLET} | precog+webhook | trail={TRAIL_PCT} | risk={INITIAL_RISK_PCT}")
    log(f"V3 trend gate: {V3_HTF}/EMA{V3_EMA} | Bybit WS: starting")
    try:
        bybit_ws.start()
    except Exception as e:
        log(f"bybit_ws start err: {e}")
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
            log(f"--- tick eq=${equity:.2f} risk={int(cur_risk*100)}% mult={risk_mult} positions={len(live_positions)} consec_L={state['consec_losses']} ---")

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
                                if pnl_pct < 0: state['consec_losses'] += 1
                                else: state['consec_losses'] = 0
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
                                sz = calc_size(equity, px, risk_pct, risk_mult)
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

            # PRECOG + WEBHOOK SIGNALS — scan all coins
            for c in COINS:
                try:
                    process(c, state, equity, live_positions, risk_mult)
                except Exception as e:
                    log(f"err {c}: {e}")
                time.sleep(2.3)  # 2.3s between coins — prevents 429 at tail end of 50-coin cycle

            save_state(state)
            log(f"--- tick complete ---")
        except Exception as e:
            log(f"tick err: {e}\n{traceback.format_exc()}")
        time.sleep(LOOP_SEC)

if __name__ == '__main__':
    # Run precog signal loop in background thread
    t = threading.Thread(target=main, daemon=True)
    t.start()
    # Run latency arbitrage module in background thread
    # LA KILLED — was burning 60 API calls/sec with 0 trades, causing 429s
    # Run Flask webhook server in main thread (Render expects port 10000)
    port = int(os.environ.get('PORT', 10000))
    log(f"Webhook server starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
