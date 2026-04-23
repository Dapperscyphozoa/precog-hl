"""Top-K ensemble voter — activates stored top-K=3 ensemble configs.

Currently PreCog fires on top-1 config per coin/regime. The enterprise grid
stores top-K=3. This module evaluates all K configs and returns:

- unanimous (3/3 same direction) → boost size 1.3x, flag high-conviction
- majority (2/3 same direction) → normal size
- split (1/3 or 0) → reduce size 0.5x OR skip (if skip_on_split=True)

Each ensemble config has its own engine/RH/RL/TP/SL. We evaluate whether
each would fire on the current bar. Voting is on agreement, not on engine
type.

Zero new data — uses existing regime_configs.py top-K storage.
"""
import json, os, time, threading
import numpy as np

_LOG_PREFIX = '[ensemble]'


def _rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    d = np.diff(closes)
    g = np.where(d > 0, d, 0); l = np.where(d < 0, -d, 0)
    ag = g[:period].mean(); al = l[:period].mean()
    for i in range(period, len(g)):
        ag = (ag * (period-1) + g[i]) / period
        al = (al * (period-1) + l[i]) / period
    if al == 0: return 100.0
    return 100 - 100 / (1 + ag / al)


def _would_fire(engine, rh, rl, closes, highs, lows, lookback_period=20):
    """Simplified engine evaluation — returns 'BUY', 'SELL', or None."""
    if len(closes) < max(lookback_period + 2, 20): return None
    price = closes[-1]
    rsi_now = _rsi(closes)

    if engine == 'BB':
        window = closes[-lookback_period:]
        mean = float(np.mean(window)); std = float(np.std(window))
        lower = mean - 2 * std; upper = mean + 2 * std
        if price < lower and rsi_now < rl: return 'BUY'
        if price > upper and rsi_now > rh: return 'SELL'
    elif engine == 'PV':
        # pivot reversal at last_5 bars
        lb = 5
        if lows[-lb-1] == min(lows[-2*lb:]) and rsi_now < rl and price > closes[-lb-1]: return 'BUY'
        if highs[-lb-1] == max(highs[-2*lb:]) and rsi_now > rh and price < closes[-lb-1]: return 'SELL'
    elif engine == 'MR':
        window = closes[-lookback_period:]
        mean = float(np.mean(window)); std = float(np.std(window))
        if std == 0: return None
        z = (price - mean) / std
        if z < -2.0 and rsi_now < rl: return 'BUY'
        if z > 2.0 and rsi_now > rh: return 'SELL'
    elif engine == 'IB':
        if len(highs) < 3: return None
        prev_h, prev_l = highs[-2], lows[-2]
        parent_h, parent_l = highs[-3], lows[-3]
        if prev_h < parent_h and prev_l > parent_l:
            if price > prev_h and rsi_now < rh: return 'BUY'
            if price < prev_l and rsi_now > rl: return 'SELL'
    elif engine == 'VS':
        window = closes[-lookback_period:]
        mean = float(np.mean(window)); std = float(np.std(window))
        if price > mean + 2 * std and rsi_now < rh: return 'BUY'
        if price < mean - 2 * std and rsi_now > rl: return 'SELL'
    elif engine == 'TR':
        if len(closes) < 60: return None
        # EMA20 > EMA50 + N-bar breakout
        def ema(vals, period):
            k = 2 / (period + 1); e = sum(vals[:period]) / period
            for v in vals[period:]: e = v * k + e * (1 - k)
            return e
        e20 = ema(closes, 20); e50 = ema(closes, 50)
        if e20 > e50 and price > max(highs[-20:-1]): return 'BUY'
        if e20 < e50 and price < min(lows[-20:-1]): return 'SELL'
    return None


def vote(coin, candles, coin_side_intent, ensemble_list):
    """Evaluate all ensemble configs, return voting result.

    candles: list of [t,o,h,l,c,v]
    coin_side_intent: the side the top-1 config is recommending ('BUY'/'SELL')
    ensemble_list: list of top-K config dicts from regime_configs.get_ensemble()

    Returns: {decision, size_multiplier, flags, votes_by_config}
    """
    if not ensemble_list or len(ensemble_list) == 0:
        return {'decision': 'no_ensemble', 'size_multiplier': 1.0}
    if len(candles) < 30:
        return {'decision': 'insufficient_bars', 'size_multiplier': 1.0}

    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]

    votes = []
    for cfg in ensemble_list:
        engine = cfg.get('sigs', [cfg.get('engine', 'BB')])[0] if 'sigs' in cfg else cfg.get('engine', 'BB')
        rh = cfg.get('RH') or cfg.get('rh') or 70
        rl = cfg.get('RL') or cfg.get('rl') or 30
        side = _would_fire(engine, rh, rl, closes, highs, lows)
        votes.append({'engine': engine, 'fired_side': side, 'matches_intent': side == coin_side_intent})

    fire_intent_count = sum(1 for v in votes if v['matches_intent'])
    total = len(votes)

    if total == 0:
        return {'decision': 'no_votes', 'size_multiplier': 1.0, 'votes': votes}

    if fire_intent_count == total:
        return {
            'decision': 'unanimous_confirmation',
            'size_multiplier': 1.3,
            'confirming_votes': fire_intent_count,
            'total_votes': total,
            'flag': 'high_conviction',
            'votes': votes,
        }
    elif fire_intent_count >= total * 0.5:
        return {
            'decision': 'majority_confirmation',
            'size_multiplier': 1.0,
            'confirming_votes': fire_intent_count,
            'total_votes': total,
            'votes': votes,
        }
    else:
        return {
            'decision': 'minority_confirmation',
            'size_multiplier': 0.5,
            'confirming_votes': fire_intent_count,
            'total_votes': total,
            'flag': 'weak_conviction',
            'votes': votes,
        }
