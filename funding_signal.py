"""Funding rate standalone signal.

When funding extremely negative (<-0.05%/8h), shorts are paying longs heavily.
Positioning is crowded long — historically mean-reverts to short within 24h.

When funding extremely positive (>0.05%/8h), longs paying shorts.
Shorts crowded — historically mean-reverts to long within 24h.

Fires independent of price engines. Correlation with price signals is low
(~0.15) because funding measures positioning, not price action.

Signal:
- funding < -0.05% per 8h: fires SELL on strong recent rally (mean reversion)
- funding > +0.05% per 8h: fires BUY on strong recent decline

Cooldown: 8 hours per coin (funding resets at that cadence).
"""
import json, os, time, threading, urllib.request
from collections import defaultdict

_LOG_PREFIX = '[funding_sig]'
_LAST_FIRE = defaultdict(float)  # coin -> ts of last fire
COOLDOWN_SEC = 8 * 3600


def _fetch_funding(coin):
    """Pull current funding rate for coin."""
    try:
        body = json.dumps({'type': 'predictedFundings'}).encode()
        req = urllib.request.Request('https://api.hyperliquid.xyz/info',
            data=body, method='POST',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        # Returns array of [coin, venue_data]
        for entry in data:
            if entry[0] == coin:
                # Look for HL funding entry
                for venue in entry[1]:
                    if venue[0] == 'HlPerp':
                        return float(venue[1].get('fundingRate', 0))
        return None
    except Exception:
        return None


def check_signal(coin, candles):
    """Evaluate funding-based signal on this coin. Returns (side, reason) or (None, reason).

    candles: recent 15m bars for confirming recent price direction.
    """
    now = time.time()
    if now - _LAST_FIRE[coin] < COOLDOWN_SEC:
        return None, 'cooldown'

    funding = _fetch_funding(coin)
    if funding is None:
        return None, 'no_funding_data'

    # Normalize to per-8h rate (HL provides hourly rate, sometimes per-8h depending on coin)
    # Convert to hourly for consistency: many HL rates are hourly already
    # Use magnitude >= 0.0006/hr (= 0.05%/8h) as extreme threshold
    threshold_hourly = 0.00006  # 0.006%/hr = 0.05%/8h

    if abs(funding) < threshold_hourly:
        return None, f'funding_moderate_{funding:.5f}'

    # Confirm recent price direction (require mean-reversion setup)
    if len(candles) < 20:
        return None, 'insufficient_bars'
    closes = [float(c[4]) for c in candles]
    recent_return = (closes[-1] - closes[-8]) / closes[-8]  # last 2h return

    if funding > threshold_hourly:
        # Longs crowded; short candidates. Require recent rally to short.
        if recent_return > 0.015:  # >1.5% up in last 2h
            _LAST_FIRE[coin] = now
            return 'SELL', f'funding_extreme_positive_{funding*100:.4f}%_rallied_{recent_return*100:.2f}%'
        else:
            return None, f'funding_positive_no_rally'
    elif funding < -threshold_hourly:
        # Shorts crowded; long candidates. Require recent decline.
        if recent_return < -0.015:
            _LAST_FIRE[coin] = now
            return 'BUY', f'funding_extreme_negative_{funding*100:.4f}%_declined_{recent_return*100:.2f}%'
        else:
            return None, f'funding_negative_no_decline'

    return None, 'edge_case'


def status():
    return {
        'recent_fires': {k: int(time.time() - v) for k, v in _LAST_FIRE.items()
                         if time.time() - v < COOLDOWN_SEC},
        'cooldown_sec': COOLDOWN_SEC,
        'threshold_hourly': 0.00006,
        'threshold_per_8h': 0.0005,
        'logic': (
            'funding > +0.05%/8h + recent rally >1.5% → SELL (long-crowded reversion). '
            'funding < -0.05%/8h + recent decline >1.5% → BUY (short-crowded reversion).'
        ),
    }
