#!/usr/bin/env python3
"""PreCog v8.6 — EXPANDED UNIVERSE (13 coins, validated per-coin)

v8.6 changes:
- Coin universe: 8 → 13 (BT-validated from 21-coin sweep)
- CHASE_GATE extended: BTC, SUI, DOT, ATOM, FARTCOIN (all +15-30pp WR with gate)
- BNB moved to RAW (raw 65.6% ret +1.9% vs gated 82.4% ret +1.8% — similar)
- Added high-edge coins from 30-day validation:
    APT    76.9% RAW  +18.8%/30d  ← star
    PEPE   78.4% RAW  +15.1%/30d  (HL: kPEPE)
    BONK   77.5% RAW   +8.9%/30d  (HL: kBONK)
    SHIB   77.8% RAW   +4.2%/30d  (HL: kSHIB)
    SUI    70.6% GATE  +6.4%/30d
    DOT    77.3% GATE  +3.2%/30d
    ATOM   77.3% GATE  +4.2%/30d
- Dropped: ETH, ARB, DOGE, NEAR, TRX, RENDER (sub-edge in 30d BT)

Portfolio BT (30d, maker fees, 10x, 5% risk, selective gating):
  13 coins | 7.94 trades/day | 75.9% avg WR | +149.7% / 30d
  Daily compound: +3.10%
  Trajectory: $229 → $574 (30d) → $3.6K (90d) → $55K (180d) → $15.7M (365d)
"""
import os, json, time, random, traceback
from datetime import datetime
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

WALLET     = os.environ['HYPERLIQUID_ACCOUNT']
PRIV_KEY   = os.environ['HL_PRIVATE_KEY']
STATE_PATH = '/var/data/precog_state.json'
KILL_FILE  = '/var/data/KILL'

# v8.6 coin list — 13 validated keepers from 21-coin BT
# RAW (no gate): SOL 75.8%, BNB 65.6%, LINK 75.0%, XRP 74.3%, APT 76.9%,
#                PEPE 78.4%, BONK 77.5%, SHIB 77.8%, FARTCOIN 68.4%
# GATED: BTC 92.3%, SUI 70.6%, DOT 77.3%, ATOM 77.3%
# DROPPED: ETH, ARB, DOGE, NEAR, TRX, RENDER (sub-65% WR in BT, negative return)
# Note: HL uses k-prefix for 1000x tokens (kPEPE, kBONK, kSHIB). Using HL names.
COINS = [
    # High-conviction (75%+ WR validated)
    'SOL','LINK','APT','kPEPE','kBONK','kSHIB','BTC','FARTCOIN',
    # Mid-conviction (70-75% WR validated)
    'XRP','BNB','SUI','DOT','ATOM',
]

# v8.6 SELECTIVE GATE — per-BT chase-filter coins
CHASE_GATE_COINS = {'BTC','SUI','DOT','ATOM','FARTCOIN'}
CHASE_LOOKBACK = 20  # bars to measure 20-bar hi/lo range

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

INITIAL_RISK_PCT = 0.05
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
CB_PAUSE_SEC = 3600
FUNDING_CUT_RATIO = 0.20

# v8.3 RUNNER LOGIC — DISABLED in v8.4 (BT showed it hurt performance).
# Kept code in place for future re-enable if validated on different data.
RUNNER_ENABLED  = False
RUNNER_SL_PCT   = 0.004
RUNNER_TP1_PCT  = 0.005
RUNNER_TP2_PCT  = 0.010
RUNNER_TRAIL    = 0.007
RUNNER_BE_BUFF  = 0.0005

# v8.4 MAKER ORDER settings
MAKER_FALLBACK_SEC = 30  # if Alo doesn't fill in 30s, fallback to Ioc taker

info = Info(constants.MAINNET_API_URL, skip_ws=True)
account = Account.from_key(PRIV_KEY)
exchange = Exchange(account, constants.MAINNET_API_URL, account_address=WALLET)

def log(m): print(f"[{datetime.utcnow().isoformat()}] {m}", flush=True)

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
    """v8.4: Try MAKER (Alo post-only) first, fall back to TAKER (Ioc) if not filled."""
    px=get_mid(coin)
    if not px: return None
    set_isolated_leverage(coin)
    # Maker limit price: passive side of book — buy at bid, sell at ask (slightly less aggressive)
    # Use exact current mid for post-only — HL rejects crossing orders as non-maker.
    maker_px = round(px * (0.9998 if is_buy else 1.0002), 6)
    try:
        r = exchange.order(coin, is_buy, size, maker_px, {'limit':{'tif':'Alo'}}, reduce_only=False)
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'resting' in status or 'filled' in status:
            log(f"MAKER {coin} {'BUY' if is_buy else 'SELL'} {size}@{maker_px}: {status}")
            # Poll up to MAKER_FALLBACK_SEC for fill
            oid = status.get('resting',{}).get('oid') or status.get('filled',{}).get('oid')
            if 'filled' in status:
                return maker_px
            # Resting — wait briefly then check
            for wait_s in range(MAKER_FALLBACK_SEC):
                time.sleep(1)
                state_now = info.user_state(WALLET)
                has_pos = any(p['position'].get('coin')==coin and float(p['position'].get('szi',0))!=0
                              for p in state_now.get('assetPositions',[]))
                if has_pos:
                    log(f"MAKER fill {coin} after {wait_s+1}s")
                    return maker_px
            # Cancel unfilled maker and fall back to taker
            try:
                exchange.cancel(coin, oid)
                log(f"MAKER unfilled {coin}, canceling oid={oid} -> TAKER fallback")
            except Exception as ce:
                log(f"cancel err {coin}: {ce}")
    except Exception as e:
        log(f"maker place err {coin}: {e}")

    # Taker fallback
    slip = round(px * (1.005 if is_buy else 0.995), 6)
    try:
        r = exchange.order(coin, is_buy, size, slip, {'limit':{'tif':'Ioc'}}, reduce_only=False)
        log(f"TAKER {coin} {'BUY' if is_buy else 'SELL'} {size}@{slip}: {r}")
        return px
    except Exception as e:
        log(f"taker err {coin}: {e}")
        return None

def close(coin):
    """Returns realized pnl_pct for logging (FIX #11)."""
    live = get_all_positions_live().get(coin)
    if not live: return None
    is_buy=live['size']<0; size=abs(live['size']); px=get_mid(coin)
    if not px: return None
    slip=round(px*1.01,4) if is_buy else round(px*0.99,4)
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
        slip_px = round(mark * (1.005 if is_buy_close else 0.995), 6)
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

    # FIX #3: 4h max hold check BEFORE signal logic
    if cur and cur.get('opened_at'):
        age = time.time() - cur['opened_at']
        if age > MAX_HOLD_SEC:
            log(f"{coin} MAX HOLD exceeded ({age/3600:.1f}h) — force close")
            pnl_pct = close(coin)
            if pnl_pct is not None:
                if pnl_pct < 0: state['consec_losses'] += 1
                else: state['consec_losses'] = 0
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
    log(f"PreCog v8.6 | wallet={WALLET} | coins={len(COINS)} | 5m | {LEV}x ISOLATED | MAKER + CHASE-GATE")
    log(f"Universe: {COINS}")
    log(f"Chase-gate coins: {CHASE_GATE_COINS} | lookback={CHASE_LOOKBACK} bars")
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

            for c in COINS:
                try:
                    process(c, state, equity, live_positions, risk_mult)
                    # Refresh live_positions snapshot periodically (every 10 coins)
                    if COINS.index(c) % 10 == 9:
                        live_positions = get_all_positions_live()
                except Exception as e:
                    log(f"err {c}: {e}")
                time.sleep(0.6)

            save_state(state)
            log(f"--- tick complete ---")
        except Exception as e:
            log(f"tick err: {e}\n{traceback.format_exc()}")
        time.sleep(LOOP_SEC)

if __name__ == '__main__':
    main()
