"""Wall-context router. Pure function under test.

Mental model — for any trade direction:
  same-side wall  = resistance opposing the trade (BUY: ask above, SELL: bid below)
                    → blocker if too close, TP target if at reasonable distance
  opposite-side wall = support helping the trade (BUY: bid below, SELL: ask above)
                    → SL anchor (place SL just beyond it; wall failing = exit signal)

Four levers (council):
  1. side-aware:        only same-side walls block (was: any wall blocked both directions)
  2. approach-aware:    only block if price moving TOWARD wall (not bouncing off it)
  3. persistence-aware: wall must be present ≥N sec (filters transient spoofs)
  4. size-modulating:   sandwich regime = reduce size, not binary block
"""
from typing import Optional, Literal, List, Tuple


def route(
    coin: str,
    side: Literal['BUY', 'SELL'],
    entry_px: float,
    walls: List[dict],                          # {side: 'bid'|'ask', price, usd, persistence_sec}
    recent_px_trajectory: Optional[List[Tuple[float, float]]] = None,  # [(ts, px), ...]
    # ---------- TUNABLES (council mandate: every threshold is a parameter) ----------
    block_proximity_pct: float = 0.003,         # 0.3% — same-side wall this close = blocker
    target_proximity_pct: float = 0.020,        # 2.0% — same-side wall this close = TP target
    sl_anchor_proximity_pct: float = 0.010,     # 1.0% — opposite-side wall this close = SL anchor
    min_wall_usd: float = 500_000,
    min_persistence_sec: float = 120,           # "verified" threshold
    entrenched_persistence_sec: float = 600,    # "entrenched" threshold → full veto
    sandwich_size_mult: float = 0.5,
    approach_lookback_sec: int = 60,
    approach_eps_pct: float = 0.0005,
    require_approach_to_block: bool = True,
) -> dict:
    """Returns: {action: ALLOW|BLOCK|MODIFY, reason, suggested_sl_px, suggested_tp_px, size_mult}."""
    # ----- 1. Filter walls by persistence + size -----
    eligible = [
        w for w in walls
        if w.get('usd', 0) >= min_wall_usd
        and w.get('persistence_sec', 0) >= min_persistence_sec
    ]

    # ----- 2. Classify walls relative to this trade -----
    # same-side = opposes trade direction (BUY: ask above; SELL: bid below)
    # opp-side  = supports trade direction (BUY: bid below; SELL: ask above)
    if side == 'BUY':
        same_side = [w for w in eligible if w.get('side') == 'ask' and w.get('price', 0) > entry_px]
        opp_side  = [w for w in eligible if w.get('side') == 'bid' and w.get('price', 0) < entry_px]
    else:  # SELL
        same_side = [w for w in eligible if w.get('side') == 'bid' and w.get('price', 0) < entry_px]
        opp_side  = [w for w in eligible if w.get('side') == 'ask' and w.get('price', 0) > entry_px]

    nearest_blocker = min(same_side, key=lambda w: abs(w['price'] - entry_px)) if same_side else None
    nearest_blocker_dist = abs(nearest_blocker['price'] - entry_px) / entry_px if nearest_blocker else None

    nearest_support = min(opp_side, key=lambda w: abs(w['price'] - entry_px)) if opp_side else None
    nearest_support_dist = abs(nearest_support['price'] - entry_px) / entry_px if nearest_support else None

    # ----- 3. Approach direction (lever 2) -----
    approaching = True  # default conservative: assume worst
    if require_approach_to_block and nearest_blocker and recent_px_trajectory:
        latest_ts = recent_px_trajectory[-1][0]
        cutoff = latest_ts - approach_lookback_sec
        window = [p for p in recent_px_trajectory if p[0] >= cutoff]
        if len(window) >= 2:
            px0, pxN = window[0][1], window[-1][1]
            move = (pxN - px0) / px0
            # Approaching = moving toward blocker.
            # BUY's blocker (ask) is above → approaching means px going UP → move > 0
            # SELL's blocker (bid) is below → approaching means px going DOWN → move < 0
            approaching = (move > approach_eps_pct) if side == 'BUY' else (move < -approach_eps_pct)

    # ----- 4. Sandwich detection: blocker AND support both close -----
    sandwiched = (
        nearest_blocker_dist is not None and nearest_blocker_dist <= block_proximity_pct and
        nearest_support_dist is not None and nearest_support_dist <= block_proximity_pct
    )

    # ----- 5. DECISION TREE -----
    # (a) Entrenched same-side wall, very close, price approaching → hard BLOCK
    if (nearest_blocker
        and nearest_blocker.get('persistence_sec', 0) >= entrenched_persistence_sec
        and nearest_blocker_dist <= block_proximity_pct
        and approaching):
        return {
            'action': 'BLOCK',
            'reason': f'entrenched_{nearest_blocker["side"]}_wall_{nearest_blocker_dist*100:.3f}%_${nearest_blocker["usd"]/1e6:.1f}M_p{int(nearest_blocker["persistence_sec"])}s',
            'suggested_sl_px': None,
            'suggested_tp_px': None,
            'size_mult': 0.0,
        }

    # (b) Sandwich: range-fade regime → size cut + use both walls as SL/TP anchors
    if sandwiched:
        if side == 'BUY':
            tp = nearest_blocker['price'] * 0.999  # just below resistance
            sl = nearest_support['price'] * 0.999  # just below support
        else:
            tp = nearest_blocker['price'] * 1.001  # just above support
            sl = nearest_support['price'] * 1.001  # just above resistance
        return {
            'action': 'MODIFY',
            'reason': f'sandwich_blk_{nearest_blocker_dist*100:.3f}%_sup_{nearest_support_dist*100:.3f}%',
            'suggested_sl_px': sl,
            'suggested_tp_px': tp,
            'size_mult': sandwich_size_mult,
        }

    # (c) Same-side wall at TP-target distance → set TP just before it
    suggested_tp = None
    if (nearest_blocker
        and block_proximity_pct < nearest_blocker_dist <= target_proximity_pct):
        suggested_tp = nearest_blocker['price'] * (0.999 if side == 'BUY' else 1.001)

    # (d) Opposite-side wall at SL-anchor distance → tighten SL behind it
    suggested_sl = None
    if (nearest_support
        and nearest_support_dist <= sl_anchor_proximity_pct):
        suggested_sl = nearest_support['price'] * (0.999 if side == 'BUY' else 1.001)

    if suggested_tp is not None or suggested_sl is not None:
        bits = []
        if suggested_tp is not None:
            bits.append(f'tp_at_{nearest_blocker["side"]}_${nearest_blocker["usd"]/1e6:.1f}M')
        if suggested_sl is not None:
            bits.append(f'sl_behind_{nearest_support["side"]}_${nearest_support["usd"]/1e6:.1f}M')
        return {
            'action': 'MODIFY',
            'reason': '+'.join(bits),
            'suggested_sl_px': suggested_sl,
            'suggested_tp_px': suggested_tp,
            'size_mult': 1.0,
        }

    # (e) Soft warn: blocker close but not entrenched, or not approaching → size cut
    if nearest_blocker and nearest_blocker_dist <= block_proximity_pct:
        return {
            'action': 'MODIFY',
            'reason': f'near_{nearest_blocker["side"]}_wall_{nearest_blocker_dist*100:.3f}%_not_entrenched_or_not_approaching',
            'suggested_sl_px': None,
            'suggested_tp_px': None,
            'size_mult': 0.5,
        }

    # (f) Default: clear field
    return {
        'action': 'ALLOW',
        'reason': 'no_qualifying_walls',
        'suggested_sl_px': None,
        'suggested_tp_px': None,
        'size_mult': 1.0,
    }
