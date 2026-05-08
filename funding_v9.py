"""funding_v9.py — Kill positions where funding has eaten the edge.

Logic:
  position has been held >= MIN_HOLD_S (4h)
  AND cumFunding.sinceOpen > 0 (we are paying, not receiving)
  AND cumFunding.sinceOpen >= unrealizedPnl (funding has consumed any gain)
  -> close at market

This catches positions that are technically "in profit" but where the
accrued funding cost has already wiped that profit. Per the manual,
positions that haven't worked in 4h on the desired timeframe are
structurally invalidated regardless of mark price.

If unrealizedPnl is negative, the SL handles it — we don't need
funding-based exits for losers.
"""
import time
from typing import Optional, Dict, List

MIN_HOLD_S = 4 * 3600  # 4 hours


def positions_to_close(state_positions: Dict[str, dict],
                       hl_asset_positions: List[dict],
                       engine_prefix: str = 'V9',
                       now_ts: Optional[float] = None) -> List[Dict]:
    """Scan open HL positions; return those V9 should close due to funding burn.

    Eligibility:
      - position is in V9 state_positions
      - position has trade_id starting with engine_prefix (skips pre-cloid legacy entries)
      - held >= MIN_HOLD_S
      - cumFunding.sinceOpen > 0 (we are paying)
      - cumFunding.sinceOpen >= unrealizedPnl

    Legacy positions without a V9-prefixed trade_id are skipped — they may
    belong to another engine that shares the wallet.
    """
    if now_ts is None:
        now_ts = time.time()
    out = []
    for ap in hl_asset_positions:
        p = ap.get('position', {})
        coin = p.get('coin')
        if coin not in state_positions:
            continue
        sp = state_positions[coin]
        tid = sp.get('trade_id', '')
        if not tid or not tid.startswith(engine_prefix):
            continue  # not V9-tagged; do not close
        sz = float(p.get('szi', 0))
        if abs(sz) < 1e-9:
            continue
        try:
            unrealized = float(p.get('unrealizedPnl', 0))
            cum_funding = float(p.get('cumFunding', {}).get('sinceOpen', 0))
        except (TypeError, ValueError):
            continue
        opened_t_ms = sp.get('opened_t') or sp.get('filled_t', 0)
        age_s = now_ts - (opened_t_ms / 1000.0) if opened_t_ms else 0
        if age_s < MIN_HOLD_S:
            continue
        if cum_funding <= 0:
            continue
        if cum_funding < unrealized:
            continue
        out.append({
            'coin': coin,
            'size': abs(sz),
            'side': 'BUY' if sz > 0 else 'SELL',
            'reason': f"FUNDING-KILL age={age_s/3600:.1f}h pnl={unrealized:.2f} funding={cum_funding:.2f}",
            'unrealizedPnl': unrealized,
            'cumFunding': cum_funding,
            'age_s': age_s,
        })
    return out
