#!/usr/bin/env python3
"""Latency Arbitrage Module — 9 Gates
Exploits HL mark price lag vs Binance real-time trades.
Runs as background thread alongside precog signal loop.
"""
import asyncio, json, time, threading, traceback
import websockets
from collections import deque
from datetime import datetime

# ═══════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════
LA_COINS = ['BTC','ETH','SOL','XRP','DOGE','LINK']  # HIGH LIQUIDITY ONLY — HL lags reliably on these

# Bybit symbol mapping (no geo-block from US servers unlike Binance)
BYBIT_SYMBOLS = {c: c + 'USDT' for c in LA_COINS}
BYBIT_SYMBOLS['PEPE'] = '1000PEPEUSDT'
BYBIT_SYMBOLS['BONK'] = '1000BONKUSDT'

# HL symbol mapping
HL_SYMBOLS = {'PEPE':'kPEPE','BONK':'kBONK'}

# Gate thresholds — TUNED for profit
GATE_0_STALENESS_MS   = 200    # HL price must be >200ms stale (was 300)
GATE_1_WINDOW_MS      = 3000   # 3s entry window (was 2s — more time to validate)
GATE_2_VOL_SIGMA      = 1.5    # Lower vol threshold (was 2.7 — too restrictive)
GATE_3_ZSCORE_MIN     = 1.5    # Lower z-score (was 2.0 — missing opportunities)
GATE_4_FEAR_THRESHOLD = 0.2    # Lower fear bar (was 0.3)
GATE_5_SPREAD_MAX     = 0.0008 # 0.08% max spread (was 0.05% — too tight)
GATE_6_ODDA_MIN       = 2      # 2 trades/sec minimum (was 3)
GATE_7_DISLOCATION    = 0.002  # 0.20% minimum gap (was 0.15% — too small, losing on noise)
GATE_8_COMPOSITE_MIN  = 0.60   # Higher composite threshold (was 0.55)

# Execution — TUNED for profit capture
POSITION_RISK_PCT     = 0.20   # 20% equity per arb (low directional risk — convergence is near-certain)
HOLD_MAX_SEC          = 45     # 45s timeout (was 30 — more time for convergence)
CONVERGENCE_PCT       = 0.0005 # Exit at 0.05% remaining gap
COOLDOWN_SEC          = 10     # 10s between arbs per coin (was 3 — hammering NEAR)
LEV                   = 10     # Leverage

# Binance price buffer
PRICE_WINDOW_MS       = 3000   # 3s window (was 2s — more history for stats)
TICK_BUFFER_SIZE      = 1000   # More ticks (was 500)

# ═══════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════
binance_prices = {}   # {coin: deque of (timestamp_ms, price, volume)}
hl_prices = {}        # {coin: (timestamp_ms, bid, ask, mid)}
arb_cooldowns = {}    # {coin: last_arb_timestamp}
arb_positions = {}    # {coin: {side, entry, hl_entry, opened_at}}
la_log_buffer = []    # Ring buffer for LA-specific logs
la_stats = {'signals':0, 'trades':0, 'wins':0, 'losses':0, 'pnl':0.0}

def la_log(m):
    msg = f"[LA {datetime.utcnow().isoformat()}] {m}"
    print(msg, flush=True)
    la_log_buffer.append(msg)
    if len(la_log_buffer) > 50: la_log_buffer.pop(0)

# ═══════════════════════════════════════════════════════
# GATE 0: DATA FRESHNESS
# ═══════════════════════════════════════════════════════
def gate_0_freshness(coin):
    """Is HL price stale enough to arb? Returns (pass, score, detail)."""
    hl = hl_prices.get(coin)
    if not hl: return False, 0, "no HL price"
    age_ms = time.time() * 1000 - hl[0]
    if age_ms < GATE_0_STALENESS_MS:
        return False, age_ms / GATE_0_STALENESS_MS, f"HL fresh ({age_ms:.0f}ms)"
    score = min(1.0, age_ms / (GATE_0_STALENESS_MS * 3))
    return True, score, f"HL stale ({age_ms:.0f}ms)"

# ═══════════════════════════════════════════════════════
# GATE 1: ENTRY TIMING
# ═══════════════════════════════════════════════════════
def gate_1_timing(detection_ts):
    """Is the window still open? Returns (pass, score, detail)."""
    elapsed_ms = (time.time() - detection_ts) * 1000
    if elapsed_ms > GATE_1_WINDOW_MS:
        return False, 0, f"window closed ({elapsed_ms:.0f}ms)"
    score = 1.0 - (elapsed_ms / GATE_1_WINDOW_MS)
    return True, score, f"window open ({elapsed_ms:.0f}ms)"

# ═══════════════════════════════════════════════════════
# GATE 2: MIN TICK COUNT / VOLATILITY REGIME
# ═══════════════════════════════════════════════════════
def gate_2_volatility(coin):
    """Is volatility high enough? Returns (pass, score, detail)."""
    ticks = binance_prices.get(coin)
    if not ticks or len(ticks) < 20: return False, 0, "insufficient ticks"
    prices = [t[1] for t in ticks]
    mean = sum(prices) / len(prices)
    variance = sum((p - mean)**2 for p in prices) / len(prices)
    std = variance ** 0.5
    if mean == 0: return False, 0, "zero mean"
    cv = std / mean  # coefficient of variation
    # Normalize to sigma scale (approx)
    sigma = cv * 1000  # scale for crypto
    if sigma < GATE_2_VOL_SIGMA:
        return False, sigma / GATE_2_VOL_SIGMA, f"low vol ({sigma:.1f}σ)"
    score = min(1.0, sigma / (GATE_2_VOL_SIGMA * 2))
    return True, score, f"vol OK ({sigma:.1f}σ)"

# ═══════════════════════════════════════════════════════
# GATE 3: Z-SCORE Ψ (STATISTICAL DIVERGENCE)
# ═══════════════════════════════════════════════════════
def gate_3_zscore(coin, binance_px):
    """Is the divergence statistically significant? Returns (pass, score, detail)."""
    hl = hl_prices.get(coin)
    if not hl: return False, 0, "no HL price"
    hl_mid = hl[3]
    ticks = binance_prices.get(coin)
    if not ticks or len(ticks) < 10: return False, 0, "insufficient data"
    # Calculate rolling divergence stats
    divs = [(t[1] - hl_mid) / hl_mid for t in list(ticks)[-50:]]
    mean_div = sum(divs) / len(divs)
    var_div = sum((d - mean_div)**2 for d in divs) / len(divs)
    std_div = max(var_div ** 0.5, 1e-8)
    current_div = (binance_px - hl_mid) / hl_mid
    z = abs(current_div) / std_div
    if z < GATE_3_ZSCORE_MIN:
        return False, z / GATE_3_ZSCORE_MIN, f"z={z:.2f} (need {GATE_3_ZSCORE_MIN})"
    score = min(1.0, z / (GATE_3_ZSCORE_MIN * 2))
    return True, score, f"z={z:.2f} ✓"

# ═══════════════════════════════════════════════════════
# GATE 4: FEAR-ADJUSTED EDGE
# ═══════════════════════════════════════════════════════
def gate_4_fear(coin, get_funding_fn=None):
    """Market conditions favorable? Returns (pass, score, detail)."""
    ticks = binance_prices.get(coin)
    if not ticks or len(ticks) < 5: return True, 0.5, "neutral (no data)"
    # Volume surge = fear/urgency = bigger dislocations
    recent_vol = sum(t[2] for t in list(ticks)[-10:])
    older_vol = sum(t[2] for t in list(ticks)[-50:-10]) / 4 if len(ticks) > 50 else recent_vol
    vol_ratio = recent_vol / max(older_vol, 1e-8)
    fear = min(1.0, vol_ratio / 5.0)  # 5x volume = max fear
    return True, fear, f"fear={fear:.2f} (vol ratio {vol_ratio:.1f}x)"

# ═══════════════════════════════════════════════════════
# GATE 5: SPREAD FILTER
# ═══════════════════════════════════════════════════════
def gate_5_spread(coin):
    """Is HL spread tight enough? Returns (pass, score, detail)."""
    hl = hl_prices.get(coin)
    if not hl: return False, 0, "no HL price"
    bid, ask = hl[1], hl[2]
    if bid <= 0 or ask <= 0: return False, 0, "invalid spread"
    spread = (ask - bid) / ((ask + bid) / 2)
    if spread > GATE_5_SPREAD_MAX:
        return False, 1.0 - (spread / GATE_5_SPREAD_MAX), f"spread wide ({spread*100:.3f}%)"
    score = 1.0 - (spread / GATE_5_SPREAD_MAX)
    return True, score, f"spread tight ({spread*100:.3f}%)"

# ═══════════════════════════════════════════════════════
# GATE 6: ODDA VELOCITY (Order/Data Arrival Speed)
# ═══════════════════════════════════════════════════════
def gate_6_odda(coin):
    """How fast is Binance moving? Returns (pass, score, detail)."""
    ticks = binance_prices.get(coin)
    if not ticks or len(ticks) < 5: return False, 0, "no ticks"
    now_ms = time.time() * 1000
    recent = [t for t in ticks if now_ms - t[0] < 1000]  # last 1 second
    tps = len(recent)  # trades per second
    if tps < GATE_6_ODDA_MIN:
        return False, tps / GATE_6_ODDA_MIN, f"slow flow ({tps} t/s)"
    score = min(1.0, tps / (GATE_6_ODDA_MIN * 5))
    return True, score, f"fast flow ({tps} t/s)"

# ═══════════════════════════════════════════════════════
# GATE 7: DISLOCATION CHECK (THE CORE SIGNAL)
# ═══════════════════════════════════════════════════════
def gate_7_dislocation(coin, binance_px):
    """Confirmed price gap? Returns (pass, score, direction, detail)."""
    hl = hl_prices.get(coin)
    if not hl: return False, 0, None, "no HL price"
    hl_mid = hl[3]
    gap = (binance_px - hl_mid) / hl_mid
    abs_gap = abs(gap)
    if abs_gap < GATE_7_DISLOCATION:
        return False, abs_gap / GATE_7_DISLOCATION, None, f"gap {gap*100:.3f}% < {GATE_7_DISLOCATION*100:.1f}%"
    direction = 'BUY' if gap > 0 else 'SELL'  # Binance higher = HL will catch up = BUY on HL
    score = min(1.0, abs_gap / (GATE_7_DISLOCATION * 3))
    return True, score, direction, f"GAP {gap*100:.3f}% → {direction}"

# ═══════════════════════════════════════════════════════
# GATE 8: COMPOSITE SCORE
# ═══════════════════════════════════════════════════════
GATE_WEIGHTS = [0.10, 0.08, 0.10, 0.20, 0.07, 0.12, 0.10, 0.23]
# Weights:  G0    G1    G2    G3    G4    G5    G6    G7
# Z-score (G3) and dislocation (G7) weighted highest — they're the edge

def gate_8_composite(coin, binance_px, detection_ts, get_funding_fn=None):
    """Run all gates, compute composite score. Returns (fire, direction, score, details)."""
    g0_pass, g0_score, g0_detail = gate_0_freshness(coin)
    g1_pass, g1_score, g1_detail = gate_1_timing(detection_ts)
    g2_pass, g2_score, g2_detail = gate_2_volatility(coin)
    g3_pass, g3_score, g3_detail = gate_3_zscore(coin, binance_px)
    g4_pass, g4_score, g4_detail = gate_4_fear(coin, get_funding_fn)
    g5_pass, g5_score, g5_detail = gate_5_spread(coin)
    g6_pass, g6_score, g6_detail = gate_6_odda(coin)
    g7_pass, g7_score, g7_dir, g7_detail = gate_7_dislocation(coin, binance_px)

    scores = [g0_score, g1_score, g2_score, g3_score, g4_score, g5_score, g6_score, g7_score]
    composite = sum(s * w for s, w in zip(scores, GATE_WEIGHTS))

    # Hard gates: G7 (dislocation) MUST pass. G5 (spread) MUST pass.
    hard_pass = g7_pass and g5_pass
    fire = hard_pass and composite >= GATE_8_COMPOSITE_MIN

    details = {
        'G0_fresh': g0_detail, 'G1_timing': g1_detail, 'G2_vol': g2_detail,
        'G3_zscore': g3_detail, 'G4_fear': g4_detail, 'G5_spread': g5_detail,
        'G6_odda': g6_detail, 'G7_disloc': g7_detail,
        'composite': round(composite, 3), 'fire': fire
    }
    return fire, g7_dir, composite, details

# ═══════════════════════════════════════════════════════
# BINANCE WEBSOCKET — REAL-TIME TRADE STREAM
# ═══════════════════════════════════════════════════════
async def binance_ws_handler():
    """Connect to Bybit public trade stream for all LA coins (no geo-block)."""
    url = "wss://stream.bybit.com/v5/public/linear"
    la_log(f"Connecting to Bybit WS: {len(LA_COINS)} coins")

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                # Subscribe to all coin trade streams
                topics = [f"publicTrade.{BYBIT_SYMBOLS[c]}" for c in LA_COINS]
                # Bybit allows max 10 per subscribe, batch them
                for i in range(0, len(topics), 10):
                    batch = topics[i:i+10]
                    sub = json.dumps({"op": "subscribe", "args": batch})
                    await ws.send(sub)
                la_log(f"Bybit WS connected, subscribed to {len(topics)} streams")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        topic = msg.get('topic', '')
                        if not topic.startswith('publicTrade.'): continue
                        symbol = topic.replace('publicTrade.', '')

                        # Map back to our coin name
                        coin = None
                        for c, s in BYBIT_SYMBOLS.items():
                            if s == symbol: coin = c; break
                        if not coin: continue

                        for trade in msg.get('data', []):
                            px = float(trade.get('p', 0))
                            qty = float(trade.get('v', 0))
                            ts_ms = trade.get('T', time.time() * 1000)

                            if coin not in binance_prices:
                                binance_prices[coin] = deque(maxlen=TICK_BUFFER_SIZE)
                            binance_prices[coin].append((ts_ms, px, qty))

                            # Prune old ticks
                            now_ms = time.time() * 1000
                            while binance_prices[coin] and now_ms - binance_prices[coin][0][0] > PRICE_WINDOW_MS:
                                binance_prices[coin].popleft()

                    except Exception as e:
                        pass  # Silently skip bad messages
        except Exception as e:
            la_log(f"Bybit WS error: {e}, reconnecting in 5s")
            await asyncio.sleep(5)

# ═══════════════════════════════════════════════════════
# ARB SCANNER — detects dislocations and fires trades
# ═══════════════════════════════════════════════════════
def scan_arb_opportunities(get_mid_fn, place_fn, close_fn, get_balance_fn, get_funding_fn, log_fn):
    """Called from main loop. Scans all coins for arb opportunities.
    
    Args: HL helper functions passed from precog.py
    """
    now = time.time()

    # First: manage open arb positions (convergence exit / timeout)
    for coin in list(arb_positions.keys()):
        pos = arb_positions[coin]
        age = now - pos['opened_at']

        # Get current HL price
        hl_px = get_mid_fn(HL_SYMBOLS.get(coin, coin))
        if not hl_px: continue

        entry = pos['hl_entry']
        side = pos['side']
        pnl_pct = (hl_px - entry) / entry if side == 'BUY' else (entry - hl_px) / entry

        # Convergence exit: gap has closed AND we're in profit
        bn_ticks = binance_prices.get(coin)
        bn_px = bn_ticks[-1][1] if bn_ticks else hl_px
        current_gap = abs(bn_px - hl_px) / hl_px

        # Must be at least +0.05% profit to exit on convergence (covers fees)
        min_profit = 0.0005
        profitable = pnl_pct > min_profit

        if (current_gap < CONVERGENCE_PCT and profitable) or age > HOLD_MAX_SEC:
            if age > HOLD_MAX_SEC:
                reason = f"timeout ({age:.0f}s) pnl={pnl_pct*100:+.3f}%"
            else:
                reason = f"converged ({current_gap*100:.3f}%) pnl={pnl_pct*100:+.3f}%"
            pnl = close_fn(HL_SYMBOLS.get(coin, coin))
            if pnl is not None:
                la_stats['pnl'] += pnl_pct
                if pnl_pct > 0: la_stats['wins'] += 1
                else: la_stats['losses'] += 1
            la_log(f"ARB CLOSE {coin} {reason} pnl={pnl_pct*100:+.3f}%")
            del arb_positions[coin]

    # Scan for new arb opportunities
    for coin in LA_COINS:
        if coin in arb_positions: continue  # already in arb position
        if now - arb_cooldowns.get(coin, 0) < COOLDOWN_SEC: continue

        ticks = binance_prices.get(coin)
        if not ticks or len(ticks) < 5: continue

        # Latest Binance price
        bn_px = ticks[-1][1]

        # Update HL price (with timestamp)
        hl_coin = HL_SYMBOLS.get(coin, coin)
        hl_mid = get_mid_fn(hl_coin)
        if hl_mid:
            hl_prices[coin] = (time.time() * 1000, hl_mid * 0.9999, hl_mid * 1.0001, hl_mid)

        # Quick pre-check: is there even a gap worth scoring?
        hl = hl_prices.get(coin)
        if not hl: continue
        quick_gap = abs(bn_px - hl[3]) / hl[3]
        if quick_gap < GATE_7_DISLOCATION * 0.5: continue  # not even close

        # Run 9 gates
        detection_ts = time.time()
        fire, direction, composite, details = gate_8_composite(
            coin, bn_px, detection_ts, get_funding_fn
        )
        la_stats['signals'] += 1

        if not fire: continue

        # FIRE — execute arb trade on HL
        la_log(f"ARB FIRE {coin} {direction} | composite={composite:.3f} | gap={quick_gap*100:.3f}%")
        la_log(f"  Gates: {json.dumps(details)}")

        try:
            equity = get_balance_fn()
            size = equity * POSITION_RISK_PCT * LEV / hl[3]
            is_buy = (direction == 'BUY')
            fill = place_fn(hl_coin, is_buy, size)
            if fill:
                arb_positions[coin] = {
                    'side': direction, 'entry': bn_px,
                    'hl_entry': fill, 'opened_at': time.time()
                }
                arb_cooldowns[coin] = time.time()
                la_stats['trades'] += 1
                la_log(f"ARB OPEN {coin} {direction} @ {fill} (bn={bn_px})")
        except Exception as e:
            la_log(f"ARB EXEC ERR {coin}: {e}")

# ═══════════════════════════════════════════════════════
# THREAD ENTRY POINTS
# ═══════════════════════════════════════════════════════
def start_binance_ws():
    """Start Binance websocket in asyncio event loop (run in thread)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(binance_ws_handler())

def start_la_module(get_mid_fn, place_fn, close_fn, get_balance_fn, get_funding_fn, log_fn):
    """Start the full LA module: Binance WS + arb scanner.
    
    Call from precog.py:
        from latency_arb import start_la_module
        start_la_module(get_mid, place, close, get_balance, get_funding_rate, log)
    """
    # Start Binance websocket thread
    ws_thread = threading.Thread(target=start_binance_ws, daemon=True)
    ws_thread.start()
    la_log(f"LA module started: {len(LA_COINS)} coins, composite_min={GATE_8_COMPOSITE_MIN}")

    # Arb scanner loop (runs in same thread as caller, or pass to precog main loop)
    while True:
        try:
            scan_arb_opportunities(get_mid_fn, place_fn, close_fn, get_balance_fn, get_funding_fn, log_fn)
        except Exception as e:
            la_log(f"LA scan err: {e}\n{traceback.format_exc()}")
        time.sleep(0.1)  # 100ms scan interval — fast enough for LA

def get_la_status():
    """Return LA module status for /health endpoint."""
    return {
        'la_active': True,
        'coins': len(LA_COINS),
        'binance_feeds': len(binance_prices),
        'open_arbs': len(arb_positions),
        'stats': la_stats.copy(),
        'recent_logs': la_log_buffer[-10:]
    }
