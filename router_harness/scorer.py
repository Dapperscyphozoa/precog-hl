"""Outcome scoring: given a signal at time T and price P, fetch what actually
happened to the coin's price in the next 30/60/120m via HL 1m candles.

For each (signal, decision) pair we compute:
  - max_favorable: best price for our side within window
  - max_adverse:   worst price for our side within window
  - hit_sl: did adverse exceed suggested SL (if any)
  - hit_tp: did favorable reach suggested TP (if any)
  - net_at_30/60/120m: simple close - entry P&L

Outcome stays neutral to router decision — same outcome is scored against
different decisions (BLOCK = 0, ALLOW = full move, MODIFY = clipped at SL/TP).
"""
import json
import time
import urllib.request
from typing import Optional, List, Tuple, Literal


def fetch_candles_1m(coin: str, start_ms: int, end_ms: int) -> List[dict]:
    """HL 1m candles. Returns list of {t, o, h, l, c, v}."""
    body = json.dumps({
        'type': 'candleSnapshot',
        'req': {'coin': coin, 'interval': '1m', 'startTime': start_ms, 'endTime': end_ms}
    }).encode()
    req = urllib.request.Request(
        'https://api.hyperliquid.xyz/info', data=body,
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        return json.load(urllib.request.urlopen(req, timeout=15))
    except Exception:
        return []


def score_signal(coin: str, side: Literal['BUY', 'SELL'], entry_px: float,
                 ts_signal: float,
                 horizons_min: Tuple[int, ...] = (30, 60, 120)) -> dict:
    """Compute max_fav / max_adv at each horizon."""
    end_ms = int((ts_signal + max(horizons_min) * 60 + 30) * 1000)
    start_ms = int((ts_signal - 60) * 1000)
    candles = fetch_candles_1m(coin, start_ms, end_ms)
    # Only consider candles AFTER signal
    post = [c for c in candles if int(c['t']) >= ts_signal * 1000 - 1000]
    if not post:
        return {'err': 'no_candles', 'coin': coin, 'ts': ts_signal}

    out = {'coin': coin, 'side': side, 'entry_px': entry_px, 'ts_signal': ts_signal}
    sig_ms = int(ts_signal * 1000)
    for h in horizons_min:
        horizon_ms = sig_ms + h * 60 * 1000
        window = [c for c in post if int(c['t']) <= horizon_ms]
        if not window:
            continue
        max_high = max(float(c['h']) for c in window)
        min_low = min(float(c['l']) for c in window)
        if side == 'BUY':
            max_fav_pct = (max_high - entry_px) / entry_px * 100
            max_adv_pct = (entry_px - min_low) / entry_px * 100
        else:  # SELL
            max_fav_pct = (entry_px - min_low) / entry_px * 100
            max_adv_pct = (max_high - entry_px) / entry_px * 100
        out[f'fav_{h}m_pct'] = round(max_fav_pct, 4)
        out[f'adv_{h}m_pct'] = round(max_adv_pct, 4)
        # net at horizon = close of last candle in window
        out[f'net_{h}m_pct'] = round(
            (float(window[-1]['c']) - entry_px) / entry_px * 100 * (1 if side == 'BUY' else -1), 4)
    return out


def attribute_decision(score: dict, decision: dict, default_sl_pct: float = 0.003,
                       default_tp_pct: float = 0.009) -> dict:
    """Given a price outcome + a router decision, compute P&L attribution.

    Returns: {
      'pnl_pct_30m', 'pnl_pct_60m', 'pnl_pct_120m': realized P&L assuming the
        decision's SL/TP were used; uses default 1:3 R:R if router didn't suggest
      'hit_sl': bool, 'hit_tp': bool (at any horizon)
      'size_mult': from decision (applies to all PnLs)
      'action': from decision
    }
    """
    action = decision.get('action', 'ALLOW')
    size_mult = decision.get('size_mult', 1.0)
    sl_px = decision.get('suggested_sl_px')
    tp_px = decision.get('suggested_tp_px')

    entry_px = score['entry_px']
    side = score['side']

    # Build SL/TP as % moves from entry
    if sl_px is not None:
        sl_pct = abs(entry_px - sl_px) / entry_px
    else:
        sl_pct = default_sl_pct
    if tp_px is not None:
        tp_pct = abs(entry_px - tp_px) / entry_px
    else:
        tp_pct = default_tp_pct

    out = {'action': action, 'size_mult': size_mult, 'sl_pct': sl_pct, 'tp_pct': tp_pct}

    if action == 'BLOCK':
        for h in (30, 60, 120):
            out[f'pnl_pct_{h}m'] = 0.0
        out['hit_sl'] = False
        out['hit_tp'] = False
        return out

    for h in (30, 60, 120):
        fav = score.get(f'fav_{h}m_pct', None)
        adv = score.get(f'adv_{h}m_pct', None)
        net = score.get(f'net_{h}m_pct', None)
        if fav is None or adv is None or net is None:
            continue
        # Did TP hit before SL? Naive: if max_fav >= tp_pct*100 AND max_adv < sl_pct*100, TP first.
        # If both, we don't know ordering without intra-candle data → use min-of-both as conservative.
        hit_sl_h = (adv / 100) >= sl_pct
        hit_tp_h = (fav / 100) >= tp_pct
        if hit_tp_h and not hit_sl_h:
            pnl = tp_pct * 100
        elif hit_sl_h and not hit_tp_h:
            pnl = -sl_pct * 100
        elif hit_tp_h and hit_sl_h:
            # Ambiguous — assume SL-first (worst case) for conservative scoring
            pnl = -sl_pct * 100
        else:
            pnl = net  # neither hit, mark at horizon-close
        out[f'pnl_pct_{h}m'] = round(pnl * size_mult, 4)
        if h == 120:
            out['hit_sl'] = hit_sl_h
            out['hit_tp'] = hit_tp_h
    return out
