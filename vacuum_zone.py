"""Vacuum zone TP targeting. Identifies price ranges with NO verified walls = fast-moving zones.
When entering a trade, set TP at the start of the next vacuum (where walls end) rather than arbitrary %.
"""
import orderbook_ws

def find_vacuum_tp(coin, side, entry_price, max_distance_pct=0.05):
    """Returns suggested TP price based on nearest wall on the exit side, or None.
    LONG: TP at nearest ask wall (resistance) — wall_confluence already does this as hard TP.
    For vacuum: if no wall within 2%, target next gap (up to 5%) with reduced conviction.
    """
    if not entry_price: return None
    wall_side = 'ask' if side == 'BUY' else 'bid'
    try:
        walls = orderbook_ws.get_walls(coin)
    except Exception:
        return None
    relevant = sorted([w for w in walls if w['side'] == wall_side],
                      key=lambda w: w['distance_pct'])
    if not relevant: return None
    nearest = relevant[0]
    # TP slightly before wall (don't expect fill at exact wall price)
    buffer = 0.001
    if side == 'BUY':
        return nearest['price'] * (1 - buffer)
    else:
        return nearest['price'] * (1 + buffer)

def vacuum_width(coin, side, entry_price):
    """Returns distance (%) from entry to nearest wall. Wider = more room = bigger winner possible."""
    if not entry_price: return None
    wall_side = 'ask' if side == 'BUY' else 'bid'
    try:
        walls = orderbook_ws.get_walls(coin)
    except Exception:
        return None
    relevant = [w for w in walls if w['side'] == wall_side]
    if not relevant: return None
    nearest = min(relevant, key=lambda w: w['distance_pct'])
    return nearest['distance_pct']
