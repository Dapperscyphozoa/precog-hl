"""
risk_cap.py — Account-level risk gate.

Single source of truth for whether a new trade should be allowed across all
engines on the shared HL wallet. Each engine queries the dashboard before
firing a setup; dashboard computes current cumulative exposure and returns
go/no-go.

Constraints enforced:

  1. TOTAL NOTIONAL CAP
     sum(all open + all pending notional, all engines) ≤ MAX_LEVERAGE × equity
     Default: 2.5x.

  2. PER-COIN-DIRECTION CONCENTRATION
     sum(notional on COIN + DIRECTION across all engines) ≤ MAX_COIN_DIR_LEVERAGE × equity
     Default: 0.3x. Prevents 4 engines all going BTC LONG simultaneously.

  3. PER-COIN GROSS
     sum(notional on COIN regardless of direction) ≤ MAX_COIN_LEVERAGE × equity
     Default: 0.5x. Prevents over-concentration even with hedged positions.

  4. PORTFOLIO RISK
     sum($ at risk per position = notional × sl_distance_pct) ≤ MAX_PORTFOLIO_RISK × equity
     Default: 25%. Caps drawdown if every SL fires simultaneously.

The check uses both (a) reported open positions from each engine's last push +
(b) live HL open orders fetched fresh each call (so pending limits count too).

Exposed as:  dashboard /api/risk_check
Engines call this BEFORE bulk_orders. If can_fire=False, skip the setup.
"""
import os, json, time, urllib.request, threading
from collections import defaultdict

# ─── Tunables (env-overridable) ──────────────────────────────────
MAX_LEVERAGE              = float(os.environ.get('RISK_MAX_LEVERAGE', '2.5'))
MAX_COIN_LEVERAGE         = float(os.environ.get('RISK_MAX_COIN_LEVERAGE', '0.5'))
MAX_COIN_DIR_LEVERAGE     = float(os.environ.get('RISK_MAX_COIN_DIR_LEVERAGE', '0.3'))
MAX_PORTFOLIO_RISK        = float(os.environ.get('RISK_MAX_PORTFOLIO_RISK', '0.25'))
DEFAULT_SL_PCT            = float(os.environ.get('RISK_DEFAULT_SL_PCT', '0.005'))  # 0.5%

WALLET = os.environ.get('HL_ADDRESS', '0x3eDaD0649Db466E6E7B9a0Caa3E5d6ddc71B5ffE')

# Cache HL open orders briefly to avoid hammering the info endpoint
_orders_cache = {'ts': 0, 'data': []}
_orders_lock = threading.Lock()
ORDERS_CACHE_TTL_SEC = 5

def _hl_post(payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request('https://api.hyperliquid.xyz/info',
                                 data=body, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())

def _get_hl_open_orders():
    with _orders_lock:
        if time.time() - _orders_cache['ts'] < ORDERS_CACHE_TTL_SEC:
            return _orders_cache['data']
    try:
        data = _hl_post({'type':'frontendOpenOrders', 'user':WALLET})
    except Exception:
        return _orders_cache['data']  # serve stale on error
    with _orders_lock:
        _orders_cache['data'] = data
        _orders_cache['ts'] = time.time()
    return data

def _compute_exposure(engine_states):
    """Aggregate open positions across all engines + live HL pending orders.

    Returns:
      {
        'total_notional': float,
        'total_risk_usd': float,    # sum of notional × sl_pct
        'by_coin':        {coin: {'long': $, 'short': $, 'total': $}},
        'positions':      [list of position dicts for diagnostic],
      }
    """
    by_coin = defaultdict(lambda: {'long': 0.0, 'short': 0.0, 'total': 0.0,
                                    'risk_long': 0.0, 'risk_short': 0.0})
    total_notional = 0.0
    total_risk = 0.0
    diag_positions = []

    # 1. Engine-reported open positions
    for ename, s in (engine_states or {}).items():
        for p in (s.get('open_positions') or []):
            coin = p.get('coin') or '?'
            side = (p.get('side') or '').upper()
            entry = float(p.get('entry') or 0)
            size = float(p.get('size') or 0)
            sl_px = float(p.get('sl') or 0)
            ntl = entry * size
            if ntl <= 0:
                continue
            sl_pct = abs(entry - sl_px) / entry if (entry and sl_px) else DEFAULT_SL_PCT
            risk_usd = ntl * sl_pct
            is_long = (side == 'LONG')
            by_coin[coin]['total'] += ntl
            if is_long:
                by_coin[coin]['long'] += ntl
                by_coin[coin]['risk_long'] += risk_usd
            else:
                by_coin[coin]['short'] += ntl
                by_coin[coin]['risk_short'] += risk_usd
            total_notional += ntl
            total_risk += risk_usd
            diag_positions.append({'engine': ename, 'coin': coin, 'side': side,
                                   'notional': round(ntl, 2), 'risk_usd': round(risk_usd, 4)})

    # 2. Live HL pending limit-entry orders (un-filled but committed)
    # These count toward exposure because they could fill at any moment.
    orders = _get_hl_open_orders()
    pending_by_coin = defaultdict(lambda: {'long': 0.0, 'short': 0.0})
    for o in orders:
        # Skip triggers (SL/TP) — only count the entry limit
        if o.get('isTrigger'): continue
        if o.get('reduceOnly'): continue
        coin = o.get('coin', '?')
        side = o.get('side')  # 'B' = buy/long, 'A' = ask/short
        is_long = (side == 'B')
        sz = float(o.get('sz', 0))
        px = float(o.get('limitPx', 0))
        ntl = sz * px
        if ntl <= 0: continue
        # Pending notional adds to exposure but at default risk pct (since SL not visible here)
        if is_long:
            pending_by_coin[coin]['long'] += ntl
        else:
            pending_by_coin[coin]['short'] += ntl
        by_coin[coin]['total'] += ntl
        if is_long:
            by_coin[coin]['long'] += ntl
            by_coin[coin]['risk_long'] += ntl * DEFAULT_SL_PCT
        else:
            by_coin[coin]['short'] += ntl
            by_coin[coin]['risk_short'] += ntl * DEFAULT_SL_PCT
        total_notional += ntl
        total_risk += ntl * DEFAULT_SL_PCT
        diag_positions.append({'engine': '(pending)', 'coin': coin,
                               'side': 'LONG' if is_long else 'SHORT',
                               'notional': round(ntl, 2),
                               'risk_usd': round(ntl * DEFAULT_SL_PCT, 4)})

    return {
        'total_notional': round(total_notional, 2),
        'total_risk_usd': round(total_risk, 4),
        'by_coin': {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in by_coin.items()},
        'positions': diag_positions,
        'pending_by_coin': {k: dict(v) for k, v in pending_by_coin.items()},
    }

def evaluate(equity, engine_states, requested_coin=None,
             requested_side=None, requested_notional=0.0, requested_sl_pct=None):
    """Decide whether a new setup is allowed.

    Args:
      equity:            current account value ($)
      engine_states:     dict {engine_name: state} from dashboard
      requested_coin:    coin symbol the engine wants to fire on
      requested_side:    'LONG' or 'SHORT'
      requested_notional: $ size of the new position
      requested_sl_pct:   SL distance as % of entry (defaults to DEFAULT_SL_PCT)

    Returns:
      {
        'can_fire':           bool,
        'block_reason':       None | 'leverage' | 'coin_concentration' | 'coin_dir' | 'portfolio_risk',
        'limits':             {...},
        'current':            {...},
        'projected':          {...},
        'equity':             float,
      }
    """
    if equity is None or equity <= 0:
        return {'can_fire': False, 'block_reason': 'no_equity_data',
                'equity': equity, 'limits': {}, 'current': {}, 'projected': {}}

    cur = _compute_exposure(engine_states)
    sl_pct = requested_sl_pct if requested_sl_pct else DEFAULT_SL_PCT
    rn = float(requested_notional or 0)
    proj_total_notional = cur['total_notional'] + rn
    proj_total_risk = cur['total_risk_usd'] + rn * sl_pct

    # Per-coin numbers
    coin_data = cur['by_coin'].get(requested_coin or '', {'long':0,'short':0,'total':0})
    is_long = (requested_side or '').upper() == 'LONG'
    proj_coin_total = coin_data.get('total', 0) + rn
    proj_coin_dir = coin_data.get('long' if is_long else 'short', 0) + rn

    limits = {
        'max_total_notional':  round(MAX_LEVERAGE * equity, 2),
        'max_coin_notional':   round(MAX_COIN_LEVERAGE * equity, 2),
        'max_coin_dir_notional': round(MAX_COIN_DIR_LEVERAGE * equity, 2),
        'max_portfolio_risk':  round(MAX_PORTFOLIO_RISK * equity, 2),
    }
    projected = {
        'total_notional':       round(proj_total_notional, 2),
        'total_risk_usd':       round(proj_total_risk, 4),
        'coin_total':           round(proj_coin_total, 2),
        'coin_dir':             round(proj_coin_dir, 2),
    }

    # Run checks in priority order
    block_reason = None
    if proj_total_notional > limits['max_total_notional']:
        block_reason = 'leverage'
    elif proj_total_risk > limits['max_portfolio_risk']:
        block_reason = 'portfolio_risk'
    elif requested_coin and proj_coin_total > limits['max_coin_notional']:
        block_reason = 'coin_concentration'
    elif requested_coin and proj_coin_dir > limits['max_coin_dir_notional']:
        block_reason = 'coin_dir'

    return {
        'can_fire':     block_reason is None,
        'block_reason': block_reason,
        'equity':       equity,
        'limits':       limits,
        'current': {
            'total_notional': cur['total_notional'],
            'total_risk_usd': cur['total_risk_usd'],
            'coin_total':     coin_data.get('total', 0),
            'coin_dir':       coin_data.get('long' if is_long else 'short', 0),
            'open_positions_count': len(cur['positions']),
        },
        'projected':    projected,
        'request': {
            'coin':     requested_coin,
            'side':     requested_side,
            'notional': rn,
            'sl_pct':   sl_pct,
        },
    }
