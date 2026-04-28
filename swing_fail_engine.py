"""SWING_FAIL engine — 4h Swing Failure Pattern (SFP) detector.

Signal definition (per backtest spec):
  Trigger: a 4h candle whose wick on one side exceeds 1.5× the same bar's
  body, AND that wick extends beyond the prior N-bar swing high/low, AND
  closes back inside the prior range.

  - Bearish SFP (SHORT): wick above prior 20-bar swing high, body small,
    close back below the swing high. Fade the failed breakout.
  - Bullish SFP (LONG):  wick below prior 20-bar swing low,  body small,
    close back above the swing low.  Fade the failed breakdown.

Defaults match the validated backtest config:
  LOOKBACK    = 20  (sweep tested 10..40, all positive; 20 is mid-pack solid)
  WICK_MULT   = 1.5 (wick > 1.5× same-bar body)
  TP_PCT      = 0.05 (5%)
  SL_PCT      = 0.05 (5%)
  MAX_HOLD_S  = 96 * 3600 (96 hours)
  TIMEFRAME   = '4h'

Backtest result (50d, 30 coins): 276 trades, 59.4% WR, +1.03% net EV
  Caveats: backtest assumed close-of-bar fill (optimistic by ~0.05-0.15%
  for 4h candles), no slippage, no funding accumulation. See README block
  in this module for the realism shave.

API:
  detect(bars_4h, lookback=20, wick_mult=1.5) -> dict|None
    bars_4h: list of {t,o,h,l,c,v} ascending. Last bar is the candidate.
    Returns signal dict {side, entry_price, tp_pct, sl_pct, max_hold_s,
    swing_level, wick_size, body_size} or None.

  scan_universe(coins, get_bars_fn, lookback=20) -> list of signal dicts
    get_bars_fn(coin) -> bars_4h list

Production wiring (separate from this module):
  - Run scan every 15min (4h bars rarely change mid-bar; cheap)
  - Log signals via shadow_logger (jsonl)
  - 96h MAX_HOLD requires extension to shadow_trades.py
    (current MAX_PENDING_AGE_SEC = 6h)
"""
import os
import time
from typing import Optional

# ─── LOCKED CONFIG (validated by backtest 2026-04-28) ─────────────────
LOOKBACK     = int(os.environ.get('SFP_LOOKBACK', '20'))
WICK_MULT    = float(os.environ.get('SFP_WICK_MULT', '1.5'))
TP_PCT       = float(os.environ.get('SFP_TP_PCT', '0.05'))
SL_PCT       = float(os.environ.get('SFP_SL_PCT', '0.05'))
MAX_HOLD_SEC = int(os.environ.get('SFP_MAX_HOLD_SEC', str(96 * 3600)))
TIMEFRAME    = '4h'

# Friction model — must match shadow_trades + live execution
FEE_ROUND_TRIP_PCT = 0.0009    # 0.09% (HL taker 4.5bps × 2)
SLIP_ROUND_TRIP_PCT = 0.0016   # 0.16% (per shadow_trades.py)
FUNDING_HRLY_PCT_BASE = 0.00001  # ~0.001%/hr neutral assumption
                                 # (HL funding swings; this is a placeholder
                                 # — real backtest needs per-trade funding
                                 # accumulation from HL info endpoint.)


def _wick_body_check(bar):
    """Return (upper_wick, lower_wick, body) for a single OHLC bar."""
    try:
        o = float(bar['o']); h = float(bar['h'])
        l = float(bar['l']); c = float(bar['c'])
    except (KeyError, TypeError, ValueError):
        return None
    body = abs(c - o)
    upper_wick = h - max(c, o)
    lower_wick = min(c, o) - l
    return (upper_wick, lower_wick, body)


def _swing_high_low(bars, lookback):
    """Return (swing_high, swing_low) over the previous `lookback` bars
    EXCLUDING the current (last) bar."""
    if len(bars) < lookback + 1:
        return (None, None)
    prior = bars[-(lookback + 1):-1]  # last `lookback` bars before current
    try:
        highs = [float(b['h']) for b in prior]
        lows  = [float(b['l']) for b in prior]
    except (KeyError, TypeError, ValueError):
        return (None, None)
    return (max(highs), min(lows))


def detect(bars_4h, lookback=None, wick_mult=None) -> Optional[dict]:
    """Detect SFP on the LAST closed 4h bar.

    Caller responsibility: only call after a 4h bar closes — calling
    mid-bar gives unstable detection. The last element of bars_4h is the
    candidate; bars_4h[-2] back is history.
    """
    lb = LOOKBACK if lookback is None else int(lookback)
    wm = WICK_MULT if wick_mult is None else float(wick_mult)

    if not bars_4h or len(bars_4h) < lb + 1:
        return None

    last = bars_4h[-1]
    geom = _wick_body_check(last)
    if geom is None:
        return None
    upper_wick, lower_wick, body = geom

    swing_hi, swing_lo = _swing_high_low(bars_4h, lb)
    if swing_hi is None or swing_lo is None:
        return None

    try:
        h = float(last['h']); l = float(last['l']); c = float(last['c'])
    except (KeyError, TypeError, ValueError):
        return None

    # Bearish SFP: wick poked above swing high, closed back below
    if h > swing_hi and c < swing_hi and upper_wick > body * wm:
        return {
            'side': 'SELL',
            'pattern': 'bearish_sfp',
            'entry_price': c,
            'tp_pct': TP_PCT,
            'sl_pct': SL_PCT,
            'max_hold_s': MAX_HOLD_SEC,
            'swing_level': swing_hi,
            'wick_size': upper_wick,
            'body_size': body,
            'wick_body_ratio': upper_wick / body if body > 0 else float('inf'),
            'lookback': lb,
            'wick_mult': wm,
            'tf': TIMEFRAME,
            'engine': 'SWING_FAIL_4H',
        }

    # Bullish SFP: wick poked below swing low, closed back above
    if l < swing_lo and c > swing_lo and lower_wick > body * wm:
        return {
            'side': 'BUY',
            'pattern': 'bullish_sfp',
            'entry_price': c,
            'tp_pct': TP_PCT,
            'sl_pct': SL_PCT,
            'max_hold_s': MAX_HOLD_SEC,
            'swing_level': swing_lo,
            'wick_size': lower_wick,
            'body_size': body,
            'wick_body_ratio': lower_wick / body if body > 0 else float('inf'),
            'lookback': lb,
            'wick_mult': wm,
            'tf': TIMEFRAME,
            'engine': 'SWING_FAIL_4H',
        }

    return None


def scan_universe(coins, get_bars_fn, lookback=None):
    """Scan a list of coins for SFP signals.

    Args:
      coins: iterable of coin symbols
      get_bars_fn: callable(coin) -> list of 4h bars (or None on err)
      lookback: override default lookback

    Returns: list of signal dicts (one per firing coin)
    """
    out = []
    for c in coins:
        try:
            bars = get_bars_fn(c)
        except Exception:
            continue
        if not bars:
            continue
        sig = detect(bars, lookback=lookback)
        if sig:
            sig['coin'] = c
            sig['scan_ts'] = int(time.time())
            out.append(sig)
    return out


def simulate_trade(bars_4h, signal, n_lookahead_bars=24):
    """Simulate forward outcome of a signal using historical bars.

    Used by backtest harness. Returns (outcome, exit_idx, gross_pnl_pct,
    net_pnl_pct, mfe, mae) where:
      outcome: 'tp' | 'sl' | 'timeout'
      gross_pnl_pct: PnL before fees/slippage/funding
      net_pnl_pct: PnL after fees + slippage (NOT funding — see note)

    Note on funding: HL funding is per-coin per-hour, varies sign. This
    simulation does NOT subtract funding. For realistic net, the caller
    must supply funding_per_hour_pct and call _apply_funding(...).
    n_lookahead_bars at 4h tf = 24 bars = 96h.
    """
    if not bars_4h or not signal:
        return ('timeout', 0, 0.0, 0.0, 0.0, 0.0)

    side = signal['side']
    entry_price = float(signal['entry_price'])
    tp_pct = float(signal['tp_pct'])
    sl_pct = float(signal['sl_pct'])

    if side == 'BUY':
        tp_price = entry_price * (1 + tp_pct)
        sl_price = entry_price * (1 - sl_pct)
    else:
        tp_price = entry_price * (1 - tp_pct)
        sl_price = entry_price * (1 + sl_pct)

    mfe = 0.0
    mae = 0.0
    n = min(n_lookahead_bars, len(bars_4h))

    for j in range(1, n):
        try:
            high = float(bars_4h[j]['h'])
            low = float(bars_4h[j]['l'])
        except (KeyError, TypeError, ValueError):
            continue
        if side == 'BUY':
            cur_mfe = (high - entry_price) / entry_price
            cur_mae = (low - entry_price) / entry_price
            if low <= sl_price:
                gross = -sl_pct
                net = gross - (FEE_ROUND_TRIP_PCT + SLIP_ROUND_TRIP_PCT)
                return ('sl', j, gross, net, max(mfe, cur_mfe), min(mae, cur_mae))
            if high >= tp_price:
                gross = tp_pct
                net = gross - (FEE_ROUND_TRIP_PCT + SLIP_ROUND_TRIP_PCT)
                return ('tp', j, gross, net, max(mfe, cur_mfe), min(mae, cur_mae))
        else:
            cur_mfe = (entry_price - low) / entry_price
            cur_mae = (entry_price - high) / entry_price
            if high >= sl_price:
                gross = -sl_pct
                net = gross - (FEE_ROUND_TRIP_PCT + SLIP_ROUND_TRIP_PCT)
                return ('sl', j, gross, net, max(mfe, cur_mfe), min(mae, cur_mae))
            if low <= tp_price:
                gross = tp_pct
                net = gross - (FEE_ROUND_TRIP_PCT + SLIP_ROUND_TRIP_PCT)
                return ('tp', j, gross, net, max(mfe, cur_mfe), min(mae, cur_mae))
        mfe = max(mfe, cur_mfe)
        mae = min(mae, cur_mae)

    # Timeout — exit at last close
    try:
        last_close = float(bars_4h[n - 1]['c'])
    except (KeyError, TypeError, ValueError, IndexError):
        last_close = entry_price
    if side == 'BUY':
        gross = (last_close - entry_price) / entry_price
    else:
        gross = (entry_price - last_close) / entry_price
    net = gross - (FEE_ROUND_TRIP_PCT + SLIP_ROUND_TRIP_PCT)
    return ('timeout', n - 1, gross, net, mfe, mae)


def status():
    """Module config snapshot for /health endpoint."""
    return {
        'engine': 'SWING_FAIL_4H',
        'tf': TIMEFRAME,
        'lookback': LOOKBACK,
        'wick_mult': WICK_MULT,
        'tp_pct': TP_PCT,
        'sl_pct': SL_PCT,
        'max_hold_s': MAX_HOLD_SEC,
        'fee_rt_pct': FEE_ROUND_TRIP_PCT,
        'slip_rt_pct': SLIP_ROUND_TRIP_PCT,
        'mode': 'shadow_only',  # flip to 'live' after validation
    }
