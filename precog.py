#!/usr/bin/env python3
"""PreCog v4 — 5m winning grid config, 30% risk with auto-scaledown at $50k

GRID WINNER (3,840 configs tested on 47 MT4 tickers + 27 HL coins):
  sens=1, rsi=3, wick=1, ext=1, block=1, vol=1, cd=3
  Backtest: 92.9% WR / PF 55 / 283 trades/day MT4, 333/day HL

ARCHIVED PREVIOUS CONFIG (v3):
  15m candles, 5x/20%, asymmetric SELL/BUY sliders
  SELL = sens:5,rsi:7,wick:1,ext:7,struct:5,vol:1,cd:6
  BUY  = sens:1,rsi:3,wick:4,ext:4,struct:5,vol:1,cd:9
"""
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

# === WINNING GRID CONFIG — symmetric BUY/SELL, 5m timeframe ===
GRID = {'sens':1, 'rsi':3, 'wick':1, 'ext':1, 'block':1, 'vol':1, 'cd':3}

def derive(s):
    return {
        'lb':       max(2, 2 + (s['ext']-1)*15),        # ext=1 → lb=2 (any local high/low)
        'rsi_hi':   50 + s['rsi']*3,                     # rsi=3 → 59
        'rsi_lo':   50 - s['rsi']*3,                     # rsi=3 → 41
        'wick':     (s['wick']-1) * 0.07,                # wick=1 → 0 (bypassed)
        'struct_n': 99 if s['block']<2 else max(2, round(7 - s['block']*0.5)),  # block=1 → 99 (off)
        'pivot_lb': max(2, 9 - s['sens']),               # sens=1 → 8 (wide pivots)
        'vol_mult': 1.0 + (s['vol']-1)*0.15,             # vol=1 → 1.0 (off)
        'cd':       s['cd']                              # cd=3 bars
    }
SP = derive(GRID)   # SELL params
BP = derive(GRID)   # BUY params (symmetric)

# === RISK CONFIG with auto-scaledown at $50k ===
INITIAL_RISK_PCT = 0.03      # 3% per position (20 concurrent = 60% deployed)
SCALED_RISK_PCT  = 0.005     # 0.5% post-50k (maintain diversification)
SCALE_DOWN_AT    = 50000     # $50k trigger

LEV = 10                     # 10x leverage (up from 5x — matches backtest model)
LOOP_SEC = 300               # 5 min loop (5m bar close cadence)

# === POSITION CAPS (safety) ===
MAX_POSITIONS = 20           # v5: 20 concurrent = 20% total at 1%/trade
MAX_SAME_SIDE = 15           # max one side (keeps some directional balance)
MAX_TOTAL_RISK = 0.65        # v6.1: 20 x 3% = 60% + 5% buffer
BTC_VOL_THRESHOLD = 0.03     # 3% 1h range = halve risk
# v6.4 FAST rules (no price SL — winners dip before running)
CUT_NEG_BARS = 12          # 60min: if still negative, cut at market
CHOP_CUT_BARS = 9          # 45min: if |PnL| < 0.5%, cut at market
CHOP_BAND = 0.005          # 0.5% price band for chop detection
HARD_MAX_BARS = 48         # 4h: absolute hold ceiling

info = Info(constants.MAINNET_API_URL, skip_ws=True)
account = Account.from_key(PRIV_KEY)
exchange = Exchange(account, constants.MAINNET_API_URL, account_address=WALLET)

def log(m): print(f"[{datetime.utcnow().isoformat()}] {m}", flush=True)

def current_risk_pct(equity):
    return SCALED_RISK_PCT if equity >= SCALE_DOWN_AT else INITIAL_RISK_PCT

def load_state():
    s = {'positions':{}, 'cooldowns':{}}
    try:
        with open(STATE_PATH) as f: loaded = json.load(f)
        if 'positions' in loaded: s['positions'] = loaded['positions']
        if 'cooldowns' in loaded: s['cooldowns'] = loaded['cooldowns']
    except: pass
    # Migrate old string-format positions to dict format {side, opened_at}
    for k,v in list(s['positions'].items()):
        if isinstance(v, str):
            s['positions'][k] = {'side': v, 'opened_at': time.time()}
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
    """Fetch 5m candles instead of 15m"""
    end=int(time.time()*1000); start=end-n_bars*5*60*1000
    try:
        d=info.candles_snapshot(coin,'5m',start,end)
        return [(int(c['t']),float(c['o']),float(c['h']),float(c['l']),float(c['c']),float(c['v'])) for c in d]
    except Exception as e:
        log(f"candle err {coin}: {e}"); return []

def signal(candles, last_sell_bar, last_buy_bar):
    """Grid winning config: sens=1 (wide pivot lb=8), rsi=3, wick=1 (off), 
       ext=1 (any local high/low, lb=2), block=1 (off), vol=1 (off), cd=3"""
    if len(candles)<100: return None,None
    o=[c[1] for c in candles]; h=[c[2] for c in candles]; l=[c[3] for c in candles]
    cl=[c[4] for c in candles]; v=[c[5] for c in candles]
    N=len(cl); r14=rsi_calc(cl,14)
    
    i = N-1
    if r14[i] is None: return None, None
    br = h[i]-l[i]
    if br <= 0: return None, None
    uw = (h[i]-max(o[i],cl[i]))/br
    lw = (min(o[i],cl[i])-l[i])/br
    
    # GRID conditions (all bypassed when slider=1):
    # - Multi-bar high/low: lb=2, so low==min(l[i-2:i+3])  (any local bottom qualifies)
    # - Wick: slider=1 → 0 threshold (bypass)
    # - RSI: slider=3 → lo=41, hi=59 (moderate requirement)
    # - Block (trend): slider=1 → struct_n=99 (bypassed)
    # - Volume: slider=1 → 1.0x (bypass)
    # - Sens: pivot_lb=8 (pivot confirmation window)
    
    LB = SP['pivot_lb']  # =8 for sens=1
    # Check multi-bar confirmation
    is_pivot_high = h[i] == max(h[max(0,i-LB):i+1])
    is_pivot_low  = l[i] == min(l[max(0,i-LB):i+1])
    
    sell_ok = is_pivot_high and r14[i] > SP['rsi_hi'] and (i-last_sell_bar) > SP['cd']
    buy_ok  = is_pivot_low  and r14[i] < BP['rsi_lo'] and (i-last_buy_bar)  > BP['cd']
    
    if sell_ok: return 'SELL', i
    if buy_ok:  return 'BUY', i
    return None, None


def get_balance():
    try: return float(info.user_state(WALLET)['marginSummary']['accountValue'])
    except: return 0

def get_total_locked(state):
    """Sum of notional margin currently locked in open positions (approximation via position count × risk)"""
    try:
        user = info.user_state(WALLET)
        total_margin = float(user['marginSummary'].get('totalMarginUsed', 0))
        return total_margin
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

def calc_size(equity, px, risk_pct, risk_mult=1.0):
    raw = equity * risk_pct * risk_mult * LEV / px
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
    candles = fetch(coin)
    last_s = state['cooldowns'].get(coin+'_sell', -1000)
    last_b = state['cooldowns'].get(coin+'_buy', -1000)
    sig, bar = signal(candles, last_s, last_b)
    if not sig: return
    
    cur_entry = state['positions'].get(coin)
    cur = cur_entry.get('side') if isinstance(cur_entry, dict) else cur_entry
    open_pos = {k:(v.get('side') if isinstance(v,dict) else v) for k,v in state['positions'].items() if v}
    want_side = 'L' if sig=='BUY' else 'S'
    
    # Position cap
    if not cur and len(open_pos) >= MAX_POSITIONS:
        log(f"{coin} {sig} SKIP (max {MAX_POSITIONS} positions)"); return
    # Same-side cap
    same_side = sum(1 for v in open_pos.values() if v == want_side)
    if not cur and same_side >= MAX_SAME_SIDE:
        log(f"{coin} {sig} SKIP (side cap {MAX_SAME_SIDE})"); return
    # Total margin cap
    risk_pct = current_risk_pct(equity)
    total_locked = get_total_locked(state)
    proposed = equity * risk_pct * risk_mult
    if not cur and (total_locked + proposed) / equity > MAX_TOTAL_RISK:
        log(f"{coin} {sig} SKIP (margin cap: locked={total_locked:.0f} +{proposed:.0f} > {MAX_TOTAL_RISK*100:.0f}%)"); return
    
    log(f"{coin} SIGNAL: {sig} (risk={risk_pct*100:.1f}% mult={risk_mult})")
    
    if sig == 'SELL':
        state['cooldowns'][coin+'_sell'] = bar
        if cur == 'L': close(coin); state['positions'][coin] = None
        if cur != 'S':
            px = get_mid(coin)
            if px:
                place(coin, False, calc_size(equity, px, risk_pct, risk_mult))
                state['positions'][coin] = {'side': 'S', 'opened_at': time.time()}
    else:
        state['cooldowns'][coin+'_buy'] = bar
        if cur == 'S': close(coin); state['positions'][coin] = None
        if cur != 'L':
            px = get_mid(coin)
            if px:
                place(coin, True, calc_size(equity, px, risk_pct, risk_mult))
                state['positions'][coin] = {'side': 'L', 'opened_at': time.time()}

def main():
    log(f"PreCog v6 | wallet={WALLET} | coins={len(COINS)} | 5m | {LEV}x lev")
    log(f"Risk: {INITIAL_RISK_PCT*100:.1f}% → {SCALED_RISK_PCT*100:.2f}% at ${SCALE_DOWN_AT} | 20 concurrent, NO STACKING")
    log(f"Caps: max_pos={MAX_POSITIONS} side={MAX_SAME_SIDE} margin={int(MAX_TOTAL_RISK*100)}% | v6.4 rules: neg_cut=1h chop=45min max=4h")
    log(f"Grid config: {GRID}")
    log(f"Derived: pivot_lb={SP['pivot_lb']} rsi_lo={BP['rsi_lo']} rsi_hi={SP['rsi_hi']} cd={SP['cd']}")
    
    while True:
        try:
            state = load_state()
            equity = get_balance()
            # RECONCILE state with actual HL positions — kills phantom entries
            try:
                actual = {}
                for p in info.user_state(WALLET).get('assetPositions', []):
                    pos = p['position']
                    sz = float(pos.get('szi', 0))
                    if sz != 0:
                        actual[pos['coin']] = 'L' if sz > 0 else 'S'
                # Drop any state positions not actually open on HL
                phantom = [k for k in list(state['positions'].keys()) if state['positions'][k] and k not in actual]
                for k in phantom:
                    log(f"RECONCILE: clearing phantom {k}")
                    state['positions'][k] = None
                # Add/update actual positions (preserve opened_at if already tracked)
                for k, v in actual.items():
                    existing = state['positions'].get(k)
                    existing_side = existing.get('side') if isinstance(existing, dict) else existing
                    if existing_side != v:
                        state['positions'][k] = {'side': v, 'opened_at': time.time()}
                        log(f"RECONCILE: tracking existing {k} {v}")
            except Exception as e:
                log(f"reconcile err: {e}")
            
            # BTC vol throttle
            risk_mult = 1.0
            try:
                btc_c = fetch('BTC')
                if len(btc_c) >= 12:  # last 12 x 5m = 1h
                    recent = btc_c[-12:]
                    hi = max(c[2] for c in recent); lo = min(c[3] for c in recent)
                    btc_range = (hi-lo)/lo
                    if btc_range > BTC_VOL_THRESHOLD:
                        risk_mult = 0.5
                        log(f"BTC vol {btc_range*100:.1f}% > {BTC_VOL_THRESHOLD*100:.0f}% — risk halved")
            except Exception as e: log(f"vol check err: {e}")
            
            cur_risk = current_risk_pct(equity)
            scaled_status = "SCALED" if equity >= SCALE_DOWN_AT else "INITIAL"
            log(f"--- tick eq=${equity:.2f} risk={int(cur_risk*100)}%({scaled_status}) mult={risk_mult} positions={sum(1 for v in state['positions'].values() if v)} ---")
            # v6.4 FAST RULES: check each open position per tick
            # - HARD MAX (4h): force close
            # - NEG CUT (1h): if still negative, cut
            # - CHOP CUT (45min): if |PnL| < 0.5%, cut at breakeven
            now = time.time()
            hard_max_sec = HARD_MAX_BARS * 5 * 60     # 14400s = 4h
            neg_cut_sec = CUT_NEG_BARS * 5 * 60       # 3600s = 1h
            chop_sec = CHOP_CUT_BARS * 5 * 60         # 2700s = 45min
            for coin, entry in list(state['positions'].items()):
                if not entry or not isinstance(entry, dict): continue
                age = now - entry.get('opened_at', now)
                if age < chop_sec: continue  # too young for any rule
                # Get live PnL
                try:
                    pos = get_position(coin)
                    if not pos: continue
                    entry_px = float(pos.get('entry', 0))
                    mid = get_mid(coin)
                    if not entry_px or not mid: continue
                    if entry['side'] == 'L':
                        pnl_pct = (mid - entry_px) / entry_px
                    else:
                        pnl_pct = (entry_px - mid) / entry_px
                except Exception as e:
                    log(f"pnl check err {coin}: {e}")
                    continue
                # HARD MAX
                if age > hard_max_sec:
                    log(f"HARD_MAX {coin} {entry['side']} age={age/60:.0f}min pnl={pnl_pct*100:+.2f}% — closing")
                    try: close(coin); state['positions'][coin] = None
                    except Exception as e: log(f"close err {coin}: {e}")
                    time.sleep(0.3)
                    continue
                # NEG CUT at 1h
                if age > neg_cut_sec and pnl_pct < 0:
                    log(f"NEG_CUT {coin} {entry['side']} age={age/60:.0f}min pnl={pnl_pct*100:+.2f}% — closing")
                    try: close(coin); state['positions'][coin] = None
                    except Exception as e: log(f"close err {coin}: {e}")
                    time.sleep(0.3)
                    continue
                # CHOP CUT at 45min
                if age > chop_sec and abs(pnl_pct) < CHOP_BAND:
                    log(f"CHOP_CUT {coin} {entry['side']} age={age/60:.0f}min pnl={pnl_pct*100:+.2f}% — closing")
                    try: close(coin); state['positions'][coin] = None
                    except Exception as e: log(f"close err {coin}: {e}")
                    time.sleep(0.3)
                    continue
            
            for c in COINS:
                try:
                    process(c, state, equity, risk_mult)
                except Exception as e:
                    log(f"err {c}: {e}")
                time.sleep(0.6)  # rate limit (was hitting 429)
            
            save_state(state)
            log(f"--- tick complete ---")
        except Exception as e:
            log(f"tick err: {e}\n{traceback.format_exc()}")
        time.sleep(LOOP_SEC)

if __name__ == '__main__':
    main()
