"""trend_v9.py — Structural trend bias from 1h pivot sequence.

Definition (per the manual):
  HH/HL/HH/HL... = uptrend (last 3 pivot highs ascending AND last 3 pivot lows ascending)
  LH/LL/LH/LL... = downtrend (last 3 pivot highs descending AND last 3 pivot lows descending)
  Anything else = neutral

This is structure, not EMA. A flat range can have rising HHs and rising HLs
(uptrend), or descending LHs/LLs (downtrend) without significant net price
movement. The structure says what the market is doing; the slope says how fast.

Used as a multiplier on min_rr, not a hard veto:
  with-trend setup    -> min_rr unchanged (1.5 / 1.5)
  counter-trend setup -> min_rr × 1.7 (effectively 2.55 / 2.55)
  neutral             -> min_rr unchanged
"""
import time
from typing import Dict, List, Optional, Tuple

# Cache: coin -> (computed_at_ts, bias_str)
_CACHE: Dict[str, Tuple[float, str]] = {}
CACHE_S = 300  # 1h trend rarely flips in 5 min


def _pivot_highs(bars: List[dict], lb: int = 5) -> List[Tuple[int, float]]:
    """Return [(idx, high)] where bars[idx]['h'] is the highest in [idx-lb, idx+lb]."""
    out = []
    for i in range(lb, len(bars) - lb):
        h = bars[i]['h']
        window = bars[i - lb: i + lb + 1]
        if h == max(b['h'] for b in window):
            out.append((i, h))
    return out


def _pivot_lows(bars: List[dict], lb: int = 5) -> List[Tuple[int, float]]:
    out = []
    for i in range(lb, len(bars) - lb):
        l = bars[i]['l']
        window = bars[i - lb: i + lb + 1]
        if l == min(b['l'] for b in window):
            out.append((i, l))
    return out


def classify(bars: List[dict], lb: int = 5) -> str:
    """Return 'up' | 'down' | 'neutral' from last 3 pivot highs + 3 pivot lows.

    bars: list of {'h','l','o','c','t'} dicts, oldest -> newest.
    lb: pivot lookback (5 = standard structural).

    Up:   last 3 pivot highs strictly ascending AND last 3 pivot lows strictly ascending
    Down: last 3 pivot highs strictly descending AND last 3 pivot lows strictly descending
    Else: neutral.
    """
    if len(bars) < lb * 2 + 6:
        return 'neutral'
    ph = _pivot_highs(bars, lb)
    pl = _pivot_lows(bars, lb)
    if len(ph) < 3 or len(pl) < 3:
        return 'neutral'
    h3 = [p[1] for p in ph[-3:]]
    l3 = [p[1] for p in pl[-3:]]
    asc_h = h3[0] < h3[1] < h3[2]
    asc_l = l3[0] < l3[1] < l3[2]
    desc_h = h3[0] > h3[1] > h3[2]
    desc_l = l3[0] > l3[1] > l3[2]
    if asc_h and asc_l: return 'up'
    if desc_h and desc_l: return 'down'
    return 'neutral'


def get_bias(coin: str, fetch_1h_bars) -> str:
    """Cached bias lookup. fetch_1h_bars is callable(coin) -> List[bar]."""
    now = time.time()
    cached = _CACHE.get(coin)
    if cached and now - cached[0] < CACHE_S:
        return cached[1]
    try:
        bars = fetch_1h_bars(coin)
        bias = classify(bars) if bars else 'neutral'
    except Exception:
        bias = 'neutral'
    _CACHE[coin] = (now, bias)
    return bias


def rr_multiplier(side: str, bias: str) -> float:
    """DEPRECATED. Returns 1.0 always — trend now affects size, not RR.
    Kept for back-compat with any callers. Use size_multiplier instead."""
    return 1.0


def size_multiplier(side: str, bias: str) -> float:
    """Position size multiplier based on trend alignment.

    With-trend or neutral: full size (1.0).
    Counter-trend: half size (0.5) — preserves fire frequency, halves exposure
    on lower-conviction setups.
    """
    if bias == 'neutral':
        return 1.0
    with_trend = (side == 'BUY' and bias == 'up') or (side == 'SELL' and bias == 'down')
    return 1.0 if with_trend else 0.5
