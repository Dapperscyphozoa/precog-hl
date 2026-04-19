"""Wall confluence boost. Checks if entry price near verified orderbook wall → 3× risk multiplier.
Reads from orderbook_ws.get_nearest_wall. Returns multiplier, not hard gate.
"""
import orderbook_ws

PROXIMITY_PCT = 0.002  # 0.2% — entry within this % of wall to qualify
BOOST_MULT = 3.0       # 3× normal risk when wall confluence present

def risk_boost(coin, side, entry_price):
    """Returns multiplier to apply to base risk. 1.0 = no boost, 3.0 = confluence hit."""
    # For BUY, we want support wall BELOW entry (bid side)
    # For SELL, we want resistance wall ABOVE entry (ask side)
    wall_side = 'bid' if side == 'BUY' else 'ask'
    try:
        wall = orderbook_ws.get_nearest_wall(coin, wall_side)
    except Exception:
        return 1.0
    if not wall or entry_price <= 0: return 1.0
    dist = abs(entry_price - wall['price']) / entry_price
    if dist > PROXIMITY_PCT: return 1.0
    # Scale boost by wall size — larger wall = more confidence
    size_mult = min(1.5, 1.0 + (wall['usd'] - 500000) / 2000000)  # $500k=1.0, $3.5M=1.5
    return BOOST_MULT * size_mult

def wall_context(coin, side, entry_price):
    """Return descriptive context for logging/dashboard."""
    wall_side = 'bid' if side == 'BUY' else 'ask'
    try:
        wall = orderbook_ws.get_nearest_wall(coin, wall_side)
    except Exception:
        return None
    if not wall: return None
    dist = abs(entry_price - wall['price']) / max(entry_price, 1) * 100
    return {'price': wall['price'], 'usd': wall['usd'], 'dist_pct': dist,
            'persistence': wall.get('persistence_windows', 0)}
