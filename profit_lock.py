"""Position-level profit take: if unrealized PnL hits +1.5%, move SL to +0.7% (lock profit).
Called each tick for open positions. Returns new SL price or None.
"""

PROFIT_TRIGGER = 0.015  # +1.5% unrealized
LOCK_LEVEL = 0.007      # lock at +0.7%

def compute_new_sl(entry_price, current_price, side, current_sl):
    """Returns new SL price if profit-lock should engage, else None."""
    if side == 'BUY':
        pnl = (current_price - entry_price) / entry_price
        if pnl >= PROFIT_TRIGGER:
            new_sl = entry_price * (1 + LOCK_LEVEL)
            if current_sl is None or new_sl > current_sl:
                return new_sl
    else:
        pnl = (entry_price - current_price) / entry_price
        if pnl >= PROFIT_TRIGGER:
            new_sl = entry_price * (1 - LOCK_LEVEL)
            if current_sl is None or new_sl < current_sl:
                return new_sl
    return None
