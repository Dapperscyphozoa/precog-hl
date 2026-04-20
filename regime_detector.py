"""Market regime detector — uses Binance 1H/4H data (no HL 17d limit).
Detects: TREND_UP / TREND_DOWN / CHOP / HIGH_VOL / LOW_VOL
Auto-adjusts per-tier risk multipliers based on regime."""
import time, json, threading, urllib.request, urllib.parse

_LOCK = threading.Lock()
_state = {
    'ts': 0,
    'regime': 'UNKNOWN',
    'btc_1h_trend': 0,
    'btc_4h_trend': 0,
    'btc_vol_24h': 0,
    'risk_mult': 1.0,   # global risk multiplier based on regime
    'altcoin_strength': 0,  # -1 to +1, BTC vs alts
}

REFRESH_SEC = 300  # 5min

def _fetch_binance_klines(symbol='BTCUSDT', interval='1h', limit=100):
    """Returns list of [open_time, O, H, L, C, V, close_time, ...]"""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        r = urllib.request.urlopen(req, timeout=10).read()
        return json.loads(r)
    except Exception as e:
        return None

def _ema(values, period):
    if len(values) < period: return None
    k = 2/(period+1); e = sum(values[:period])/period
    for v in values[period:]: e = v*k + e*(1-k)
    return e

def refresh():
    """Pull BTC 1H + 4H, compute regime."""
    k_1h = _fetch_binance_klines('BTCUSDT', '1h', 100)
    k_4h = _fetch_binance_klines('BTCUSDT', '4h', 50)
    if not k_1h or not k_4h: return False
    
    # Close prices
    closes_1h = [float(x[4]) for x in k_1h]
    closes_4h = [float(x[4]) for x in k_4h]
    highs_24h = [float(x[2]) for x in k_1h[-24:]]
    lows_24h = [float(x[3]) for x in k_1h[-24:]]
    
    # 1H EMA 9 vs EMA 21 for short trend
    ema_1h_fast = _ema(closes_1h, 9)
    ema_1h_slow = _ema(closes_1h, 21)
    trend_1h = 1 if ema_1h_fast and ema_1h_slow and ema_1h_fast > ema_1h_slow * 1.002 else (-1 if ema_1h_fast and ema_1h_slow and ema_1h_fast < ema_1h_slow * 0.998 else 0)
    
    # 4H EMA 9 vs EMA 21 for mid trend
    ema_4h_fast = _ema(closes_4h, 9)
    ema_4h_slow = _ema(closes_4h, 21)
    trend_4h = 1 if ema_4h_fast and ema_4h_slow and ema_4h_fast > ema_4h_slow * 1.005 else (-1 if ema_4h_fast and ema_4h_slow and ema_4h_fast < ema_4h_slow * 0.995 else 0)
    
    # 24h vol: avg (high-low)/close
    vol_24h = sum((h - l) / c for h,l,c in zip(highs_24h, lows_24h, closes_1h[-24:])) / 24 if closes_1h else 0
    
    # ETH strength (for altcoin regime proxy)
    k_eth = _fetch_binance_klines('ETHUSDT', '1h', 24)
    eth_strength = 0
    if k_eth:
        eth_closes = [float(x[4]) for x in k_eth]
        btc_24h_change = (closes_1h[-1] - closes_1h[-24]) / closes_1h[-24] if len(closes_1h) >= 24 else 0
        eth_24h_change = (eth_closes[-1] - eth_closes[-24]) / eth_closes[-24] if len(eth_closes) >= 24 else 0
        eth_strength = max(-1, min(1, (eth_24h_change - btc_24h_change) * 50))
    
    # Determine regime
    if trend_1h == 1 and trend_4h >= 0 and vol_24h < 0.03:
        regime = 'TREND_UP'; risk_mult = 1.2
    elif trend_1h == -1 and trend_4h <= 0 and vol_24h < 0.03:
        regime = 'TREND_DOWN'; risk_mult = 1.0  # shorts work
    elif vol_24h > 0.04:
        regime = 'HIGH_VOL'; risk_mult = 0.6  # reduce risk
    elif vol_24h < 0.01:
        regime = 'LOW_VOL'; risk_mult = 0.8  # chop kills mean reversion
    elif trend_1h == 0 and trend_4h == 0:
        regime = 'CHOP'; risk_mult = 0.7
    else:
        regime = 'NEUTRAL'; risk_mult = 1.0
    
    with _LOCK:
        _state['ts'] = time.time()
        _state['regime'] = regime
        _state['btc_1h_trend'] = trend_1h
        _state['btc_4h_trend'] = trend_4h
        _state['btc_vol_24h'] = round(vol_24h, 4)
        _state['risk_mult'] = risk_mult
        _state['altcoin_strength'] = round(eth_strength, 3)
    return True

def get_regime():
    with _LOCK:
        if time.time() - _state['ts'] > REFRESH_SEC:
            threading.Thread(target=refresh, daemon=True).start()
        return dict(_state)

def get_risk_mult():
    with _LOCK:
        if time.time() - _state['ts'] > REFRESH_SEC:
            threading.Thread(target=refresh, daemon=True).start()
        return _state['risk_mult']
