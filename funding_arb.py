"""Cross-venue funding divergence. When HL funding differs from peer venue, directional bias.

Sources:
- HL: api.hyperliquid.xyz/info metaAndAssetCtxs (hourly funding rate)
- Binance: fapi.binance.com/fapi/v1/premiumIndex (8h rate) — GEO-BLOCKED from Render
- Bybit: api.bybit.com/v5/market/tickers (8h rate) — bulk endpoint, works globally

On Render (US AWS), Binance returns HTTP 451 (geo-restricted). Bybit is the
de-facto cross-venue source on this host. _CACHE['binance'] kept for backward-
compat with existing readers but populated from Bybit when Binance is blocked.
The 'binance' name in code paths is a historical label, not a source guarantee.
"""
import threading, time, urllib.request, json

_CACHE = {'hl': {}, 'binance': {}, 'bybit': {}, 'ts': 0, 'sources': {'binance': False, 'bybit': False}}
_LOCK = threading.Lock()
REFRESH_SEC = 900  # 15min

def _refresh_hl():
    try:
        req = urllib.request.Request('https://api.hyperliquid.xyz/info',
            data=json.dumps({'type':'metaAndAssetCtxs'}).encode(),
            headers={'Content-Type':'application/json'})
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if not isinstance(r, list) or len(r) < 2: return
        universe = r[0].get('universe', [])
        ctxs = r[1]
        with _LOCK:
            for i, asset in enumerate(universe):
                if i >= len(ctxs): break
                name = asset.get('name')
                f = ctxs[i].get('funding')
                if name and f is not None: _CACHE['hl'][name] = float(f)
    except Exception: pass

def _refresh_binance():
    """Binance fapi — geo-blocked from Render (HTTP 451). Kept for non-US deploys."""
    try:
        r = json.loads(urllib.request.urlopen(
            'https://fapi.binance.com/fapi/v1/premiumIndex', timeout=15).read())
        with _LOCK:
            for item in r:
                sym = item.get('symbol','')
                if sym.endswith('USDT'):
                    coin = sym[:-4]
                    f = item.get('lastFundingRate')
                    if f is not None: _CACHE['binance'][coin] = float(f)
            if r: _CACHE['sources']['binance'] = True
    except Exception:
        with _LOCK: _CACHE['sources']['binance'] = False

def _refresh_bybit():
    """Bybit bulk tickers — single call returns funding for all linear perps.
    Stores into _CACHE['bybit']. Also mirrors into _CACHE['binance'] as a
    fallback so existing readers (divergence/arb_bias/funding_engine) work
    transparently when Binance is geo-blocked."""
    try:
        r = json.loads(urllib.request.urlopen(
            'https://api.bybit.com/v5/market/tickers?category=linear', timeout=20).read())
        items = r.get('result', {}).get('list', []) if r.get('retCode') == 0 else []
        if not items: return
        bybit_funding = {}
        for it in items:
            sym = it.get('symbol', '')
            if not sym.endswith('USDT'): continue
            coin = sym[:-4]
            fr = it.get('fundingRate')
            if fr is None or fr == '': continue
            try:
                bybit_funding[coin] = float(fr)
            except Exception: continue
        with _LOCK:
            _CACHE['bybit'] = bybit_funding
            _CACHE['sources']['bybit'] = True
            # Mirror into 'binance' slot ONLY if Binance fetch failed.
            # This keeps existing readers working without code changes.
            if not _CACHE['sources']['binance']:
                _CACHE['binance'] = dict(bybit_funding)
    except Exception:
        with _LOCK: _CACHE['sources']['bybit'] = False

def refresh():
    _refresh_hl()
    _refresh_binance()
    _refresh_bybit()
    with _LOCK: _CACHE['ts'] = time.time()

def divergence(coin):
    """Returns HL_funding - peer_funding (peer = Binance if available else Bybit). Positive = HL higher."""
    with _LOCK:
        h = _CACHE['hl'].get(coin); b = _CACHE['binance'].get(coin)
    if h is None or b is None: return None
    return h - b

def arb_bias(coin, threshold=0.0005):
    """If HL funding much higher than peer → HL longs paying too much → short bias on HL."""
    d = divergence(coin)
    if d is None: return 0
    if d > threshold: return -1   # HL funding rich, short HL
    if d < -threshold: return 1   # HL funding cheap, long HL
    return 0

def status():
    with _LOCK:
        return {'hl_coins': len(_CACHE['hl']), 'binance_coins': len(_CACHE['binance']),
                'bybit_coins': len(_CACHE.get('bybit', {})),
                'sources': dict(_CACHE.get('sources', {})),
                'last_refresh': int(time.time() - _CACHE['ts']) if _CACHE['ts'] else None}

def needs_refresh():
    with _LOCK:
        return time.time() - _CACHE['ts'] > REFRESH_SEC


# ─── POSITION-LEVEL FUNDING COLLECTION (added Apr 20 2026) ───
# When holding a position, check if funding is paying US. If yes, consider extended hold.

def get_hl_funding_rate(coin):
    """Return HL's current hourly funding rate for coin. Returns 0 if unavailable."""
    with _LOCK:
        return _CACHE.get('hl', {}).get(coin, 0)

def should_extend_hold(coin, side, age_sec, pnl_pct):
    """Should we HOLD past normal TP to collect funding?
    Conditions:
    - Position is in profit
    - Funding favors us (paying our side)
    - Age < 4h
    - Rate magnitude > 0.01%/hr
    Returns (extend: bool, reason: str)
    """
    if age_sec > 4 * 3600: return (False, "age_cap")
    if pnl_pct <= 0: return (False, "not_in_profit")
    rate = get_hl_funding_rate(coin)
    if rate == 0: return (False, "no_funding_data")
    # LONG receives when rate < 0 (shorts pay longs), SHORT receives when rate > 0
    our_side_paid = (side == 'L' and rate < 0) or (side == 'S' and rate > 0)
    if not our_side_paid: return (False, f"funding_unfavorable_{rate*100:.4f}%")
    if abs(rate) < 0.0001: return (False, f"funding_too_small_{rate*100:.4f}%")
    return (True, f"collecting_+{abs(rate)*100:.4f}%/hr")

def funding_pnl_bonus(coin, side, notional_usd, hours_held):
    """Compute cumulative funding PnL in USD for position held hours_held."""
    rate = get_hl_funding_rate(coin)
    if side == 'L' and rate < 0: return abs(rate) * notional_usd * hours_held
    if side == 'S' and rate > 0: return rate * notional_usd * hours_held
    if side == 'L' and rate > 0: return -rate * notional_usd * hours_held
    if side == 'S' and rate < 0: return -abs(rate) * notional_usd * hours_held
    return 0
