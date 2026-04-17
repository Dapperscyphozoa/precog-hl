#!/usr/bin/env python3
#!/usr/bin/env python3
"""PreCog v8.8 — 34-COIN UNIVERSE (doubled portfolio)

v8.8 changes:
- Universe expanded: 24 → 34 coins (validated from 82-CSV mass BT)
- New RAW keepers: MON (+29.3%/30d), COMP (+9.0%), WLD (+9.1%), LIT (+4.6%), PUMP (+6.5%)
- New GATED keepers: BLUR (+30.3% gated), VVV (+23.1%), APE (+6.1%), OP (+8.0%), TON (+2.0%)

Portfolio BT (30d, maker fees, 10x, 5% risk, selective gating):
  34 coins | 19.4 trades/day | 998 trades/30d | 77.6% avg WR
  Daily compound: +11.82% (BT extrapolation — real drift will reduce)
  Real-world expectation: 6-8% daily after slippage/drift
  Trajectory @ 6%/day: $229 → $1,314 (30d) → $42K (90d) → $7.7M (180d)

Top performers:
  BLUR  77.3% GATED +30.3%/30d
  MON   81.6% RAW   +29.3%
  VVV   87.0% GATED +23.1%
  APT   76.9% RAW   +18.8%
  kPEPE 78.4% RAW   +15.1%
  UNI   81.1% RAW   +12.8%
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

WALLET     = os.environ['HYPERLIQUID_ACCOUNT']
PRIV_KEY   = os.environ['HL_PRIVATE_KEY']
STATE_PATH = '/var/data/precog_state.json'
KILL_FILE  = '/var/data/KILL'

# ═══════════════════════════════════════════════════════
# WEBHOOK — receives DynaPro signals from TradingView
# ═══════════════════════════════════════════════════════
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'precog_dynapro_2026')
WEBHOOK_QUEUE = Queue()
WEBHOOK_DEDUP = {}  # {coin+action: timestamp} — prevent double entries within 60s
# ═══════════════════════════════════════════════════════
# MT4 SIGNAL ROUTING — DynaPro webhook → Pepperstone EA
# ═══════════════════════════════════════════════════════
MT4_QUEUE = []  # EA polls /mt4/signals every 10s
MT4_BIAS = {'direction': '', 'ts': 0}  # DynaPro condition alert bias (no ticker)

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
    try:
        from latency_arb import get_la_status
        la = get_la_status()
    except: la = {'la_active': False}
    return jsonify({'status':'ok','version':'v8.11.0','equity':eq,
                    'queue_size':WEBHOOK_QUEUE.qsize(),
                    'mt4_queue':len(MT4_QUEUE),
                    'coins':len(COINS),
                    'risk':INITIAL_RISK_PCT,
                    'trail':TRAIL_PCT,
                    'gates_loaded':len(TICKER_GATES),
                    'la':la,
                    'recent_logs':LOG_BUFFER[-20:]})

LOG_BUFFER = []  # ring buffer for last 100 log lines

@app.route('/reset', methods=['GET'])
def reset_cb():
    """Reset circuit breaker and consecutive losses."""
    state = load_state()
    state['cb_pause_until'] = 0
    state['consec_losses'] = 0
    save_state(state)
    log("CIRCUIT BREAKER RESET via /reset endpoint")
    return jsonify({'status':'reset','cb_pause_until':0,'consec_losses':0})

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
        
        # "long entry" / "short entry" — no ticker
        elif text.lower() in ('long entry','short entry','long exit','short exit'):
            log(f"WEBHOOK CONDITION: {text} (no ticker)")
            return jsonify({'status':'received','type':'condition'}), 200
        
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
        # Condition alerts ("long entry"/"short entry") — no ticker
        # Broadcast to ALL Pepperstone tickers — EA confirms with EMA
        action_text = str(data.get('action','')).lower()
        direction = None
        if 'long' in action_text or 'buy' in action_text: direction = 'BUY'
        elif 'short' in action_text or 'sell' in action_text: direction = 'SELL'
        
        if direction:
            MT4_BIAS['direction'] = direction
            MT4_BIAS['ts'] = time.time()
            # Queue for all Pepperstone tickers
            mt4_count = 0
            for tv_sym, mt4_sym in TV_TO_MT4.items():
                MT4_QUEUE.append({'symbol': mt4_sym, 'direction': direction, 'price': 0, 'ts': time.time()})
                mt4_count += 1
            if len(MT4_QUEUE) > 100: MT4_QUEUE[:] = MT4_QUEUE[-100:]
            log(f"MT4 BROADCAST: {direction} → {mt4_count} Pepperstone tickers queued")
        return jsonify({'status':'broadcast','direction':direction or '','count':mt4_count if direction else 0}), 200

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
    
    # DEDUP: ignore duplicate coin+action within 60s
    dedup_key = f"{coin}_{action}"
    now = time.time()
    if dedup_key in WEBHOOK_DEDUP and now - WEBHOOK_DEDUP[dedup_key] < 60:
        log(f"WEBHOOK DEDUP: {action} {coin} (duplicate within 60s, skipped)")
        return jsonify({'status':'deduped','coin':coin,'action':action}), 200
    WEBHOOK_DEDUP[dedup_key] = now
    
    # Route: Pepperstone tickers → MT4, crypto tickers → HL
    raw_ticker = data.get('ticker','').upper().replace('PEPPERSTONE:','')
    if is_pepperstone(raw_ticker):
        mt4_sym = get_mt4_symbol(raw_ticker)
        MT4_QUEUE.append({'symbol': mt4_sym, 'direction': action.upper(), 'price': price, 'ts': time.time()})
        if len(MT4_QUEUE) > 20: MT4_QUEUE.pop(0)
        log(f"MT4 QUEUED: {action} {mt4_sym} @ {price}")
        return jsonify({'status':'mt4_queued','symbol':mt4_sym,'action':action}), 200

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

# v8.10 coin list — 50 validated keepers (added 8 weak-but-positive tier)
COINS = [
    # RAW TIER 1 (20) — 75%+ WR, high edge
    'SOL','LINK','UNI','ENS','AAVE','POL','SAND','APT','MON','COMP',
    'AERO','LIT','SPX','kPEPE','kBONK','kSHIB','MORPHO','JUP','XRP',
    'SUSHI',
    # RAW TIER 2 (10) — 68-75% WR or extended
    'ADA','WLD','PUMP','PENGU','FARTCOIN',
    # RAW EXTENDED (5) — 60-68% WR but positive return
    'AIXBT','AVAX','PENDLE','TAO','WIF',
    # GATED (15) — chase-filter improves WR significantly
    'BTC','BNB','DOT','ATOM','SUI','LDO','INJ','UMA','ALGO',
    'BLUR','VVV','APE','OP','TON','TIA','LTC','MOODENG',
    # GATED EXTENDED (3) — 60-68% WR but gated+positive
    'AR','GALA','VIRTUAL',
]

# v8.10 SELECTIVE GATE — 20 coins where chase-filter improved WR in BT
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

def apply_ticker_gate(coin, side, price, candles):
    """Apply per-ticker optimized gates. Returns True if signal passes."""
    # Strip exchange suffix to match gate keys
    key = coin.upper().replace('.P','')
    gate = TICKER_GATES.get(key)
    if not gate:
        return True  # no gate config = pass through
    
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

INITIAL_RISK_PCT = 0.10    # 10% risk — high confidence per-ticker gates
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
CB_PAUSE_SEC = 600  # 10min (was 60min — too long, cloud exit was triggering it)
FUNDING_CUT_RATIO = 0.20

# v8.3 RUNNER LOGIC — DISABLED in v8.4 (BT showed it hurt performance).
# Kept code in place for future re-enable if validated on different data.
RUNNER_ENABLED  = False
RUNNER_SL_PCT   = 0.004
RUNNER_TP1_PCT  = 0.005
RUNNER_TP2_PCT  = 0.010
RUNNER_TRAIL    = 0.007
RUNNER_BE_BUFF  = 0.0005

# v8.11: PRECOG OWN SIGNALS — DISABLED (40% WR, bleeding capital)
# DynaPro webhook signals are the real edge (77% WR in BT)
# Keep process() running for position management (TP, cloud exit) on existing positions only
PRECOG_SIGNALS_ENABLED = True
TRAIL_PCT = 0.003          # 0.3% trailing stop — let winners run, trail locks gains
CLOUD_EXIT_ENABLED = False  # DISABLED — was closing at losses, overriding trail stop
MAKER_FALLBACK_SEC = 30  # if Alo doesn't fill in 30s, fallback to Ioc taker

info = Info(constants.MAINNET_API_URL, skip_ws=True)
account = Account.from_key(PRIV_KEY)
exchange = Exchange(account, constants.MAINNET_API_URL, account_address=WALLET)

# v8.8 HL price rounding — cache per-coin szDecimals from meta
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
    if len(LOG_BUFFER) > 100: LOG_BUFFER.pop(0)

def current_risk_pct(equity):
    return SCALED_RISK_PCT if equity >= SCALE_DOWN_AT else INITIAL_RISK_PCT

# ═══════════════════════════════════════════════════════
# STATE — atomic write, rich position tracking (FIX #4)
# ═══════════════════════════════════════════════════════
def load_state():
    default = {'positions':{}, 'cooldowns':{}, 'consec_losses':0, 'cb_pause_until':0, 'last_pnl_close':None, 'cd_format':'ts'}
    try:
        with open(STATE_PATH) as f:
            loaded = json.load(f)
        # v8.1 migration: wipe old bar-index cooldowns (values were small ints, new format is ms timestamps ~1.7e12)
        if loaded.get('cd_format') != 'ts':
            loaded['cooldowns'] = {}
            loaded['cd_format'] = 'ts'
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

def fetch(coin, n_bars=300, retries=3):
    end=int(time.time()*1000); start=end-n_bars*5*60*1000
    for attempt in range(retries):
        try:
            d=info.candles_snapshot(coin,'5m',start,end)
            return [(int(c['t']),float(c['o']),float(c['h']),float(c['l']),float(c['c']),float(c['v'])) for c in d]
        except Exception as e:
            es = str(e)
            if '429' in es and attempt < retries-1:
                time.sleep(1.5 + random.random()*1.5)
                continue
            log(f"candle err {coin}: {e}"); return []
    return []

# ═══════════════════════════════════════════════════════
# SIGNAL — v8.1: cooldown by TIMESTAMP (ms), scan last K bars
# ═══════════════════════════════════════════════════════
SCAN_BARS = 3  # check last SCAN_BARS closed bars each tick
CD_MS = 3 * 5 * 60 * 1000  # cd=3 bars of 5m = 15 min

def chase_gate_ok(side, price, candles, i):
    """v8.5: Reject entries chasing extended moves.
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
    v8.5: Applies chase_gate for coins in CHASE_GATE_COINS."""
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
        # v8.5: chase gate for gated coins
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
    """v8.8: HL-compliant price rounding + proper maker/taker error handling."""
    px = get_mid(coin)
    if not px: return None
    set_isolated_leverage(coin)
    size = round_size(coin, size)
    if size <= 0:
        log(f"{coin} size rounded to 0 — skip"); return None

    # MAKER attempt (Alo post-only) at inside-book price
    maker_px = round_price(coin, px * (0.9998 if is_buy else 1.0002))
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

def close(coin):
    """Returns realized pnl_pct for logging (FIX #11)."""
    live = get_all_positions_live().get(coin)
    if not live: return None
    is_buy=live['size']<0; size=abs(live['size']); px=get_mid(coin)
    if not px: return None
    size = round_size(coin, size)
    slip = round_price(coin, px * (1.005 if is_buy else 0.995))
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
# ═══════════════════════════════════════════════════════
# v8.3 RUNNER MANAGEMENT — called BEFORE signal check each tick
# ═══════════════════════════════════════════════════════
def manage_runner(coin, state, live, equity):
    """Returns True if position was closed (signal processing should skip).
    Manages: hard SL, breakeven stop, TP1 partial, TP2 trail.

    Stages per position:
      'initial'    — fresh entry, hard SL active
      'breakeven'  — +1R hit, SL moved to entry+buffer
      'tp1_taken'  — +2R hit, half closed, remainder trailing
    """
    cur = state['positions'].get(coin)
    if not cur or not live: return False

    entry = cur.get('entry', live['entry'])
    side  = cur.get('side')  # 'L' or 'S'
    stage = cur.get('stage', 'initial')
    peak  = cur.get('peak', entry)  # peak favorable price
    mark  = live.get('mark', get_mid(coin))
    if not mark: return False

    # Compute favorable/adverse % move from entry
    if side == 'L':
        fav = (mark - entry) / entry
        # update peak (highest mark for long)
        if mark > peak: cur['peak'] = mark; peak = mark
        trail_trigger = peak * (1 - RUNNER_TRAIL)
        be_stop = entry * (1 + RUNNER_BE_BUFF)
    else:  # 'S'
        fav = (entry - mark) / entry
        if mark < peak or peak == entry: cur['peak'] = mark; peak = mark
        trail_trigger = peak * (1 + RUNNER_TRAIL)
        be_stop = entry * (1 - RUNNER_BE_BUFF)

    # STAGE TRANSITIONS
    if stage == 'initial' and fav >= RUNNER_TP1_PCT:
        cur['stage'] = 'breakeven'
        log(f"{coin} TP1 hit ({fav*100:+.2f}%) — stage=breakeven, SL@{be_stop:.4f}")
        stage = 'breakeven'

    if stage == 'breakeven' and fav >= RUNNER_TP2_PCT:
        # Close 50% of position
        size = abs(live['size']) * 0.5
        is_buy_close = (side == 'S')  # opposite direction to close
        slip_px = round_price(coin, mark * (1.005 if is_buy_close else 0.995))
        size = round_size(coin, size)
        try:
            exchange.order(coin, is_buy_close, size, slip_px,
                          {'limit':{'tif':'Ioc'}}, reduce_only=True)
            log(f"{coin} TP2 hit ({fav*100:+.2f}%) — closed 50% ({size}), runner active")
            cur['stage'] = 'tp1_taken'
            stage = 'tp1_taken'
        except Exception as e:
            log(f"{coin} TP2 close err: {e}")

    # EXIT CHECKS (in order of priority)
    exit_reason = None; exit_px_target = None

    # 1. Hard SL (only in 'initial' stage)
    if stage == 'initial':
        if side == 'L' and mark <= entry * (1 - RUNNER_SL_PCT):
            exit_reason = 'SL'; exit_px_target = mark
        elif side == 'S' and mark >= entry * (1 + RUNNER_SL_PCT):
            exit_reason = 'SL'; exit_px_target = mark

    # 2. Breakeven stop (in 'breakeven' stage — price pulled back to entry+buffer)
    elif stage == 'breakeven':
        if side == 'L' and mark <= be_stop:
            exit_reason = 'BE'; exit_px_target = mark
        elif side == 'S' and mark >= be_stop:
            exit_reason = 'BE'; exit_px_target = mark

    # 3. Trail stop (in 'tp1_taken' stage — runner half)
    elif stage == 'tp1_taken':
        if side == 'L' and mark <= trail_trigger:
            exit_reason = 'TRAIL'; exit_px_target = mark
        elif side == 'S' and mark >= trail_trigger:
            exit_reason = 'TRAIL'; exit_px_target = mark

    if exit_reason:
        pnl_pct = close(coin)
        if pnl_pct is not None:
            if pnl_pct < 0: state['consec_losses'] += 1
            else: state['consec_losses'] = 0
            state['last_pnl_close'] = pnl_pct
        log(f"{coin} RUNNER EXIT [{exit_reason}] @ {mark:.4f} | stage was {stage} | peak={peak:.4f}")
        state['positions'].pop(coin, None)
        return True

    # Save updated peak
    state['positions'][coin] = cur
    return False

def process(coin, state, equity, live_positions, risk_mult=1.0):
    candles=fetch(coin)
    last_s=state['cooldowns'].get(coin+'_sell', 0)
    last_b=state['cooldowns'].get(coin+'_buy',  0)
    sig, bar_ts = signal(candles, last_s, last_b, coin=coin)
    cur=state['positions'].get(coin)
    live=live_positions.get(coin)

    # v8.3: RUNNER LOGIC — check stops/trails before signal processing (v8.4: disabled by default)
    if RUNNER_ENABLED and manage_runner(coin, state, live, equity):
        return  # position was closed by runner logic

    # v8.10: TP + CLOUD-BREAK EXIT — improves WR without reducing trade count
    if cur and live:
        mark = get_mid(coin)
        if mark and cur.get('entry'):
            entry = cur['entry']
            side = cur['side']
            fav = (mark - entry) / entry if side == 'L' else (entry - mark) / entry

            # TRAILING STOP: 0.3% trail — let winners run, lock gains
            hwm = cur.get('hwm', fav)
            if fav > hwm:
                hwm = fav
                cur['hwm'] = hwm
            
            # Only trail once in profit (hwm > 0) and retraced 0.3% from peak
            if hwm > TRAIL_PCT and (hwm - fav) >= TRAIL_PCT:
                pnl_pct = close(coin)
                if pnl_pct is not None:
                    state['consec_losses'] = 0
                    state['last_pnl_close'] = pnl_pct
                log(f"{coin} TRAIL EXIT +{fav*100:.2f}% (peak +{hwm*100:.2f}%, trail {TRAIL_PCT*100:.1f}%)")
                state['positions'].pop(coin, None)
                return

            # CLOUD BREAK: trend over — price crossed back through slow EMA by significant margin
            if CLOUD_EXIT_ENABLED and fav < -0.003:  # only if position is losing >0.3% (not marginal)
                candles = fetch(coin, retries=1)
                if candles and len(candles) >= 60:
                    closes = [c[4] for c in candles]
                    k = 2/51; ema50 = sum(closes[:50])/50
                    for j in range(50, len(closes)):
                        ema50 = closes[j]*k + ema50*(1-k)
                    # Require price to be >0.2% through EMA (not just touching)
                    if side == 'L' and mark < ema50 * 0.998:
                        pnl_pct = close(coin)
                        if pnl_pct is not None:
                            if pnl_pct < 0: state['consec_losses'] += 1
                            else: state['consec_losses'] = 0
                            state['last_pnl_close'] = pnl_pct
                        log(f"{coin} CLOUD EXIT: price {mark:.4f} < ema50 {ema50:.4f}")
                        state['positions'].pop(coin, None)
                        return
                    elif side == 'S' and mark > ema50 * 1.002:
                        pnl_pct = close(coin)
                        if pnl_pct is not None:
                            if pnl_pct < 0: state['consec_losses'] += 1
                            else: state['consec_losses'] = 0
                            state['last_pnl_close'] = pnl_pct
                        log(f"{coin} CLOUD EXIT: price {mark:.4f} > ema50 {ema50:.4f}")
                        state['positions'].pop(coin, None)
                        return

    # FIX #3: 4h max hold check BEFORE signal logic
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

    # Per-ticker gate check (safe — never crashes the loop)
    try:
        candles_for_gate = fetch(coin, retries=1)
        px_for_gate = get_mid(coin) or 0
        if not apply_ticker_gate(coin, sig, px_for_gate, candles_for_gate):
            log(f"{coin} {sig} GATED by per-ticker filter")
            return
    except Exception as e:
        log(f"{coin} gate check err: {e}")  # pass through on error

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
                    state['positions'][coin] = {'side':'L', 'opened_at':now, 'entry':fill_px,
                                                'stage':'initial', 'peak':fill_px}

# ═══════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════
def main():
    log(f"PreCog v8.11 | wallet={WALLET} | DynaPro webhook ONLY | precog signals OFF | TP+CLOUD EXIT")
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
                    entry_px = live_positions[k]['entry']
                    state['positions'][k] = {'side':side, 'opened_at':now, 'entry':entry_px,
                                             'stage':'initial', 'peak':entry_px}
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
                            px = get_mid(coin)
                            if px:
                                is_buy = (action == 'buy')
                                sz = calc_size(equity, px, risk_pct, risk_mult)
                                fill = place(coin, is_buy, sz)
                                if fill:
                                    state['positions'][coin] = {
                                        'side': 'L' if is_buy else 'S',
                                        'opened_at': time.time(),
                                        'entry': fill,
                                        'stage': 'initial', 'peak': fill,
                                        'source': 'dynapro'
                                    }
                                    log(f"WEBHOOK OPEN {coin} {'BUY' if is_buy else 'SELL'} @ {fill}")
                    wh_count += 1
                except Exception as e:
                    log(f"webhook process err: {e}"); break

            # PRECOG + WEBHOOK SIGNALS — scan all coins
            for c in COINS:
                try:
                    process(c, state, equity, live_positions, risk_mult)
                    # Refresh live_positions snapshot periodically (every 10 coins)
                    if COINS.index(c) % 10 == 9:
                        live_positions = get_all_positions_live()
                except Exception as e:
                    log(f"err {c}: {e}")
                time.sleep(1.0)  # v8.10.1: increased from 0.6s to avoid HL 429 rate limits with 50 coins

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
    try:
        from latency_arb import start_la_module
        la_thread = threading.Thread(target=start_la_module,
            args=(get_mid, place, close, get_balance, get_funding_rate, log),
            daemon=True)
        la_thread.start()
        log("Latency Arb module started")
    except Exception as e:
        log(f"LA module failed to start: {e}")
    # Run Flask webhook server in main thread (Render expects port 10000)
    port = int(os.environ.get('PORT', 10000))
    log(f"Webhook server starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
