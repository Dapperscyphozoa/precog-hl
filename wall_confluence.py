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


def clear_path_mult(coin, side, entry_price, tp_pct, boost=1.5):
    """Return (multiplier, detail) for 'clear run' sizing boost.

    Checks whether any VERIFIED wall (≥$500k, persistent 5+ min) sits between
    entry and TP target. If the path is clear, boost the trade. If a wall
    blocks the path, neutral (1.0x) — the trade may still win but the wall is
    a structural barrier that caps expected realization.

    For BUY:  path is UP (entry → entry × (1 + tp_pct)). Check 'ask' walls.
    For SELL: path is DOWN (entry → entry × (1 - tp_pct)). Check 'bid' walls.

    If the coin has no orderbook data at all (not subscribed / thin book),
    return 1.0 (no boost, no penalty — insufficient information).

    Args:
      boost: multiplier applied when path is clear (default 1.5x)

    Returns:
      (multiplier: float, detail: dict)
    """
    try:
        # Verify we actually have orderbook data for this coin
        with orderbook_ws._LOCK:
            depth = orderbook_ws._DEPTH.get(coin)
            has_data = bool(depth and depth.get('mid'))
        if not has_data:
            return 1.0, {'reason': 'no_orderbook_data'}

        wall_side = 'ask' if side == 'BUY' else 'bid'
        walls = orderbook_ws.get_walls(coin)
        path_walls = [w for w in walls if w.get('side') == wall_side]

        if side == 'BUY':
            target_px = entry_price * (1 + tp_pct)
            blockers = [w for w in path_walls if entry_price < w['price'] <= target_px]
        else:  # SELL
            target_px = entry_price * (1 - tp_pct)
            blockers = [w for w in path_walls if target_px <= w['price'] < entry_price]

        if blockers:
            nearest = min(blockers, key=lambda w: abs(w['price'] - entry_price))
            dist_pct = abs(nearest['price'] - entry_price) / entry_price * 100
            return 1.0, {
                'reason': 'wall_in_path',
                'wall_px': round(nearest['price'], 6),
                'wall_usd': int(nearest['usd']),
                'dist_pct': round(dist_pct, 2),
                'target_px': round(target_px, 6),
            }

        # Path is clear — count walls considered (for confidence reporting)
        return boost, {
            'reason': 'clear_path',
            'target_px': round(target_px, 6),
            'walls_on_side_considered': len(path_walls),
            'boost': boost,
        }
    except Exception as e:
        return 1.0, {'err': str(e)}


def wall_pressure(coin, mid_price, band_pct=0.02):
    """Returns -1 to +1: aggregated bid-ask imbalance within ±band_pct of mid.
    Positive = ask-heavy (resistance dominant, bearish pressure).
    Negative = bid-heavy (support dominant, bullish pressure).
    """
    try:
        with orderbook_ws._LOCK:
            d = orderbook_ws._DEPTH.get(coin)
            if not d or not d.get('mid'): return 0
            bids = list(d['bids'].values())
            asks = list(d['asks'].values())
        total_bid = sum(px*sz for px,sz in bids if abs(px-mid_price)/mid_price < band_pct)
        total_ask = sum(px*sz for px,sz in asks if abs(px-mid_price)/mid_price < band_pct)
        total = total_bid + total_ask
        if total < 100000: return 0  # too thin to trust
        return (total_ask - total_bid) / total
    except Exception:
        return 0

def composite_boost(coin, side, entry_price, news_direction):
    """News + orderbook confluence.
    side=BUY: bullish view. news_direction>0 = bullish. wall_pressure<0 = bid-heavy = support.
    Aligned: news bull + walls bull + BUY = 2x. Contradicted: 0.5x. Mixed: 1.0x.
    """
    pressure = wall_pressure(coin, entry_price)
    # Normalize: side_view = +1 for BUY, -1 for SELL
    side_view = 1 if side == 'BUY' else -1
    # Wall view: negative pressure (bid-heavy) favors BUY, positive (ask-heavy) favors SELL
    wall_view = -pressure  # flip sign: -pressure aligns with bullish view
    # News view already signed: +1 bullish, -1 bearish
    news_view = news_direction
    # Composite alignment: how well all three agree
    alignment = side_view * (wall_view + news_view) / 2  # -1 to +1
    if alignment > 0.5: return 2.0   # strong confluence
    if alignment > 0.2: return 1.4
    if alignment > -0.2: return 1.0  # neutral
    if alignment > -0.5: return 0.7  # mild conflict
    return 0.4                        # strong conflict: news+walls both against trade
