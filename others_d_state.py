"""OTHERS.D direction filter for alt entry conviction.

Reads alerts from TradingView CRYPTOCAP:OTHERS.D 15m chart that combine:
  1. EMA Cloud trend (fast vs slow + price position)  
  2. 1H % change momentum (>±0.2%)

State 'rising'  = both indicators agree alts gaining share → veto SHORTS
State 'falling' = both indicators agree alts losing share → veto LONGS  
State 'flat'    = no agreement → no veto, signals pass through

Backtested on 26 days of CRYPTOCAP:OTHERS.D 15m + BTC 15m:
  LONG when both agree rising:  86.4% WR (n=22)
  SHORT when both agree falling: 60.0% WR (n=30)
  ~70% of bars are 'flat' — filter only fires on high-conviction states.

Webhook contract:
  POST /webhook with body containing one of:
    "OTHERS_D rising"
    "OTHERS_D falling"
    "OTHERS_D flat"

State staleness: returns 'unknown' if last update > STALE_HOURS (default 4h).
'unknown' fails open — no veto, signals pass through.

API:
  update(direction)  -> None       # called by webhook handler on alert receipt
  get_state()        -> str        # returns 'rising'|'falling'|'flat'|'unknown'
  block_alt_side(coin, side) -> (bool, str)   # gate function for apply_ticker_gate
  status()           -> dict       # for /others_d_status endpoint
"""
import os
import time
import threading

_LOCK = threading.RLock()
_STATE = {
    'direction': 'unknown',  # 'rising'|'falling'|'flat'|'unknown'
    'updated_at': 0.0,
    'source': 'init',
}

ENABLED = os.environ.get('OTHERS_D_GATE_ENABLED', '1') == '1'
STALE_HOURS = float(os.environ.get('OTHERS_D_STALE_HOURS', '4'))
ENGINE_ALLOWLIST = [e.strip() for e in os.environ.get('OTHERS_D_ENGINES', '').split(',') if e.strip()]

# Coins exempt from the gate (BTC and ETH trade on their own logic, not alt direction)
EXEMPT_COINS = {'BTC', 'ETH'}


def update(direction, source='webhook'):
    """Called by webhook handler when OTHERS_D alert received.
    direction must be one of: 'rising', 'falling', 'flat'
    """
    direction = (direction or '').strip().lower()
    if direction not in ('rising', 'falling', 'flat'):
        return False
    with _LOCK:
        _STATE['direction'] = direction
        _STATE['updated_at'] = time.time()
        _STATE['source'] = source
    return True


def get_state():
    """Returns current OTHERS.D state. 'unknown' if stale or never updated."""
    with _LOCK:
        if _STATE['updated_at'] == 0:
            return 'unknown'
        age_h = (time.time() - _STATE['updated_at']) / 3600.0
        if age_h > STALE_HOURS:
            return 'unknown'
        return _STATE['direction']


def block_alt_side(coin, side):
    """Returns (blocked: bool, reason: str).
    Fails open — unknown/flat/disabled never blocks.
    """
    if not ENABLED:
        return False, 'gate-disabled'
    coin_u = (coin or '').upper().replace('.P', '')
    if coin_u in EXEMPT_COINS:
        return False, 'exempt-coin'
    
    state = get_state()
    if state in ('unknown', 'flat'):
        return False, 'no-signal'
    
    side_u = (side or '').upper()
    
    # Rising state: alts gaining share → block SHORTS
    if state == 'rising' and side_u in ('SELL', 'S', 'SHORT'):
        return True, 'others_d_rising_blocks_short'
    
    # Falling state: alts losing share → block LONGS
    if state == 'falling' and side_u in ('BUY', 'B', 'L', 'LONG'):
        return True, 'others_d_falling_blocks_long'
    
    return False, 'aligned'


def status():
    """Snapshot for /others_d_status endpoint."""
    with _LOCK:
        age_s = time.time() - _STATE['updated_at'] if _STATE['updated_at'] else None
        return {
            'enabled': ENABLED,
            'direction': _STATE['direction'],
            'updated_at': _STATE['updated_at'],
            'age_seconds': age_s,
            'age_hours': age_s / 3600.0 if age_s else None,
            'stale_hours_threshold': STALE_HOURS,
            'is_stale': age_s is None or age_s / 3600.0 > STALE_HOURS,
            'source': _STATE['source'],
            'state_returned': get_state(),
        }
