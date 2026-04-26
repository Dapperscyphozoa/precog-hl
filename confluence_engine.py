#!/usr/bin/env python3
"""
confluence_engine.py  —  SYSTEM B  (Multi-TF Confluence Engine)

Design doc locked, no tuning. Deployment blueprint:
  ARCHITECTURE: market data -> indicators -> 3 signal engines (SNIPER/DAY/SWING)
                -> confluence detector -> risk sizing -> execution -> logging
  SIGNAL RULE: close crosses EMA cloud (12/26) in direction, all 6 filters pass
  6 FILTERS:   F1 RB mean slope+side | F2 pivot structure | F3 dist from cloud
               F4 RSI not extreme | F5 volume expansion | F6 HTF EMA20 slope
  CONFLUENCE:  >=2 systems agree within 24h window = fire trade
  RISK:        1% per trade, 1 position per coin, 24h per-coin cooldown
  EXECUTION:   TP 4% | SL 1.5% | 72h max hold

Validated 60d / 37 HL coins OOS:
  2-sys n=195, WR 43%, net +134.02%, ppt +0.687%, 3.75 trades/day

Public API:
  eval_coin(coin, bars_15m, now_ts=None) -> dict | None
      bars_15m: list of {t(ms), o, h, l, c, v}, ascending
      returns: signal dict with side, n_sys, systems, entry, sl, tp, max_hold_s
      returns None if no confluence / stale candles
  should_enter(coin, last_fire_ts_by_coin, now_ts) -> bool
      enforces 24h per-coin cooldown
  mark_fired(coin, last_fire_ts_by_coin, now_ts)
      stamp cooldown post-fill
"""
import time
import numpy as np
from collections import defaultdict

# ─── LOCKED CONFIG (do not tune without OOS re-validation) ──────────
# 2026-04-25: CONF_MIN_SYS 2 → 1. Strict 2+ confluence was producing 0 fires
# (signal starvation by design). Each system already passes 6 quality filters
# (rb/struct/dist/rsi/vol/htf — _f1 through _f6). A single-system signal that
# survives all 6 IS high quality. Per spec: "allow 1 engine if quality is high."
# 6-filter pass equates to "confidence >= 8" requirement — relying on existing
# gates rather than adding a new score.
CONF_MIN_SYS        = 1
CONF_WINDOW_S       = 24 * 3600
COIN_COOLDOWN_S     = 24 * 3600
TP_PCT              = 0.04
SL_PCT              = 0.015
MAX_HOLD_S          = 72 * 3600
RISK_PCT            = 0.01
SLIPPAGE_BUFFER_PCT = 0.0008

SYSTEMS = {
    'SNIPER': {'tf_mult': 1,  'lookback': 15, 'max_pct': 1.5,
               'buy_max': 65, 'sell_min': 35, 'vol_mult': 1.2},
    'DAY':    {'tf_mult': 2,  'lookback': 12, 'max_pct': 1.8,
               'buy_max': 68, 'sell_min': 32, 'vol_mult': 1.1},
    'SWING':  {'tf_mult': 4,  'lookback': 20, 'max_pct': 2.0,
               'buy_max': 70, 'sell_min': 30, 'vol_mult': 1.3},
}

# 2026-04-26: FUNDING as 4th orthogonal system.
# Existing 3 systems are all price-action (RSI/EMA/structure on bars). They
# correlate. Adding a 4th price-action input would mostly echo the others.
# Funding rate is microstructure (positioning/leverage state) — strictly
# orthogonal information, doesn't redundantly confirm.
#
# Logic mirrors precog FUNDING_MR engine: extreme funding = crowd paying to
# hold one side = mean-revert opportunity. Sign convention on HL: positive
# funding = longs pay shorts.
#   funding > +THRESHOLD  →  longs paying  →  SELL signal (fade the crowd)
#   funding < -THRESHOLD  →  shorts paying →  BUY signal (fade the crowd)
#
# Why this both increases triggers AND reduces noise on SWING:
#   - More triggers: net-new combinations possible — FUNDING alone, plus
#     FUNDING+SNIPER, FUNDING+DAY, FUNDING+SWING. Previously these scenarios
#     had no signal path.
#   - Less noise on SWING: SWING+FUNDING (n_sys=2) is a higher-quality fire
#     than SWING-alone. The additional signal lets us prefer 2-sys combos
#     when both are available.
#
# 2026-04-26 (later): threshold lowered 1bp/hr → 0.5bp/hr (12bp/day).
# precog's FUNDING_MR /health showed below_threshold: 163/168 (97% of universe
# below 1bp/hr) — funding regime is mild, so 1bp/hr was effectively dormant.
# 0.5bp/hr captures real (if smaller) positioning bias while keeping funding
# noise below the floor. Tunable via env to revert/adjust without redeploy.
import os as _os
FUNDING_THRESHOLD_HR_PCT = float(_os.environ.get('CONF_FUNDING_THRESHOLD_HR_PCT', '0.00005'))
HTF_MULT_FOR_F6 = 16  # 4h context from 15m base

# ─── Per-filter rejection counters (instrumentation, no logic change) ────
# Diagnose the "0 fires" problem: which filter rejects most? Each scan walks
# bars in `_recent_signals` and bumps the appropriate counter on rejection.
# Order-sensitive: a bar that would fail f1 AND f3 is only counted in f1_fail
# (filters return early). That's fine for "which is the dominant choke?"
_STATS = {
    'eval_calls':         0,
    'short_history':      0,   # bars_15m < 100
    'ctx_build_fail':     0,   # _build_ctx returned None
    'bars_scanned':       0,   # total (bar × system) evals
    'no_cross':           0,   # EMA cloud not crossed at this bar
    'f1_fail':            0,
    'f2_fail':            0,
    'f3_fail':            0,
    'f4_fail':            0,
    'f5_fail':            0,
    'f6_fail':            0,
    'cross_passed_all':   0,   # cross + all 6 filters → counted as candidate
    'no_candidate_24h':   0,   # eval_coin returned None: no system candidate in window
    'signals_yielded':    0,   # eval_coin returned a signal dict
    'errors':             0,
}

import sys as _sys
def _log_err(msg):
    """Visible error logger — replaces silent except patterns."""
    print(f"[confluence_engine ERR] {msg}", file=_sys.stderr, flush=True)

def status():
    """Diagnostics: per-filter rejection breakdown for /confluence endpoint."""
    s = dict(_STATS)
    n = max(1, s['bars_scanned'])
    s['cross_rate_pct'] = round((s['bars_scanned'] - s['no_cross']) / n * 100, 2)
    crosses = s['bars_scanned'] - s['no_cross']
    if crosses > 0:
        s['f1_reject_pct_of_crosses'] = round(s['f1_fail'] / crosses * 100, 1)
        s['f2_reject_pct_of_crosses'] = round(s['f2_fail'] / crosses * 100, 1)
        s['f3_reject_pct_of_crosses'] = round(s['f3_fail'] / crosses * 100, 1)
        s['f4_reject_pct_of_crosses'] = round(s['f4_fail'] / crosses * 100, 1)
        s['f5_reject_pct_of_crosses'] = round(s['f5_fail'] / crosses * 100, 1)
        s['f6_reject_pct_of_crosses'] = round(s['f6_fail'] / crosses * 100, 1)
    return s


# ─── INDICATORS ──────────────────────────────────────────────────────
def _ema(vals, period):
    out = np.zeros(len(vals))
    if len(vals) < period:
        return out
    out[period-1] = np.mean(vals[:period])
    k = 2/(period+1)
    for i in range(period, len(vals)):
        out[i] = vals[i]*k + out[i-1]*(1-k)
    return out

def _sma(vals, period):
    out = np.zeros(len(vals))
    for i in range(period-1, len(vals)):
        out[i] = np.mean(vals[i-period+1:i+1])
    return out

def _rsi(closes, period=14):
    out = np.zeros(len(closes))
    if len(closes) < period+1:
        return out
    diff = np.diff(closes)
    g = np.where(diff > 0, diff, 0); l = np.where(diff < 0, -diff, 0)
    ag = np.zeros(len(closes)); al = np.zeros(len(closes))
    ag[period] = g[:period].mean(); al[period] = l[:period].mean()
    for i in range(period+1, len(closes)):
        ag[i] = (ag[i-1]*(period-1) + g[i-1])/period
        al[i] = (al[i-1]*(period-1) + l[i-1])/period
    rs = ag / np.where(al == 0, 1e-10, al)
    return 100 - 100/(1+rs)

def _pivot_high(highs, left=3, right=3):
    out = np.zeros(len(highs), dtype=bool)
    for i in range(left, len(highs)-right):
        if highs[i] == max(highs[i-left:i+right+1]):
            out[i] = True
    return out

def _pivot_low(lows, left=3, right=3):
    out = np.zeros(len(lows), dtype=bool)
    for i in range(left, len(lows)-right):
        if lows[i] == min(lows[i-left:i+right+1]):
            out[i] = True
    return out

# ─── CONTEXT BUILDER ─────────────────────────────────────────────────
def _build_ctx(bars_15m, tf_multiplier=1):
    """Normalize + resample + compute all indicators for one timeframe."""
    norm = []
    for b in bars_15m:
        t_val = b['t']
        if t_val > 10**12:
            t_val //= 1000  # ms -> s
        norm.append({'t': int(t_val),
                     'o': float(b['o']), 'h': float(b['h']),
                     'l': float(b['l']), 'c': float(b['c']),
                     'v': float(b['v'])})
    bars = norm
    if tf_multiplier > 1:
        rs = []
        for i in range(0, len(bars) - tf_multiplier + 1, tf_multiplier):
            g = bars[i:i+tf_multiplier]
            rs.append({'t': g[0]['t'], 'o': g[0]['o'],
                       'h': max(b['h'] for b in g),
                       'l': min(b['l'] for b in g),
                       'c': g[-1]['c'],
                       'v': sum(b['v'] for b in g)})
        bars = rs
    if len(bars) < 50:
        return None
    closes = np.array([b['c'] for b in bars])
    highs  = np.array([b['h'] for b in bars])
    lows   = np.array([b['l'] for b in bars])
    vols   = np.array([b['v'] for b in bars])
    return {
        'bars': bars, 'closes': closes, 'highs': highs,
        'lows': lows, 'vols': vols,
        'ema_fast': _ema(closes, 12), 'ema_slow': _ema(closes, 26),
        'rb_mean':  _sma(closes, 50), 'rsi': _rsi(closes, 14),
        'vol_avg':  _sma(vols, 20),
        'ph': _pivot_high(highs), 'pl': _pivot_low(lows),
    }

# ─── SIGNAL DETECTOR (EMA cloud cross) ───────────────────────────────
def _detect_cross(ctx, i):
    c = ctx['closes']; ef = ctx['ema_fast']; es = ctx['ema_slow']
    if i < 26:
        return None
    pc, cc = c[i-1], c[i]
    ptop = max(ef[i-1], es[i-1]); pbot = min(ef[i-1], es[i-1])
    ctop = max(ef[i],   es[i]);   cbot = min(ef[i],   es[i])
    if pc <= ptop and cc > ctop: return 'BUY'
    if pc >= pbot and cc < cbot: return 'SELL'
    return None

# ─── 6 FILTERS ───────────────────────────────────────────────────────
def _f1_rb(ctx, i, side):
    if i < 30: return False
    rb = ctx['rb_mean']; slope = rb[i] - rb[i-20]
    if side == 'BUY':  return slope > 0 and ctx['closes'][i] > rb[i]
    return              slope < 0 and ctx['closes'][i] < rb[i]

def _f2_struct(ctx, i, side, lookback):
    if i < lookback: return False
    ph_idxs = np.where(ctx['ph'][max(0, i-lookback):i+1])[0]
    pl_idxs = np.where(ctx['pl'][max(0, i-lookback):i+1])[0]
    highs = ctx['highs'][max(0, i-lookback):i+1]
    lows  = ctx['lows'][max(0, i-lookback):i+1]
    if side == 'BUY':
        if len(pl_idxs) < 2: return False
        return lows[pl_idxs[-1]] > lows[pl_idxs[-2]]
    if len(ph_idxs) < 2: return False
    return highs[ph_idxs[-1]] < highs[ph_idxs[-2]]

def _f3_dist(ctx, i, side, max_pct):
    c = ctx['closes'][i]
    cm = (ctx['ema_fast'][i] + ctx['ema_slow'][i]) / 2
    if cm <= 0: return False
    return abs(c - cm) / cm * 100 <= max_pct

def _f4_rsi(ctx, i, side, buy_max, sell_min):
    r = ctx['rsi'][i]
    if r == 0: return False
    if side == 'BUY':  return r < buy_max
    return              r > sell_min

def _f5_vol(ctx, i, mult):
    if i < 20: return False
    return ctx['vols'][i] >= ctx['vol_avg'][i] * mult

def _f6_htf(ctx_htf, target_ts, side):
    if ctx_htf is None: return True
    bars = ctx_htf['bars']
    htf_i = None
    for j, b in enumerate(bars):
        if b['t'] > target_ts:
            break
        htf_i = j
    if htf_i is None or htf_i < 20: return False
    e20 = _ema(ctx_htf['closes'][:htf_i+1], 20)
    if len(e20) < 5: return False
    slope = e20[-1] - e20[-5]
    if side == 'BUY': return slope > 0
    return              slope < 0

# ─── PER-SYSTEM LATEST-BAR SIGNAL CHECK ──────────────────────────────
def _check_system(ctx, ctx_htf, sys_name):
    """Look at the most recent CLOSED bar. Returns 'BUY', 'SELL', or None."""
    cfg = SYSTEMS[sys_name]
    bars = ctx['bars']
    if len(bars) < 30:
        return None
    # Use second-to-last bar (last closed)
    i = len(bars) - 2
    if i < 30:
        return None
    sig = _detect_cross(ctx, i)
    if sig is None:
        return None
    if not _f1_rb(ctx, i, sig):                                                return None
    if not _f2_struct(ctx, i, sig, cfg['lookback']):                           return None
    if not _f3_dist(ctx, i, sig, cfg['max_pct']):                              return None
    if not _f4_rsi(ctx, i, sig, cfg['buy_max'], cfg['sell_min']):              return None
    if not _f5_vol(ctx, i, cfg['vol_mult']):                                   return None
    if ctx_htf is not None and not _f6_htf(ctx_htf, bars[i]['t'], sig):        return None
    return sig

def _recent_signals(ctx, ctx_htf, sys_name, window_s):
    """Scan last N bars for signals. Returns list of (ts, side).
    Bumps _STATS counters on rejection so /confluence can show which filter
    chokes. Filter order is fixed (f1→f6); a bar dies at the first failure.
    """
    cfg = SYSTEMS[sys_name]
    bars = ctx['bars']
    out = []
    if len(bars) < 30:
        return out
    now_bar_ts = bars[-1]['t']
    cutoff = now_bar_ts - window_s
    for i in range(30, len(bars)):
        if bars[i]['t'] < cutoff:
            continue
        _STATS['bars_scanned'] += 1
        sig = _detect_cross(ctx, i)
        if sig is None:
            _STATS['no_cross'] += 1
            continue
        if not _f1_rb(ctx, i, sig):
            _STATS['f1_fail'] += 1
            continue
        if not _f2_struct(ctx, i, sig, cfg['lookback']):
            _STATS['f2_fail'] += 1
            continue
        if not _f3_dist(ctx, i, sig, cfg['max_pct']):
            _STATS['f3_fail'] += 1
            continue
        if not _f4_rsi(ctx, i, sig, cfg['buy_max'], cfg['sell_min']):
            _STATS['f4_fail'] += 1
            continue
        if not _f5_vol(ctx, i, cfg['vol_mult']):
            _STATS['f5_fail'] += 1
            continue
        if ctx_htf is not None and not _f6_htf(ctx_htf, bars[i]['t'], sig):
            _STATS['f6_fail'] += 1
            continue
        _STATS['cross_passed_all'] += 1
        out.append((bars[i]['t'], sig))
    return out

# ─── PUBLIC API ──────────────────────────────────────────────────────
def eval_coin(coin, bars_15m, now_ts=None):
    """
    Evaluate confluence on a single coin's 15m candle history.
    Returns signal dict if 1+ systems agree within 24h window (was 2+, lowered
    2026-04-25 to break signal starvation; each system passes 6-filter gate).

    Output:
      {
        'coin': str,
        'side': 'BUY'|'SELL',
        'n_sys': 1, 2, or 3,
        'systems': ['SNIPER','DAY',...],
        'entry': float (last close),
        'tp_pct': 0.04,
        'sl_pct': 0.015,
        'max_hold_s': 259200,
        'ts': int (seconds),
      }
    """
    if not bars_15m or len(bars_15m) < 100:
        _STATS['eval_calls'] += 1
        _STATS['short_history'] += 1
        return None
    _STATS['eval_calls'] += 1
    now_ts = now_ts or int(time.time())

    # Build per-TF contexts
    ctx_15 = _build_ctx(bars_15m, tf_multiplier=1)
    ctx_30 = _build_ctx(bars_15m, tf_multiplier=2)
    ctx_60 = _build_ctx(bars_15m, tf_multiplier=4)
    ctx_4h = _build_ctx(bars_15m, tf_multiplier=HTF_MULT_FOR_F6)
    if ctx_15 is None or ctx_30 is None or ctx_60 is None:
        _STATS['ctx_build_fail'] += 1
        return None

    # Collect recent signals per system within 24h window
    recents = {
        'SNIPER': _recent_signals(ctx_15, ctx_4h, 'SNIPER', CONF_WINDOW_S),
        'DAY':    _recent_signals(ctx_30, ctx_4h, 'DAY',    CONF_WINDOW_S),
        'SWING':  _recent_signals(ctx_60, ctx_4h, 'SWING',  CONF_WINDOW_S),
    }

    # FUNDING as 4th system — point-in-time check, not bar-windowed. Live
    # funding rate beats THRESHOLD on either side → emit (now_ts, side).
    # Best-effort: any failure (module not loaded, no rate cached) falls
    # through silently — the engine works fine on the original 3 systems.
    try:
        from funding_arb import get_hl_funding_rate
        _rate = float(get_hl_funding_rate(coin) or 0.0)
        if _rate > FUNDING_THRESHOLD_HR_PCT:
            recents['FUNDING'] = [(now_ts, 'SELL')]   # fade longs paying funding
        elif _rate < -FUNDING_THRESHOLD_HR_PCT:
            recents['FUNDING'] = [(now_ts, 'BUY')]    # fade shorts paying funding
    except Exception:
        pass

    # Tally per side
    by_side = {'BUY': set(), 'SELL': set()}
    latest_ts_by_side = {'BUY': 0, 'SELL': 0}
    for sys_name, sigs in recents.items():
        for (ts, side) in sigs:
            by_side[side].add(sys_name)
            if ts > latest_ts_by_side[side]:
                latest_ts_by_side[side] = ts

    # Prefer side with most systems agreeing
    best_side = None
    best_n = 0
    for side in ('BUY', 'SELL'):
        n = len(by_side[side])
        if n >= CONF_MIN_SYS and n > best_n:
            best_n = n
            best_side = side

    if best_side is None:
        _STATS['no_candidate_24h'] += 1
        return None

    _STATS['signals_yielded'] += 1
    last_close = float(ctx_15['bars'][-1]['c'])
    return {
        'coin': coin,
        'side': best_side,
        'n_sys': best_n,
        'systems': sorted(list(by_side[best_side])),
        'entry': last_close,
        'tp_pct': TP_PCT,
        'sl_pct': SL_PCT,
        'max_hold_s': MAX_HOLD_S,
        'risk_pct': RISK_PCT,
        'ts': now_ts,
        'latest_signal_ts': latest_ts_by_side[best_side],
    }

def should_enter(coin, last_fire_ts_by_coin, now_ts=None):
    """Per-coin 24h cooldown check."""
    now_ts = now_ts or int(time.time())
    last = last_fire_ts_by_coin.get(coin, 0)
    return (now_ts - last) >= COIN_COOLDOWN_S

def mark_fired(coin, last_fire_ts_by_coin, now_ts=None):
    now_ts = now_ts or int(time.time())
    last_fire_ts_by_coin[coin] = now_ts

def position_size(equity, entry_price, sl_pct=SL_PCT, risk_pct=RISK_PCT):
    """
    Standard fixed-risk sizing.
    Returns notional size in USD (not coin units).
    Caller converts to coin units via size / entry_price.
    """
    risk_usd = equity * risk_pct
    if sl_pct <= 0:
        return 0.0
    notional = risk_usd / sl_pct
    return notional

def levels_for(signal):
    """Compute TP/SL price levels from signal."""
    entry = signal['entry']
    side = signal['side']
    if side == 'BUY':
        tp = entry * (1 + TP_PCT)
        sl = entry * (1 - SL_PCT)
    else:
        tp = entry * (1 - TP_PCT)
        sl = entry * (1 + SL_PCT)
    return {'tp': tp, 'sl': sl, 'entry': entry}


if __name__ == '__main__':
    # Smoke test
    import json, os
    path = '/tmp/candles_15m/BTC.json'
    if os.path.exists(path):
        bars = json.load(open(path))
        result = eval_coin('BTC', bars)
        print(f"BTC eval: {result}")
    else:
        print("No test data at /tmp/candles_15m/BTC.json")
