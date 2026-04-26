"""Time-weighted funding cost accrual for closed positions.

HL pays/charges funding HOURLY based on the rate at the top of each hour. A
position held from t_open to t_close pays funding on every hour boundary it
crosses, weighted by the rate active at that hour.

Sign convention (HL):
  rate > 0  => longs pay shorts
  rate < 0  => shorts pay longs

This module returns `funding_paid_pct` from the perspective of the position:
  positive = position PAID funding (cost — subtract from PnL)
  negative = position RECEIVED funding (credit — add to PnL)

Two paths:
  1. Best-effort: query HL fundingHistory for the held interval. Accurate.
  2. Fallback: use the current rate as a single-point estimate, time-weighted
     by hours held. Per the audit: "rough version is better than zero".

Net pct returned is on NOTIONAL (i.e. apply to entry_notional_usd to get USD).
"""
import os
import json
import time
import urllib.request

HL_INFO_URL = os.environ.get('HL_INFO_URL', 'https://api.hyperliquid.xyz/info')
_TIMEOUT = float(os.environ.get('FUNDING_ACCRUAL_TIMEOUT', 8.0))


def _hl_funding_history(coin, start_time_ms, end_time_ms=None):
    """Pull HL's funding history for `coin` over [start, end]. Returns list of
    {'time': ms, 'fundingRate': str|float}. Empty list on any failure.
    """
    body = {'type': 'fundingHistory', 'coin': coin, 'startTime': int(start_time_ms)}
    if end_time_ms is not None:
        body['endTime'] = int(end_time_ms)
    try:
        req = urllib.request.Request(
            HL_INFO_URL,
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'},
        )
        r = json.loads(urllib.request.urlopen(req, timeout=_TIMEOUT).read())
        return r if isinstance(r, list) else []
    except Exception:
        return []


def _side_sign(side):
    """Return +1 for long-equivalent, -1 for short-equivalent. Anything else: 0.
    Position 'paid' (pos_pct > 0) when (rate * side_sign) > 0 — long pays positive rate."""
    if side is None:
        return 0
    s = str(side).strip().upper()
    if s in ('BUY', 'LONG', 'L'):
        return 1
    if s in ('SELL', 'SHORT', 'S'):
        return -1
    return 0


def compute_funding_paid_pct(coin, side, entry_ts, close_ts):
    """Return the position's net funding paid as a fraction of notional.

    coin: HL coin symbol (e.g. 'BTC')
    side: 'BUY'/'LONG'/'L' or 'SELL'/'SHORT'/'S'
    entry_ts, close_ts: unix seconds (float ok)

    Returns (funding_paid_pct, source) where:
      funding_paid_pct > 0  => position paid (cost; subtract from PnL)
      funding_paid_pct < 0  => position received (credit)
      source: 'history' | 'estimate' | 'unknown'

    Never raises — all errors collapse to (0.0, 'unknown') so a fee-tracking
    failure cannot break the close path.
    """
    if not coin or entry_ts is None or close_ts is None:
        return 0.0, 'unknown'
    try:
        entry_ts = float(entry_ts)
        close_ts = float(close_ts)
    except (TypeError, ValueError):
        return 0.0, 'unknown'
    if close_ts <= entry_ts:
        return 0.0, 'unknown'

    sign = _side_sign(side)
    if sign == 0:
        return 0.0, 'unknown'

    start_ms = int(entry_ts * 1000)
    end_ms = int(close_ts * 1000)

    # Path 1: historical funding events
    events = _hl_funding_history(coin, start_ms, end_ms)
    if events:
        total = 0.0
        for ev in events:
            t_ms = ev.get('time')
            rate_raw = ev.get('fundingRate')
            if t_ms is None or rate_raw is None:
                continue
            try:
                t_ms = int(t_ms)
                rate = float(rate_raw)
            except (TypeError, ValueError):
                continue
            if t_ms < start_ms or t_ms > end_ms:
                continue
            # Each event is one hour's funding, charged on notional.
            # Long pays when rate>0; sign flips for short.
            total += rate * sign
        return total, 'history'

    # Path 2: estimate from current rate, time-weighted across hold
    try:
        from funding_arb import get_hl_funding_rate
        cur_rate = float(get_hl_funding_rate(coin) or 0.0)
    except Exception:
        cur_rate = 0.0

    if cur_rate == 0.0:
        return 0.0, 'unknown'

    hours_held = (close_ts - entry_ts) / 3600.0
    estimate = cur_rate * sign * hours_held
    return estimate, 'estimate'


def compute_funding_paid_usd(coin, side, entry_ts, close_ts, notional_usd):
    """USD form of compute_funding_paid_pct. Convenience helper."""
    pct, source = compute_funding_paid_pct(coin, side, entry_ts, close_ts)
    if not notional_usd:
        return 0.0, source
    try:
        return pct * float(notional_usd), source
    except (TypeError, ValueError):
        return 0.0, source
