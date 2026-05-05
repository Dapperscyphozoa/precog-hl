"""
attribution.py — match HL fills to engine-owned cloids for true per-engine PnL.

Problem: cloid prefixes (lsr_, smcv2_, ...) are SHA-256 hashed → cannot be
recovered from raw cloid hex. So we can't attribute a fill to an engine just
by inspecting its cloid.

Solution: each engine pushes its currently-owned cloids in `open_positions[].cloids`
and `history_12h[].cloids` (per dashboard_push.py). We build a registry mapping
{cloid → engine}, fetch HL userFills, and match each fill against the registry.
Fills not in any registry are uncategorized (manual orders, expired-cancel artifacts,
or pre-tracking fills).

Real per-engine PnL = sum(fill_pnl for fill in userFills if fill.cid in engine_registry)
                    + (sum of unrealized PnL from open positions if requested)

Difference between (real per-engine PnL) and (engine-state-reported PnL) reveals
cross-engine collisions where HL netted positions across engines.
"""
import os, time, json, urllib.request, threading
from collections import defaultdict

WALLET = os.environ.get('HL_ADDRESS', '0x3eDaD0649Db466E6E7B9a0Caa3E5d6ddc71B5ffE')
FILLS_CACHE_TTL_SEC = 30
LOOKBACK_HOURS = 24

_fills_cache = {'ts': 0, 'data': []}
_fills_lock = threading.Lock()

def _hl_post(payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request('https://api.hyperliquid.xyz/info',
                                 data=body, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())

def _get_user_fills():
    """Fetch userFills from HL. Cached for FILLS_CACHE_TTL_SEC."""
    with _fills_lock:
        if time.time() - _fills_cache['ts'] < FILLS_CACHE_TTL_SEC:
            return _fills_cache['data']
    try:
        data = _hl_post({'type': 'userFills', 'user': WALLET})
    except Exception:
        return _fills_cache['data']
    with _fills_lock:
        _fills_cache['data'] = data or []
        _fills_cache['ts'] = time.time()
    return _fills_cache['data']

def _build_cloid_registry(engine_states):
    """Walk engine_states, build {cloid_hex_lower → engine_name} from open + history."""
    registry = {}
    for engine_name, state in (engine_states or {}).items():
        # Open positions: each has cloids dict
        for p in state.get('open_positions', []) or []:
            cls = p.get('cloids') or {}
            for kind, cl in cls.items():
                if cl:
                    registry[str(cl).lower()] = engine_name
        # Closed history (12h window)
        for h in state.get('history_12h', []) or []:
            cls = h.get('cloids') or {}
            for kind, cl in cls.items():
                if cl:
                    registry[str(cl).lower()] = engine_name
    return registry

def compute_attribution(engine_states):
    """Cross-reference HL fills against engine cloid registries.

    Returns:
      {
        'window_hours': float,
        'fills_total':  int,
        'attributed':   int,
        'unattributed': int,
        'by_engine': {
          engine: {
            'fills':           int,
            'realized_pnl':    float,   # sum of closedPnl across attributed fills
            'gross_volume':    float,   # sum of |fill_size * fill_px|
            'fees_paid':       float,   # sum of fees
            'wins':            int,     # closedPnl > 0
            'losses':          int,     # closedPnl < 0
            'be':              int,     # closedPnl == 0
          }
        },
        'collision_check': {
          # Coins where multiple engines had simultaneous positions on HL
          coin: {'engines': [...], 'last_fill_t': ms}
        },
        'by_engine_unrealized': {engine: float},  # from open_positions
      }
    """
    fills = _get_user_fills()
    registry = _build_cloid_registry(engine_states)

    cutoff_ms = int((time.time() - LOOKBACK_HOURS * 3600) * 1000)
    by_engine = defaultdict(lambda: {
        'fills': 0, 'realized_pnl': 0.0, 'gross_volume': 0.0,
        'fees_paid': 0.0, 'wins': 0, 'losses': 0, 'be': 0,
    })
    coin_engines_in_window = defaultdict(set)
    coin_last_fill_t = {}

    fills_total = 0
    attributed = 0
    for f in fills or []:
        ts = int(f.get('time', 0))
        if ts < cutoff_ms:
            continue
        fills_total += 1
        cl = (f.get('cloid') or '').lower()
        if not cl:
            continue
        engine = registry.get(cl)
        if not engine:
            continue
        attributed += 1
        coin = f.get('coin', '?')
        sz = float(f.get('sz', 0) or 0)
        px = float(f.get('px', 0) or 0)
        fee = float(f.get('fee', 0) or 0)
        pnl = float(f.get('closedPnl', 0) or 0)

        e = by_engine[engine]
        e['fills'] += 1
        e['realized_pnl'] += pnl
        e['gross_volume'] += abs(sz * px)
        e['fees_paid'] += fee
        if pnl > 0.001: e['wins'] += 1
        elif pnl < -0.001: e['losses'] += 1
        else: e['be'] += 1

        coin_engines_in_window[coin].add(engine)
        coin_last_fill_t[coin] = max(coin_last_fill_t.get(coin, 0), ts)

    # Collision detection: same coin had fills attributed to >1 engine in window
    collisions = {}
    for coin, engines in coin_engines_in_window.items():
        if len(engines) > 1:
            collisions[coin] = {
                'engines':     sorted(engines),
                'last_fill_t': coin_last_fill_t.get(coin, 0),
            }

    # Per-engine unrealized from open positions
    by_engine_unrealized = {}
    for engine_name, state in (engine_states or {}).items():
        ur = sum(float((p.get('unreal_pnl') or 0)) for p in state.get('open_positions', []) or [])
        by_engine_unrealized[engine_name] = round(ur, 4)

    # Round + finalize
    out_engines = {}
    for k, v in by_engine.items():
        out_engines[k] = {
            'fills':        v['fills'],
            'realized_pnl': round(v['realized_pnl'], 4),
            'gross_volume': round(v['gross_volume'], 2),
            'fees_paid':    round(v['fees_paid'], 4),
            'wins':         v['wins'],
            'losses':       v['losses'],
            'be':           v['be'],
            'wr_pct':       round(v['wins']/(v['wins']+v['losses'])*100, 2)
                            if (v['wins']+v['losses']) > 0 else None,
            'unrealized':   by_engine_unrealized.get(k, 0.0),
        }

    return {
        'window_hours':    LOOKBACK_HOURS,
        'fills_total':     fills_total,
        'attributed':      attributed,
        'unattributed':    fills_total - attributed,
        'cloids_registered': len(registry),
        'by_engine':       out_engines,
        'collisions':      collisions,
        'fetched_t':       int(time.time() * 1000),
    }
