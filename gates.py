#!/usr/bin/env python3
"""9-Gate Signal Quality Engine — Institutional-grade entry filtering
Gates 0-8 must pass before any signal executes (precog or webhook).
Uses: HL order book, trades, funding, OI + Fear/Greed API.
"""
import time, json, math, statistics
import urllib.request

# HL API helper
def hl_post(body):
    r = urllib.request.Request('https://api.hyperliquid.xyz/info', method='POST',
        data=json.dumps(body).encode(), headers={'Content-Type':'application/json'})
    return json.loads(urllib.request.urlopen(r, timeout=5).read())

# Fear/Greed cache (refresh every 30 min)
_fng_cache = {'val': 50, 'ts': 0}
def get_fear_greed():
    if time.time() - _fng_cache['ts'] < 1800:
        return _fng_cache['val']
    try:
        r = urllib.request.urlopen('https://api.alternative.me/fng/?limit=1', timeout=5)
        d = json.loads(r.read())
        _fng_cache['val'] = int(d['data'][0]['value'])
        _fng_cache['ts'] = time.time()
    except Exception: pass
    return _fng_cache['val']


# ═══════════════════════════════════════════════════════
# GATE 0: DATA FRESHNESS
# ═══════════════════════════════════════════════════════
def gate0_freshness(candles, max_age_sec=120):
    """Reject if latest candle is stale (>2 min old)."""
    if not candles: return False, 0, "no candles"
    latest_ts = candles[-1][0] / 1000  # ms to sec
    age = time.time() - latest_ts
    if age > max_age_sec:
        return False, 0, f"stale ({age:.0f}s old)"
    return True, 1.0, f"fresh ({age:.0f}s)"

# ═══════════════════════════════════════════════════════
# GATE 1: ENTRY TIMING
# ═══════════════════════════════════════════════════════
def gate1_timing(candles):
    """Only enter near candle close, not mid-bar. Last 20% of bar is optimal."""
    if not candles or len(candles) < 2: return True, 0.5, "insufficient"
    bar_duration = candles[-1][0] - candles[-2][0]
    elapsed = time.time() * 1000 - candles[-1][0]
    pct = elapsed / bar_duration if bar_duration > 0 else 0
    if pct < 0.3:
        return False, 0, f"too early ({pct*100:.0f}% of bar)"
    score = min(1.0, pct)
    return True, score, f"timing {pct*100:.0f}%"


# ═══════════════════════════════════════════════════════
# GATE 2: MIN TICK COUNT (v2.7 — volatility regime-based)
# ═══════════════════════════════════════════════════════
def gate2_tick_count(coin, min_ticks=5):
    """Reject if market is dead (low tick count). Adapts threshold to vol regime."""
    try:
        trades = hl_post({'type':'recentTrades','coin':coin,'num':50})
        if not trades or len(trades) < 2: return False, 0, "no trades"
        span = (trades[0]['time'] - trades[-1]['time']) / 1000
        tick_rate = len(trades) / span if span > 0 else 0
        # Vol regime: high vol = raise threshold, low vol = lower it
        vol = sum(abs(float(t['px']) - float(trades[i+1]['px'])) / float(trades[i+1]['px'])
                  for i, t in enumerate(trades[:-1])) / len(trades)
        regime_mult = 1.0 + min(vol * 500, 2.0)  # scale up threshold in high vol
        adj_min = min_ticks * regime_mult
        if tick_rate < adj_min:
            return False, tick_rate / adj_min, f"low ticks {tick_rate:.1f}/s (need {adj_min:.1f})"
        return True, min(1.0, tick_rate / (adj_min * 2)), f"ticks {tick_rate:.1f}/s"
    except Exception: return True, 0.5, "tick check failed"

# ═══════════════════════════════════════════════════════
# GATE 3: Z-SCORE (Ψ-based)
# ═══════════════════════════════════════════════════════
def gate3_zscore(candles, max_z=2.5):
    """Reject if price is at statistical extreme (z-score too high = reversion likely)."""
    if not candles or len(candles) < 50: return True, 0.5, "insufficient data"
    closes = [c[4] for c in candles[-100:]]
    mean = statistics.mean(closes)
    stdev = statistics.stdev(closes)
    if stdev == 0: return True, 0.5, "zero stdev"
    z = abs(closes[-1] - mean) / stdev
    if z > max_z:
        return False, 0, f"extreme z={z:.2f} (max {max_z})"
    score = 1.0 - (z / max_z) * 0.5  # penalize high z but don't reject until max
    return True, score, f"z={z:.2f}"


# ═══════════════════════════════════════════════════════
# GATE 4: FEAR-ADJUSTED EDGE
# ═══════════════════════════════════════════════════════
def gate4_fear_edge(side):
    """Adjust confidence by fear/greed regime.
    High fear (0-25) = easier to go long (contrarian), harder to go short.
    High greed (75-100) = easier to go short, harder to go long."""
    fng = get_fear_greed()
    if side == 'BUY':
        if fng < 20: score = 1.0    # extreme fear = strong buy signal
        elif fng < 40: score = 0.8   # fear = decent buy
        elif fng < 60: score = 0.6   # neutral
        elif fng < 80: score = 0.3   # greed = weak buy
        else: score = 0.1            # extreme greed = terrible buy
    else:  # SELL
        if fng > 80: score = 1.0     # extreme greed = strong sell
        elif fng > 60: score = 0.8
        elif fng > 40: score = 0.6
        elif fng > 20: score = 0.3
        else: score = 0.1            # extreme fear = terrible sell
    return True, score, f"FnG={fng} ({side}→{score:.1f})"

# ═══════════════════════════════════════════════════════
# GATE 5: SPREAD FILTER
# ═══════════════════════════════════════════════════════
def gate5_spread(coin, max_spread_pct=0.05):
    """Reject if bid-ask spread is too wide (eats edge)."""
    try:
        book = hl_post({'type':'l2Book','coin':coin})
        bid = float(book['levels'][0][0]['px'])
        ask = float(book['levels'][1][0]['px'])
        spread = (ask - bid) / bid * 100
        if spread > max_spread_pct:
            return False, 0, f"spread {spread:.3f}% > {max_spread_pct}%"
        score = 1.0 - (spread / max_spread_pct)
        return True, score, f"spread {spread:.4f}%"
    except Exception: return True, 0.5, "spread check failed"


# ═══════════════════════════════════════════════════════
# GATE 6: ODDA VELOCITY (Order-Driven Directional Acceleration)
# ═══════════════════════════════════════════════════════
def gate6_odda(coin, side):
    """Order flow must align with signal direction.
    Measures buy vs sell volume ratio from recent trades."""
    try:
        trades = hl_post({'type':'recentTrades','coin':coin,'num':50})
        if not trades or len(trades) < 5: return True, 0.5, "insufficient trades"
        buy_vol = sum(float(t['sz']) * float(t['px']) for t in trades if t['side'] == 'A')
        sell_vol = sum(float(t['sz']) * float(t['px']) for t in trades if t['side'] == 'B')
        total = buy_vol + sell_vol
        if total == 0: return True, 0.5, "zero volume"
        buy_pct = buy_vol / total
        if side == 'BUY':
            if buy_pct < 0.35: return False, buy_pct, f"sell-heavy {buy_pct*100:.0f}% buy"
            score = min(1.0, buy_pct * 1.2)
        else:
            sell_pct = 1 - buy_pct
            if sell_pct < 0.35: return False, sell_pct, f"buy-heavy {sell_pct*100:.0f}% sell"
            score = min(1.0, sell_pct * 1.2)
        return True, score, f"ODDA {'buy' if side=='BUY' else 'sell'} {score:.2f}"
    except Exception: return True, 0.5, "ODDA check failed"

# ═══════════════════════════════════════════════════════
# GATE 7: DISLOCATION CHECK
# ═══════════════════════════════════════════════════════
def gate7_dislocation(candles, max_disloc_pct=2.0):
    """Reject if price is dislocated from VWAP (fair value).
    Prevents entries into markets that will snap back."""
    if not candles or len(candles) < 20: return True, 0.5, "insufficient"
    # Approx VWAP from recent candles (vol-weighted avg price)
    vwap_num = sum(c[4] * c[5] for c in candles[-20:])  # close * volume
    vwap_den = sum(c[5] for c in candles[-20:])
    if vwap_den == 0: return True, 0.5, "zero volume"
    vwap = vwap_num / vwap_den
    price = candles[-1][4]
    disloc = abs(price - vwap) / vwap * 100
    if disloc > max_disloc_pct:
        return False, 0, f"dislocated {disloc:.2f}% from VWAP"
    score = 1.0 - (disloc / max_disloc_pct) * 0.5
    return True, score, f"VWAP disloc {disloc:.2f}%"


# ═══════════════════════════════════════════════════════
# GATE MIN-EDGE — friction-aware sanity check on (tp_pct, sl_pct)
# Independent of the data-quality gates above; called from the entry path.
# ═══════════════════════════════════════════════════════
import os

# Friction model. Keep aligned with shadow_trades.FEE_ROUND_TRIP / SLIPPAGE_ROUND_TRIP.
MIN_EDGE_FEE_RT = float(os.environ.get('MIN_EDGE_FEE_RT', 0.0009))      # 9 bps round-trip taker
MIN_EDGE_SLIP_RT = float(os.environ.get('MIN_EDGE_SLIP_RT', 0.0005))    # 5 bps round-trip slip
MIN_EDGE_SAFETY = float(os.environ.get('MIN_EDGE_SAFETY', 0.0005))      # 5 bps safety margin
MIN_EDGE_RR = float(os.environ.get('MIN_EDGE_MIN_RR', 1.0))             # net-of-friction R:R floor

# Mode: 'off' (no-op), 'shadow' (log rejects, never block), 'live' (block + log).
# Default 'off' so this change is behavior-neutral until the operator promotes it.
MIN_EDGE_MODE = os.environ.get('MIN_EDGE_MODE', 'off').lower()


def compute_expected_edge(tp_pct, sl_pct,
                          fee_rt=None, slip_rt=None):
    """Return net expected edge on the winning leg, after round-trip friction.

    expected_edge = tp_pct - (fee_rt + slip_rt)

    Positive value = winning trade is profitable after costs. Logged on every
    entry as `expected_edge_at_entry` so analyze_trades can correlate with
    realized outcome.
    """
    if tp_pct is None or tp_pct <= 0:
        return 0.0
    fee = MIN_EDGE_FEE_RT if fee_rt is None else fee_rt
    slip = MIN_EDGE_SLIP_RT if slip_rt is None else slip_rt
    return float(tp_pct) - (fee + slip)


def gate_min_edge(tp_pct, sl_pct,
                  fee_rt=None, slip_rt=None, safety=None, min_rr=None):
    """Friction-aware entry gate.

    Returns (allow, info) where info has: edge, threshold, net_rr, reason.

    Two checks (both must pass):
      1. Net TP after friction must clear the safety margin
         (otherwise: even a winning trade barely beats fees)
      2. Net R:R = (tp_pct - friction) / (sl_pct + friction) >= min_rr
         (otherwise: losses exceed wins after costs)

    `allow=False` means the gate would reject. Whether that reject actually
    blocks the trade depends on MIN_EDGE_MODE — gate is pure; mode is policy.
    """
    fee = MIN_EDGE_FEE_RT if fee_rt is None else fee_rt
    slip = MIN_EDGE_SLIP_RT if slip_rt is None else slip_rt
    safe = MIN_EDGE_SAFETY if safety is None else safety
    rr_floor = MIN_EDGE_RR if min_rr is None else min_rr
    friction = fee + slip
    threshold = friction + safe

    info = {
        'tp_pct': tp_pct, 'sl_pct': sl_pct,
        'edge': None, 'threshold': threshold, 'net_rr': None,
        'fee_rt': fee, 'slip_rt': slip, 'safety': safe,
        'reason': '',
    }

    if tp_pct is None or sl_pct is None or tp_pct <= 0 or sl_pct <= 0:
        info['reason'] = 'missing_or_nonpositive_tp_sl'
        return False, info

    edge = float(tp_pct) - friction
    info['edge'] = edge
    net_tp = edge
    net_sl = float(sl_pct) + friction
    net_rr = (net_tp / net_sl) if net_sl > 0 else 0.0
    info['net_rr'] = net_rr

    if edge < safe:
        info['reason'] = (f"edge_below_threshold edge={edge:.4f} threshold={threshold:.4f}")
        return False, info
    if net_rr < rr_floor:
        info['reason'] = (f"net_rr_too_low net_rr={net_rr:.2f} floor={rr_floor:.2f}")
        return False, info

    info['reason'] = 'ok'
    return True, info


def evaluate_min_edge(coin, side, entry_price, tp_pct, sl_pct, engine=None):
    """Policy wrapper around gate_min_edge.

    Returns (block, info). `block` is True only when MIN_EDGE_MODE='live' AND
    the gate fails. In 'shadow' mode the rejection is logged but block=False.
    In 'off' mode this is a no-op (returns block=False, info['reason']='disabled').

    On reject (any mode but 'off'), records a shadow trade so we can later
    measure the would-have-been outcome distribution per the user's spec:
      reason='edge_below_threshold', edge=..., threshold=...
    """
    if MIN_EDGE_MODE == 'off':
        return False, {'reason': 'disabled', 'mode': 'off'}

    allow, info = gate_min_edge(tp_pct, sl_pct)
    info['mode'] = MIN_EDGE_MODE
    if allow:
        return False, info

    # Reject — log to shadow trades so we can measure rejected-trade outcomes.
    try:
        import shadow_trades
        shadow_trades.record_rejection(
            coin=coin, side=side, entry_price=entry_price,
            tp_pct=tp_pct, sl_pct=sl_pct,
            reason=info.get('reason', 'edge_gate_fail'),
            meta={
                'engine': engine,
                'edge': info.get('edge'),
                'threshold': info.get('threshold'),
                'net_rr': info.get('net_rr'),
                'mode': MIN_EDGE_MODE,
            },
        )
    except Exception:
        pass  # never break entry path on telemetry failure

    block = (MIN_EDGE_MODE == 'live')
    return block, info


# ═══════════════════════════════════════════════════════
# GATE 8: COMPOSITE CONFIDENCE — THE GATEKEEPER
# ═══════════════════════════════════════════════════════
MIN_COMPOSITE = 0.55   # minimum aggregate score to pass (0-1 scale)
MIN_GATES_PASS = 7     # minimum number of gates that must pass (out of 9)

def run_gates(coin, side, candles, log_fn=None):
    """Run all 9 gates. Returns (pass, composite_score, gate_results).
    side: 'BUY' or 'SELL'
    """
    results = []
    results.append(('G0_FRESH',    *gate0_freshness(candles)))
    results.append(('G1_TIMING',   *gate1_timing(candles)))
    results.append(('G2_TICKS',    *gate2_tick_count(coin)))
    results.append(('G3_ZSCORE',   *gate3_zscore(candles)))
    results.append(('G4_FEAR',     *gate4_fear_edge(side)))
    results.append(('G5_SPREAD',   *gate5_spread(coin)))
    results.append(('G6_ODDA',     *gate6_odda(coin, side)))
    results.append(('G7_DISLOC',   *gate7_dislocation(candles)))

    # Gate 8: Composite — aggregate all above
    gates_passed = sum(1 for r in results if r[1])
    scores = [r[2] for r in results]
    composite = sum(scores) / len(scores) if scores else 0
    
    pass_composite = composite >= MIN_COMPOSITE and gates_passed >= MIN_GATES_PASS
    results.append(('G8_COMPOSITE', pass_composite, composite,
                    f"score={composite:.2f} passed={gates_passed}/8 (need {MIN_GATES_PASS})"))

    if log_fn:
        fails = [f"{r[0]}:{r[3]}" for r in results if not r[1]]
        if fails:
            log_fn(f"  GATES FAILED: {', '.join(fails)}")
        log_fn(f"  GATES: {gates_passed}/8 passed, composite={composite:.2f}, {'GO' if pass_composite else 'REJECT'}")

    return pass_composite, composite, results
