"""Signal confidence scoring. Returns 0-100 based on 8 factors.
Used as SIZING multiplier, not filter. Every signal trades.
OOS: higher scores = higher WR + higher avg_pnl/trade.
"""
import numpy as np

def ema_arr(arr, period):
    e = np.full(len(arr), np.nan); k = 2/(period+1)
    if len(arr) < period: return e
    e[period-1] = arr[:period].mean()
    for i in range(period, len(arr)):
        e[i] = arr[i]*k + e[i-1]*(1-k)
    return e

def score(candles5, candles4h, coin, side, btc_dir):
    """Compute 0-100 confidence score for a signal.
    candles5: list of [ts,o,h,l,c,v] (most recent last)
    candles4h: list of [ts,o,h,l,c,v]
    side: 'BUY' or 'SELL'
    btc_dir: +1/-1/0
    Returns (total, breakdown_dict)
    """
    breakdown = {}
    total = 0
    if len(candles5) < 50: return 50, {'insufficient': True}
    try:
        arr5 = np.array(candles5)
        c5 = arr5[:,4].astype(float); h5 = arr5[:,2].astype(float); l5 = arr5[:,3].astype(float); v5 = arr5[:,5].astype(float)
        rsi14 = _rsi(c5, 14)
        e9_5 = ema_arr(c5, 9)
        # 1H EMA20
        c1h = []; 
        for i in range(0, len(c5)-11, 12): c1h.append(c5[i+11])
        c1h = np.array(c1h); ema1h_20 = ema_arr(c1h, 20) if len(c1h)>=20 else None
        # 4H EMA9
        e4h_9 = None
        if candles4h and len(candles4h) >= 10:
            c4h = np.array([float(x[4]) for x in candles4h])
            e4h_9 = ema_arr(c4h, 9)
        i = len(c5)-1
        price = c5[i]

        # V3 trend (20pts)
        if e4h_9 is not None and not np.isnan(e4h_9[-1]):
            e4 = e4h_9[-1]
            c4_last = float(candles4h[-1][4])
            if side == 'BUY' and c4_last > e4 * 1.005: total += 20; breakdown['v3'] = 20
            elif side == 'SELL' and c4_last < e4 * 0.995: total += 20; breakdown['v3'] = 20
            elif abs(c4_last - e4)/e4 < 0.005: total += 10; breakdown['v3'] = 10

        # 1H pullback proximity (15pts)
        if ema1h_20 is not None and len(ema1h_20)>0 and not np.isnan(ema1h_20[-1]):
            e1h = ema1h_20[-1]
            dist = abs(price - e1h)/e1h if e1h > 0 else 1
            if dist < 0.003: total += 15; breakdown['pb'] = 15
            elif dist < 0.006: total += 7; breakdown['pb'] = 7

        # 5m momentum (10pts)
        if not np.isnan(e9_5[i]) and i > 0 and not np.isnan(e9_5[i-1]):
            ema_up = e9_5[i] > e9_5[i-1]
            if side == 'BUY' and price > e9_5[i] and ema_up: total += 10; breakdown['mom5'] = 10
            elif side == 'SELL' and price < e9_5[i] and not ema_up: total += 10; breakdown['mom5'] = 10
            elif (side == 'BUY' and price > e9_5[i]) or (side == 'SELL' and price < e9_5[i]):
                total += 5; breakdown['mom5'] = 5

        # BTC correlation (15pts)
        if coin in ('BTC','ETH'):
            total += 15; breakdown['btc'] = 15  # majors exempt
        elif btc_dir == 0:
            total += 8; breakdown['btc'] = 8
        elif (side == 'BUY' and btc_dir == 1) or (side == 'SELL' and btc_dir == -1):
            total += 15; breakdown['btc'] = 15

        # RSI depth (10pts)
        r = rsi14[i] if not np.isnan(rsi14[i]) else 50
        if side == 'SELL' and r > 75: total += 10; breakdown['rsi'] = 10
        elif side == 'SELL' and r > 72: total += 5; breakdown['rsi'] = 5
        elif side == 'BUY' and r < 30: total += 10; breakdown['rsi'] = 10
        elif side == 'BUY' and r < 33: total += 5; breakdown['rsi'] = 5

        # Volume confirmation (10pts)
        if i >= 20:
            vol_avg = v5[i-20:i].mean()
            if vol_avg > 0:
                vr = v5[i] / vol_avg
                if vr > 2: total += 10; breakdown['vol'] = 10
                elif vr > 1.5: total += 6; breakdown['vol'] = 6
                elif vr > 1.1: total += 3; breakdown['vol'] = 3

        # ATR ratio (10pts)
        if i >= 14:
            trs = []
            for j in range(1, 15):
                h_j = h5[i-j]; l_j = l5[i-j]; pc = c5[i-j-1] if i-j-1 >= 0 else c5[i-j]
                trs.append(max(h_j-l_j, abs(h_j-pc), abs(l_j-pc)))
            atr = sum(trs)/len(trs)
            atr_pct = atr/price if price > 0 else 0
            if atr_pct > 0.005: total += 10; breakdown['atr'] = 10
            elif atr_pct > 0.003: total += 5; breakdown['atr'] = 5
            elif atr_pct > 0.002: total += 2; breakdown['atr'] = 2

        # Short-term momentum (10pts)
        if i >= 3:
            mom = (c5[i] - c5[i-3])/c5[i-3] if c5[i-3] > 0 else 0
            if side == 'BUY' and mom > 0.001: total += 10; breakdown['mom3'] = 10
            elif side == 'SELL' and mom < -0.001: total += 10; breakdown['mom3'] = 10
            elif abs(mom) < 0.0005: total += 5; breakdown['mom3'] = 5

    except Exception as e:
        return 50, {'err': str(e)}
    return total, breakdown

def _rsi(c, p=14):
    d = np.diff(c); g = np.maximum(d, 0); lo = np.maximum(-d, 0)
    ag = np.full(len(c), np.nan); al = np.full(len(c), np.nan)
    if len(c) <= p: return ag
    ag[p] = g[:p].mean(); al[p] = lo[:p].mean()
    for i in range(p+1, len(c)):
        ag[i] = (ag[i-1]*(p-1) + g[i-1]) / p
        al[i] = (al[i-1]*(p-1) + lo[i-1]) / p
    return 100 - 100 / (1 + ag / np.where(al == 0, 1e-10, al))

def size_multiplier(score_val):
    """CONVICTION MODE: block low-conviction, scale up steeply at high conviction.

    At $710-$5k equity, 0.2x probe trades result in $10-30 notional positions.
    Even a perfect 2% TP on those is $0.20-$0.60 profit — below fee threshold
    and well inside market noise. No edge can accumulate from trades this small.

    Old dial (<30 → 0.2x) let every signal trade. New dial blocks <40 entirely
    and sizes remaining trades for meaningful $ impact per outcome.

    <40:  0.0x (BLOCKED — not worth fee/slippage/mental-capital overhead)
    40-49: 1.0x (baseline — acceptable conviction, full base size)
    50-64: 2.0x (meaningful — clear signal agreement across components)
    65-79: 3.5x (high — multi-component confluence)
    80+:   5.0x (conviction max — hard cap 15% equity at SL)

    With $710 equity × 3% base risk:
       baseline   (1.0x) = $21 risk  → ~$1050 notional at 2% SL  → $21/trade at 2% TP
       meaningful (2.0x) = $42 risk  → ~$2100 notional           → $42/trade
       high       (3.5x) = $74 risk  → ~$3700 notional           → $74/trade
       max        (5.0x) = $107 risk → ~$5300 notional           → $107/trade

    Returning 0.0 signals process() to skip — the explicit conviction-floor
    check must also exist at the caller (calc_size returning 0 would attempt
    a 0-size order which HL rejects noisily)."""
    if score_val < 40: return 0.0   # BLOCKED
    if score_val < 50: return 1.0
    if score_val < 65: return 2.0
    if score_val < 80: return 3.5
    return 5.0
