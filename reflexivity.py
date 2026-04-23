"""Reflexivity detector — silent telemetry.

At every signal fire, scores:
1. Crowding: volume spike vs 20-bar median + funding rate extreme
2. Move position: distance from nearest swing low/high, normalized by ATR
3. Reaction-to-reaction: ratio of prior-4-bars return to current bar return

Combined reflexivity_risk maps to recommendation (LEAD/FOLLOW/SKEPTICAL/AVOID)
— logged only, not applied to trading until activation.

Trigger: 75 closed outcomes → compare LEAD vs AVOID bucket WR.
If LEAD outperforms AVOID by >=0.25R: activate live filter.
"""
import json, os, time, threading, urllib.request, math

LOG_PATH = os.environ.get('REFLEX_LOG_PATH', '/app/reflexivity.jsonl')
MAX_LINES = 20_000
TRIGGER_THRESHOLD = 75
_LOCK = threading.Lock()
_LOG_PREFIX = '[reflex]'
_TRIGGER_FIRED = False

# Funding rate cache (per coin). Refreshed on demand.
_FUNDING_CACHE = {}
_FUNDING_CACHE_TTL = 600  # 10 min


def _fetch_funding(coin):
    """Fetch latest funding rate for a coin. Cached 10min. Returns decimal, default 0."""
    now = time.time()
    cached = _FUNDING_CACHE.get(coin)
    if cached and (now - cached['ts']) < _FUNDING_CACHE_TTL:
        return cached['rate']
    try:
        body = json.dumps({'type': 'metaAndAssetCtxs'}).encode()
        req = urllib.request.Request('https://api.hyperliquid.xyz/info',
            data=body, method='POST',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read())
        if len(data) >= 2:
            meta, ctxs = data[0], data[1]
            universe = meta.get('universe', [])
            for i, u in enumerate(universe):
                if u.get('name') == coin and i < len(ctxs):
                    rate = float(ctxs[i].get('funding', 0) or 0)
                    _FUNDING_CACHE[coin] = {'rate': rate, 'ts': now}
                    return rate
    except Exception:
        pass
    _FUNDING_CACHE[coin] = {'rate': 0, 'ts': now}
    return 0


def _fetch_recent_bars(coin, n_bars=30, interval='15m'):
    """Fetch last N 15m bars for scoring. Cached for 60s per coin."""
    ms_per_bar = {'15m': 900_000}[interval]
    end = int(time.time() * 1000)
    start = end - n_bars * ms_per_bar
    body = json.dumps({
        'type': 'candleSnapshot',
        'req': {'coin': coin, 'interval': interval,
                'startTime': start, 'endTime': end}
    }).encode()
    req = urllib.request.Request('https://api.hyperliquid.xyz/info',
        data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return [(int(b['t']), float(b['o']), float(b['h']), float(b['l']),
                 float(b['c']), float(b['v'])) for b in data]
    except Exception:
        return []


# Cache BTC bars separately (shared across all coin scorings within 60s)
_BTC_BARS_CACHE = {'ts': 0, 'bars': []}


def _get_btc_bars():
    now = time.time()
    if now - _BTC_BARS_CACHE['ts'] < 60 and _BTC_BARS_CACHE['bars']:
        return _BTC_BARS_CACHE['bars']
    bars = _fetch_recent_bars('BTC', n_bars=8)
    _BTC_BARS_CACHE['bars'] = bars
    _BTC_BARS_CACHE['ts'] = now
    return bars


def _compute_atr(bars, period=14):
    if len(bars) < period + 1: return 0
    trs = []
    for i in range(1, len(bars)):
        hi, lo = bars[i][2], bars[i][3]
        prev_c = bars[i-1][4]
        tr = max(hi - lo, abs(hi - prev_c), abs(lo - prev_c))
        trs.append(tr)
    if len(trs) < period: return 0
    return sum(trs[-period:]) / period


def _find_recent_swing(bars, side, lookback=20):
    """Find nearest recent swing low (for BUY) or high (for SELL).

    Returns (swing_price, bars_ago). None if no clear swing.
    """
    if len(bars) < lookback + 3: return None
    recent = bars[-lookback:]
    if side == 'BUY':
        # lowest low in recent
        lows = [(b[3], len(bars) - 1 - i) for i, b in enumerate(recent)]
        lows.sort()
        return lows[0]  # (price, bars_ago from current)
    else:
        highs = [(b[2], len(bars) - 1 - i) for i, b in enumerate(recent)]
        highs.sort(reverse=True)
        return highs[0]


def score(coin, side, current_price):
    """Compute reflexivity scores for a signal fire.

    Returns dict with crowding, move_position, reaction, risk, recommendation.
    """
    bars = _fetch_recent_bars(coin, n_bars=30)
    if len(bars) < 20:
        return {'risk': None, 'reason': 'insufficient_bars'}

    # ─── 1. Crowding ───
    vols = [b[5] for b in bars[:-1]]
    median_vol = sorted(vols)[len(vols) // 2] if vols else 1
    current_vol = bars[-1][5]
    vol_ratio = current_vol / median_vol if median_vol > 0 else 1.0
    # Map vol_ratio to 0-1. 1x=0.2, 2x=0.5, 4x+=0.9
    vol_crowd = min(1.0, max(0, (vol_ratio - 0.5) / 3.5))

    # Funding extreme
    funding = _fetch_funding(coin)
    # |funding| 0.05% = 0.5 score, 0.15% = 1.0 score
    funding_crowd = min(1.0, abs(funding) / 0.0015)

    crowding = 0.6 * vol_crowd + 0.4 * funding_crowd

    # ─── 2. Move position (distance from swing) ───
    atr = _compute_atr(bars, 14)
    swing = _find_recent_swing(bars, side, lookback=20)
    if atr > 0 and swing:
        swing_price, _bars_ago = swing
        distance = abs(current_price - swing_price)
        distance_atr = distance / atr if atr > 0 else 0
    else:
        distance_atr = 0.5  # unknown — default to mid
    # Map distance to 0-1. <0.3 ATR=0.1, 0.5=0.3, 1.0=0.6, 1.5+=0.9
    move_position = min(1.0, max(0, (distance_atr - 0.2) / 1.3))

    # ─── 3. Reaction-to-reaction (BTC echo) ───
    btc_bars = _get_btc_bars()
    reaction_score = 0.3  # default moderate
    if len(btc_bars) >= 5:
        current_btc_ret = abs(btc_bars[-1][4] / btc_bars[-2][4] - 1) if btc_bars[-2][4] > 0 else 0
        prior_btc_ret = abs(btc_bars[-2][4] / btc_bars[-6][4] - 1) if btc_bars[-6][4] > 0 else 0
        # If prior 4-bar move was much bigger than current bar, we're echoing
        if current_btc_ret > 0:
            ratio = prior_btc_ret / current_btc_ret
            # ratio 1.0 = 0.3, 3.0 = 0.6, 6.0+ = 0.9 — we're trading an echo
            reaction_score = min(1.0, max(0, 0.1 + ratio / 10))
        elif prior_btc_ret > 0.01:  # prior move existed, current is flat
            reaction_score = 0.7  # trading into exhaustion

    # ─── Combined risk ───
    risk = 0.4 * crowding + 0.3 * move_position + 0.3 * reaction_score

    # Recommendation
    if risk < 0.3: rec = 'LEAD'
    elif risk < 0.5: rec = 'FOLLOW'
    elif risk < 0.7: rec = 'SKEPTICAL'
    else: rec = 'AVOID'

    return {
        'risk': round(risk, 3),
        'recommendation': rec,
        'crowding': round(crowding, 3),
        'move_position': round(move_position, 3),
        'reaction_score': round(reaction_score, 3),
        'vol_ratio': round(vol_ratio, 2),
        'funding_rate': round(funding, 5),
        'distance_atr': round(distance_atr, 2),
    }


def log_signal_score(coin, side, current_price, engine=None, bar_ts=None):
    """Log reflexivity score at signal fire. Non-blocking."""
    def _do():
        try:
            s = score(coin, side, current_price)
            rec = {
                'type': 'signal',
                'ts': int(time.time()),
                'bar_ts': int(bar_ts) if bar_ts else int(time.time()),
                'coin': coin,
                'side': side,
                'engine': engine,
                'price': current_price,
                **s,
            }
            with _LOCK:
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
        except Exception as e:
            print(f"{_LOG_PREFIX} score err {coin}: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def log_outcome(coin, pnl_pct, win, bar_ts=None):
    """Pair outcome with signal score from log."""
    def _do():
        try:
            rec = {
                'type': 'outcome',
                'ts': int(time.time()),
                'bar_ts': int(bar_ts) if bar_ts else None,
                'coin': coin,
                'pnl_pct': round(float(pnl_pct), 3),
                'win': bool(win),
            }
            with _LOCK:
                with open(LOG_PATH, 'a') as f:
                    f.write(json.dumps(rec, default=str) + '\n')
                _check_trigger()
        except Exception as e:
            print(f"{_LOG_PREFIX} outcome err: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def _check_trigger():
    global _TRIGGER_FIRED
    if _TRIGGER_FIRED: return
    try:
        if not os.path.exists(LOG_PATH): return
        n = 0
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    if json.loads(line).get('type') == 'outcome': n += 1
                except Exception: continue
        if n >= TRIGGER_THRESHOLD:
            _TRIGGER_FIRED = True
            print(f"{_LOG_PREFIX} ★★★ TRIGGER: {n} outcomes. "
                  f"Ready for LEAD vs AVOID bucket analysis + activation decision. ★★★",
                  flush=True)
    except Exception:
        pass


def get_stats():
    """Bucket WR + avg PnL by recommendation."""
    if not os.path.exists(LOG_PATH):
        return {'total_signals': 0, 'total_outcomes': 0,
                'trigger_threshold': TRIGGER_THRESHOLD, 'trigger_fired': False}

    # Pair signals with outcomes by (coin, bar_ts)
    signals = {}
    buckets = {'LEAD': {'n':0,'wins':0,'pnl_sum':0},
               'FOLLOW': {'n':0,'wins':0,'pnl_sum':0},
               'SKEPTICAL': {'n':0,'wins':0,'pnl_sum':0},
               'AVOID': {'n':0,'wins':0,'pnl_sum':0}}
    n_signals = 0
    n_outcomes = 0

    try:
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception: continue
                if r.get('type') == 'signal':
                    n_signals += 1
                    key = (r.get('coin'), r.get('bar_ts'))
                    signals[key] = r.get('recommendation', 'UNKNOWN')
                elif r.get('type') == 'outcome':
                    n_outcomes += 1
                    key = (r.get('coin'), r.get('bar_ts'))
                    rec = signals.get(key)
                    if rec and rec in buckets:
                        b = buckets[rec]
                        b['n'] += 1
                        if r.get('win'): b['wins'] += 1
                        b['pnl_sum'] += r.get('pnl_pct', 0)
    except Exception as e:
        return {'error': str(e)}

    for name, b in buckets.items():
        if b['n']:
            b['wr'] = round(b['wins'] / b['n'], 3)
            b['avg_pnl'] = round(b['pnl_sum'] / b['n'], 3)

    return {
        'total_signals': n_signals,
        'total_outcomes': n_outcomes,
        'trigger_threshold': TRIGGER_THRESHOLD,
        'trigger_fired': _TRIGGER_FIRED,
        'by_recommendation': buckets,
        'file_size_kb': round(os.path.getsize(LOG_PATH) / 1024, 1),
    }


def trim_log():
    try:
        if not os.path.exists(LOG_PATH): return
        with _LOCK:
            with open(LOG_PATH) as f: lines = f.readlines()
            if len(lines) <= MAX_LINES: return
            with open(LOG_PATH, 'w') as f: f.writelines(lines[-MAX_LINES:])
    except Exception: pass


def start_trim_daemon():
    def _loop():
        while True:
            time.sleep(3600)
            trim_log()
    threading.Thread(target=_loop, daemon=True).start()
