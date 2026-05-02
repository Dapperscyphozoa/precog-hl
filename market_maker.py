"""Market-maker laddered entry system for HL.

Replaces single maker order with a ladder of post-only limit orders placed at
structural zones (Order Blocks, Fair Value Gaps, swing lows/highs). All orders
are Alo (Add Liquidity Only) — never market, never taker.

Design principles:
- Patient: 5-min fill window per ladder cycle
- Post-only: Alo TIF rejects orders that would cross the book
- Structural: rungs anchored to OBs/FVGs, not arbitrary % offsets
- Adaptive: walks ladder closer if market drifts away
- Risk-controlled: total notional capped, reduce on partial fills
"""
import os
import time
import json
import urllib.request
import threading
from collections import defaultdict


# ─── Configuration ──────────────────────────────────────────────────────
# Number of ladder rungs (typical 3-5)
LADDER_RUNGS = int(os.environ.get('LADDER_RUNGS', '4'))

# Total fill window in seconds before cancel-all
LADDER_FILL_WINDOW_S = int(os.environ.get('LADDER_FILL_WINDOW_S', '300'))

# Max distance from current mid that a rung can be placed (% from mid)
# Rungs further than this are dropped — too far to be relevant
LADDER_MAX_DISTANCE_PCT = float(os.environ.get('LADDER_MAX_DISTANCE_PCT', '0.015'))  # 1.5%

# How aggressively to scale toward worse-priced rungs.
# 1.0 = equal weight per rung. 1.5 = bigger size on better-priced rungs.
LADDER_SIZE_SKEW = float(os.environ.get('LADDER_SIZE_SKEW', '1.3'))

# How often (seconds) to re-evaluate and walk the ladder
LADDER_WALK_INTERVAL_S = int(os.environ.get('LADDER_WALK_INTERVAL_S', '30'))

# If market drifts this far from highest-priced unfilled rung, rebuild ladder
LADDER_REBUILD_TRIGGER_PCT = float(os.environ.get('LADDER_REBUILD_TRIGGER_PCT', '0.005'))  # 0.5%


# ─── Zone detection (HL candles) ────────────────────────────────────────
_HL_INFO_URL = 'https://api.hyperliquid.xyz/info'

def fetch_hl_candles(coin, interval='15m', n_bars=200):
    """Fetch OHLCV candles from Hyperliquid. Returns list of {t,o,h,l,c,v}."""
    end_ms = int(time.time() * 1000)
    # Compute start based on interval
    interval_ms = {'1m': 60_000, '5m': 300_000, '15m': 900_000, '1h': 3_600_000}
    step = interval_ms.get(interval, 900_000)
    start_ms = end_ms - (n_bars * step)
    payload = {
        'type': 'candleSnapshot',
        'req': {'coin': coin, 'interval': interval, 'startTime': start_ms, 'endTime': end_ms}
    }
    try:
        req = urllib.request.Request(_HL_INFO_URL,
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
        data = json.loads(urllib.request.urlopen(req, timeout=8).read())
        if not isinstance(data, list): return []
        return [{
            't': int(c.get('t', 0)),
            'o': float(c.get('o', 0)),
            'h': float(c.get('h', 0)),
            'l': float(c.get('l', 0)),
            'c': float(c.get('c', 0)),
            'v': float(c.get('v', 0) or 0),
        } for c in data if c.get('o') and c.get('h') and c.get('l') and c.get('c')]
    except Exception as e:
        return []


def detect_zones(coin, side, current_px, lookback=80):
    """Detect favorable zones for entry on given side.
    For BUY: bullish OBs, bullish FVGs, swing lows below current_px.
    For SELL: bearish OBs, bearish FVGs, swing highs above current_px.
    
    Returns dict {obs, fvgs, swings} — all priced relative to current_px.
    Each zone has {top, bottom, mid}.
    """
    candles = fetch_hl_candles(coin, '15m', lookback + 20)
    if len(candles) < 10:
        return {'obs': [], 'fvgs': [], 'swings': []}
    
    is_buy = (str(side).upper() == 'BUY')
    cs = candles[-lookback:]
    
    obs, fvgs, swings = [], [], []
    
    # Order Blocks: last opposite-color candle before strong move in our direction
    for i in range(2, len(cs) - 1):
        prev = cs[i-1]; cur = cs[i]; nxt = cs[i+1]
        if is_buy:
            # Bullish OB: down candle followed by strong up move
            if cur['c'] < cur['o'] and nxt['c'] > nxt['o']:
                move = (nxt['c'] - nxt['o']) / max(nxt['o'], 1e-12)
                if move > 0.005:
                    obs.append({'top': cur['h'], 'bottom': cur['l'], 'mid': (cur['h']+cur['l'])/2})
        else:
            # Bearish OB
            if cur['c'] > cur['o'] and nxt['c'] < nxt['o']:
                move = (nxt['o'] - nxt['c']) / max(nxt['o'], 1e-12)
                if move > 0.005:
                    obs.append({'top': cur['h'], 'bottom': cur['l'], 'mid': (cur['h']+cur['l'])/2})
    
    # FVGs: 3-candle gaps
    for i in range(1, len(cs) - 1):
        prev = cs[i-1]; nxt = cs[i+1]
        if is_buy:
            # Bullish FVG: prev high < nxt low (unfilled gap up)
            if prev['h'] < nxt['l']:
                fvgs.append({'top': nxt['l'], 'bottom': prev['h'], 'mid': (nxt['l']+prev['h'])/2})
        else:
            # Bearish FVG
            if prev['l'] > nxt['h']:
                fvgs.append({'top': prev['l'], 'bottom': nxt['h'], 'mid': (prev['l']+nxt['h'])/2})
    
    # Swing levels: pivot lows (BUY) or pivot highs (SELL)
    for i in range(3, len(cs) - 3):
        if is_buy:
            # Pivot low: lower than 3 on each side
            if (cs[i]['l'] < cs[i-1]['l'] and cs[i]['l'] < cs[i-2]['l'] and cs[i]['l'] < cs[i-3]['l']
                and cs[i]['l'] < cs[i+1]['l'] and cs[i]['l'] < cs[i+2]['l'] and cs[i]['l'] < cs[i+3]['l']):
                swings.append({'top': cs[i]['l'], 'bottom': cs[i]['l'], 'mid': cs[i]['l']})
        else:
            # Pivot high
            if (cs[i]['h'] > cs[i-1]['h'] and cs[i]['h'] > cs[i-2]['h'] and cs[i]['h'] > cs[i-3]['h']
                and cs[i]['h'] > cs[i+1]['h'] and cs[i]['h'] > cs[i+2]['h'] and cs[i]['h'] > cs[i+3]['h']):
                swings.append({'top': cs[i]['h'], 'bottom': cs[i]['h'], 'mid': cs[i]['h']})
    
    # Filter zones: BUY zones must be BELOW current_px (we want to buy low)
    # SELL zones must be ABOVE current_px (we want to sell high)
    def relevant(z):
        if is_buy:
            # We want to BUY the zone, so its top must be at-or-below current_px
            return z['top'] <= current_px * 1.0005  # allow tiny tolerance
        else:
            return z['bottom'] >= current_px * 0.9995
    
    obs = [z for z in obs if relevant(z)]
    fvgs = [z for z in fvgs if relevant(z)]
    swings = [z for z in swings if relevant(z)]
    
    # Filter zones outside MAX distance from current
    def near_enough(z):
        dist = abs(z['mid'] - current_px) / max(current_px, 1e-12)
        return dist <= LADDER_MAX_DISTANCE_PCT * 1.5
    
    obs = [z for z in obs if near_enough(z)]
    fvgs = [z for z in fvgs if near_enough(z)]
    swings = [z for z in swings if near_enough(z)]
    
    return {'obs': obs, 'fvgs': fvgs, 'swings': swings}


def compute_ladder_levels(coin, side, current_px, total_size, zones=None):
    """Compute laddered limit prices and sizes.
    
    Returns list of (price, size, anchor_label) tuples in ladder order:
    - First entry = highest priority (closest to current_px on BUY, furthest below)
    - All entries are at favorable prices vs current
    
    Strategy:
    1. Anchor rungs to top of ladder, mid of best zone, bottom of zone, etc.
    2. If no zones detected, fall back to evenly-spaced ladder
    3. Skew sizes toward better-priced rungs (further from current)
    """
    is_buy = (str(side).upper() == 'BUY')
    if zones is None:
        zones = detect_zones(coin, side, current_px)
    
    # Collect candidate prices from zones
    candidates = []
    for z in zones.get('obs', []):
        # For BUY: enter at top of bullish OB (most favorable still in zone)
        candidates.append((z['top'], 'OB_top'))
        candidates.append((z['mid'], 'OB_mid'))
        candidates.append((z['bottom'], 'OB_bottom'))
    for z in zones.get('fvgs', []):
        candidates.append((z['top'], 'FVG_top'))
        candidates.append((z['mid'], 'FVG_mid'))
    for z in zones.get('swings', []):
        candidates.append((z['mid'], 'swing'))
    
    # Filter: BUY rungs must be ≤ current_px (favorable). SELL ≥ current_px.
    if is_buy:
        candidates = [(p, n) for p, n in candidates if p <= current_px]
    else:
        candidates = [(p, n) for p, n in candidates if p >= current_px]
    
    # Sort: BUY rungs from highest-to-lowest (we want to fill highest first)
    # SELL rungs from lowest-to-highest
    candidates.sort(key=lambda x: -x[0] if is_buy else x[0])
    
    # Deduplicate near-identical prices (within 0.05%)
    deduped = []
    for p, n in candidates:
        if not deduped:
            deduped.append((p, n)); continue
        last_p = deduped[-1][0]
        if abs(p - last_p) / max(last_p, 1e-12) > 0.0005:
            deduped.append((p, n))
    candidates = deduped
    
    # Take top N rungs
    if len(candidates) > LADDER_RUNGS:
        candidates = candidates[:LADDER_RUNGS]
    
    # If we have fewer than 2 rungs, supplement with even-offset rungs
    while len(candidates) < min(LADDER_RUNGS, 3):
        # Build evenly-spaced fallback rungs
        idx = len(candidates)
        offset_pct = (0.0010 + idx * 0.0030)  # 0.1%, 0.4%, 0.7%, ...
        if is_buy:
            px = current_px * (1 - offset_pct)
        else:
            px = current_px * (1 + offset_pct)
        candidates.append((px, f'fallback_{idx+1}'))
    
    # Apply size skew: prices further from current get larger size (LADDER_SIZE_SKEW > 1)
    n = len(candidates)
    if n == 0:
        return []
    
    # Distance-based weights
    weights = []
    for i, (p, _) in enumerate(candidates):
        dist = abs(p - current_px) / max(current_px, 1e-12)
        # Further = larger weight (favoring patient fills)
        w = (1.0 + dist * 100) ** LADDER_SIZE_SKEW
        weights.append(w)
    
    total_w = sum(weights)
    levels = []
    for i, ((p, label), w) in enumerate(zip(candidates, weights)):
        size = total_size * (w / total_w)
        levels.append((p, size, label))
    
    return levels


# ─── Order placement ────────────────────────────────────────────────────
# These are caller-provided helpers — pass in references via context dict.
# Keeps this module decoupled from precog.py imports.

def place_laddered_entry(exchange, info, wallet, coin, is_buy, total_size, current_px,
                         round_price_fn, log_fn, cloid_obj=None):
    """Place laddered post-only limit orders.
    
    Returns dict {placed: [{px, size, oid, label}], total_placed_size, status}.
    """
    zones = detect_zones(coin, 'BUY' if is_buy else 'SELL', current_px)
    levels = compute_ladder_levels(coin, 'BUY' if is_buy else 'SELL',
                                   current_px, total_size, zones)
    
    if not levels:
        log_fn(f"LADDER {coin}: no levels computed, skipping")
        return {'placed': [], 'total_placed_size': 0, 'status': 'no_levels'}
    
    log_fn(f"LADDER {coin} {'BUY' if is_buy else 'SELL'}: {len(levels)} rungs "
           f"current_px={current_px:.6g} zones obs={len(zones['obs'])} "
           f"fvgs={len(zones['fvgs'])} swings={len(zones['swings'])}")
    
    placed = []
    for idx, (px, sz, label) in enumerate(levels):
        try:
            rounded_px = round_price_fn(coin, px)
            r = exchange.order(coin, is_buy, sz, rounded_px,
                               {'limit': {'tif': 'Alo'}},
                               reduce_only=False, cloid=cloid_obj)
            status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
            
            if 'error' in status:
                err = status['error']
                # If post-only would cross, the price moved — try one tick further
                if 'post only' in err.lower() or 'cross' in err.lower():
                    log_fn(f"LADDER rung {idx+1}/{len(levels)} {coin} would cross @ {rounded_px}, skipping rung")
                    continue
                log_fn(f"LADDER rung {idx+1}/{len(levels)} {coin} REJECT: {err}")
                continue
            
            oid = (status.get('resting',{}).get('oid')
                   or status.get('filled',{}).get('oid'))
            
            log_fn(f"LADDER rung {idx+1}/{len(levels)} {coin} {'BUY' if is_buy else 'SELL'} "
                   f"{sz:.4g}@{rounded_px:.6g} ({label}) oid={oid}")
            placed.append({'px': rounded_px, 'size': sz, 'oid': oid, 'label': label})
        except Exception as e:
            log_fn(f"LADDER rung {idx+1} {coin} err: {e}")
    
    total_placed = sum(p['size'] for p in placed)
    return {
        'placed': placed,
        'total_placed_size': total_placed,
        'status': 'placed' if placed else 'all_failed',
        'zones': zones,
        'levels': [(l[0], l[1], l[2]) for l in levels],
    }


def cancel_unfilled_ladder(exchange, info, wallet, coin, placed_orders, log_fn):
    """Cancel any unfilled ladder rungs after window expires."""
    cancelled = []
    for order in placed_orders:
        oid = order.get('oid')
        if not oid: continue
        try:
            exchange.cancel(coin, oid)
            cancelled.append(oid)
        except Exception as e:
            # Most cancel errors = already filled, OK
            pass
    if cancelled:
        log_fn(f"LADDER cancel {coin}: cancelled {len(cancelled)} unfilled rungs")
    return cancelled


def get_ladder_fill_status(info, wallet, coin, placed_orders):
    """Check how many rungs have filled and total filled size."""
    try:
        state = info.user_state(wallet)
        positions = state.get('assetPositions', [])
        live_size = 0
        for p in positions:
            pos = p.get('position', {})
            if pos.get('coin') == coin:
                live_size = abs(float(pos.get('szi', 0)))
                break
    except Exception:
        live_size = 0
    
    total_placed = sum(p['size'] for p in placed_orders)
    fill_pct = (live_size / total_placed) if total_placed > 0 else 0
    return {
        'filled_size': live_size,
        'total_placed': total_placed,
        'fill_pct': fill_pct,
    }
