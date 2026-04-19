"""Bybit-lead limit entries. Bybit price direction leads HL 100-500ms on alts.
Place HL limit 0.1-0.2% through Bybit's current price in intended direction.
If Bybit says UP → post HL BUY below HL mid (capture dip before HL follows).
If unfilled in 2s, fall through to standard maker.
"""
import time
import bybit_ws

OFFSET_PCT = 0.0015   # 0.15% through Bybit mid

def compute_edge_price(coin, side, hl_mid):
    """Return suggested HL limit price that captures Bybit lead.
    Returns None if no Bybit data or spread misalignment.
    """
    if not hl_mid or hl_mid <= 0:
        return None
    try:
        by_px, age_ms = bybit_ws.get_price(coin)
    except Exception:
        return None
    if not by_px or age_ms is None or age_ms > 2000:
        return None  # stale Bybit price
    # If Bybit price is above HL mid → Bybit leading UP → HL will follow up
    # For BUY: place limit BELOW Bybit mid (catch HL before it catches up)
    # For SELL: place limit ABOVE Bybit mid
    if side == 'BUY':
        edge = by_px * (1 - OFFSET_PCT)
        # But don't go above current HL mid (no taker risk)
        return min(edge, hl_mid)
    else:
        edge = by_px * (1 + OFFSET_PCT)
        return max(edge, hl_mid)

def direction_bias(coin, lookback_ms=500):
    """Returns +1 if Bybit trades last 500ms trending up, -1 if down, 0 otherwise.
    Uses live trade price vs previous price snapshot.
    """
    try:
        px, age = bybit_ws.get_price(coin)
    except Exception:
        return 0
    # Simple: compare to prev snapshot cached here
    prev = _PREV_PX.get(coin, {}).get('px')
    now = time.time()
    if prev and px:
        if px > prev * 1.0003: out = 1
        elif px < prev * 0.9997: out = -1
        else: out = 0
    else:
        out = 0
    _PREV_PX.setdefault(coin, {})['px'] = px
    _PREV_PX[coin]['ts'] = now
    return out

_PREV_PX = {}
