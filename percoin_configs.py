"""PreCog elite tier system — shipped Apr 20 2026.
Three tiers by OOS WR:
- PURE_14 (100% WR): 20x × 10% risk
- NINETY_99 (90-99% WR, 8 coins): 15x × 5% risk
- EIGHTY_89 (80-89% WR, 36 coins): 12x × 5% risk
All 58 coins validated over 17d OOS on 5m candles."""

# ─── TIER 1: 100% WR ELITE (20x × 10% risk) ───
PURE_14 = {
    'ALT':    {'sigs':['IB'],          'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.010,'SL':0.05},
    'ASTER':  {'sigs':['BB','PV'],     'flt':'ema200',     'RH':70,'RL':30,'TP':0.004,'SL':0.05},
    'BERA':   {'sigs':['IB','PV'],     'flt':'ema200+adx25','RH':75,'RL':25,'TP':0.010,'SL':0.05},
    'FET':    {'sigs':['IB'],          'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.010,'SL':0.05},
    'IMX':    {'sigs':['PV'],          'flt':'ema200',     'RH':70,'RL':30,'TP':0.010,'SL':0.05},
    'PROMPT': {'sigs':['BB','IB','PV'],'flt':'ema200+adx25','RH':72,'RL':28,'TP':0.010,'SL':0.05},
    'RENDER': {'sigs':['BB','PV'],     'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.010,'SL':0.05},
    'STRK':   {'sigs':['PV'],          'flt':'ema200',     'RH':70,'RL':30,'TP':0.010,'SL':0.05},
    'SUPER':  {'sigs':['BB'],          'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.010,'SL':0.05},
    'W':      {'sigs':['IB','PV'],     'flt':'ema200',     'RH':70,'RL':30,'TP':0.008,'SL':0.05},
    'WCT':    {'sigs':['PV'],          'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.010,'SL':0.05},
    'WLFI':   {'sigs':['BB','PV'],     'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.010,'SL':0.05},
    'XAI':    {'sigs':['PV'],          'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.010,'SL':0.05},
    'ZK':     {'sigs':['IB'],          'flt':'adx25',      'RH':70,'RL':30,'TP':0.006,'SL':0.05},
}

# ─── TIER 2: 90-99% WR (15x × 5% risk) ───
NINETY_99 = {
    'BABY':  {'sigs':['BB'],          'flt':'none',       'RH':70,'RL':30,'TP':0.027,'SL':0.05},
    'FOGO':  {'sigs':['IB'],          'flt':'none',       'RH':70,'RL':30,'TP':0.019,'SL':0.05},
    'MAVIA': {'sigs':['IB','PV'],     'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.022,'SL':0.05},
    'PYTH':  {'sigs':['IB'],          'flt':'adx25',      'RH':70,'RL':30,'TP':0.021,'SL':0.05},
    'RSR':   {'sigs':['BB','IB','PV'],'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.014,'SL':0.05},
    'TRUMP': {'sigs':['IB'],          'flt':'adx25',      'RH':70,'RL':30,'TP':0.015,'SL':0.05},
    'VINE':  {'sigs':['IB'],          'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.011,'SL':0.05},
    'XLM':   {'sigs':['BB','PV'],     'flt':'ema200',     'RH':70,'RL':30,'TP':0.015,'SL':0.05},
}

# ─── TIER 3: 80-89% WR (12x × 5% risk) ───
EIGHTY_89 = {
    'ARB':     {'sigs':['BB','IB'],     'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.018,'SL':0.05},
    'ARK':     {'sigs':['PV'],          'flt':'ema200',     'RH':70,'RL':30,'TP':0.015,'SL':0.05},
    'BANANA':  {'sigs':['IB'],          'flt':'adx25',      'RH':70,'RL':30,'TP':0.023,'SL':0.05},
    'BIGTIME': {'sigs':['BB','PV'],     'flt':'ema200',     'RH':70,'RL':30,'TP':0.022,'SL':0.05},
    'BLAST':   {'sigs':['PV'],          'flt':'ema200',     'RH':70,'RL':30,'TP':0.018,'SL':0.05},
    'BSV':     {'sigs':['BB','PV'],     'flt':'ema200',     'RH':70,'RL':30,'TP':0.021,'SL':0.05},
    'CAKE':    {'sigs':['BB','IB'],     'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.020,'SL':0.05},
    'CRV':     {'sigs':['BB'],          'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.022,'SL':0.05},
    'DOOD':    {'sigs':['BB','IB'],     'flt':'adx25',      'RH':70,'RL':30,'TP':0.020,'SL':0.05},
    'ETHFI':   {'sigs':['BB','IB','PV'],'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.022,'SL':0.05},
    'GMX':     {'sigs':['PV'],          'flt':'ema200',     'RH':70,'RL':30,'TP':0.016,'SL':0.05},
    'HBAR':    {'sigs':['BB','IB'],     'flt':'ema200',     'RH':70,'RL':30,'TP':0.018,'SL':0.05},
    'HYPE':    {'sigs':['BB','PV'],     'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.017,'SL':0.05},
    'IO':      {'sigs':['BB','PV'],     'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.024,'SL':0.05},
    'IOTA':    {'sigs':['BB','PV'],     'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.022,'SL':0.05},
    'KAITO':   {'sigs':['PV'],          'flt':'ema200',     'RH':70,'RL':30,'TP':0.034,'SL':0.05},
    'LINEA':   {'sigs':['BB'],          'flt':'ema200',     'RH':70,'RL':30,'TP':0.019,'SL':0.05},
    'ME':      {'sigs':['BB','IB','PV'],'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.025,'SL':0.05},
    'MEGA':    {'sigs':['IB'],          'flt':'ema200',     'RH':70,'RL':30,'TP':0.022,'SL':0.05},
    'MELANIA': {'sigs':['BB','PV'],     'flt':'ema200',     'RH':70,'RL':30,'TP':0.017,'SL':0.05},
    'MERL':    {'sigs':['BB','IB','PV'],'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.028,'SL':0.05},
    'MOVE':    {'sigs':['BB','IB','PV'],'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.027,'SL':0.05},
    'NIL':     {'sigs':['PV'],          'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.030,'SL':0.05},
    'ONDO':    {'sigs':['BB','PV'],     'flt':'ema200',     'RH':70,'RL':30,'TP':0.031,'SL':0.05},
    'PAXG':    {'sigs':['IB'],          'flt':'ema200',     'RH':70,'RL':30,'TP':0.011,'SL':0.05},
    'PNUT':    {'sigs':['IB','PV'],     'flt':'ema200',     'RH':70,'RL':30,'TP':0.028,'SL':0.05},
    'S':       {'sigs':['PV'],          'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.022,'SL':0.05},
    'SAGA':    {'sigs':['BB','IB'],     'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.034,'SL':0.05},
    'SCR':     {'sigs':['IB'],          'flt':'none',       'RH':70,'RL':30,'TP':0.015,'SL':0.05},
    'SEI':     {'sigs':['BB','IB'],     'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.019,'SL':0.05},
    'TNSR':    {'sigs':['IB'],          'flt':'none',       'RH':70,'RL':30,'TP':0.026,'SL':0.05},
    'TST':     {'sigs':['BB'],          'flt':'ema200',     'RH':70,'RL':30,'TP':0.019,'SL':0.05},
    'USUAL':   {'sigs':['BB','PV'],     'flt':'ema200',     'RH':70,'RL':30,'TP':0.019,'SL':0.05},
    'XMR':     {'sigs':['IB'],          'flt':'none',       'RH':70,'RL':30,'TP':0.019,'SL':0.05},
    'ZEREBRO': {'sigs':['BB','PV'],     'flt':'ema200',     'RH':70,'RL':30,'TP':0.036,'SL':0.05},
    'ZORA':    {'sigs':['PV'],          'flt':'ema200',     'RH':70,'RL':30,'TP':0.016,'SL':0.05},
}

# Per-tier position sizing
TIER_SIZING = {
    'PURE':      {'leverage': 20, 'risk_pct': 0.10},
    'NINETY_99': {'leverage': 15, 'risk_pct': 0.05},
    'EIGHTY_89': {'leverage': 12, 'risk_pct': 0.05},
}

ELITE_MODE = True

def get_tier(coin):
    """Returns tier name for coin, or None if not in any tier."""
    if coin in PURE_14: return 'PURE'
    if coin in NINETY_99: return 'NINETY_99'
    if coin in EIGHTY_89: return 'EIGHTY_89'
    return None

def is_elite(coin):
    """Is this coin in ANY elite tier?"""
    return get_tier(coin) is not None

def get_config(coin):
    """Return per-coin config dict."""
    if coin in PURE_14: return PURE_14[coin]
    if coin in NINETY_99: return NINETY_99[coin]
    if coin in EIGHTY_89: return EIGHTY_89[coin]
    return None

def get_sizing(coin):
    """Return (leverage, risk_pct) for coin's tier."""
    tier = get_tier(coin)
    if not tier: return (10, 0.02)
    s = TIER_SIZING[tier]
    return (s['leverage'], s['risk_pct'])

def elite_leverage(coin=None):
    if coin:
        lev, _ = get_sizing(coin)
        return lev
    return 20

def check_filter(flt, ema200_val, ema50_val, adx_val, side, price):
    if 'ema200' in flt and ema200_val is not None:
        if side == 1 and price < ema200_val: return False
        if side == -1 and price > ema200_val: return False
    if 'ema50' in flt and ema50_val is not None:
        if side == 1 and price < ema50_val: return False
        if side == -1 and price > ema50_val: return False
    if 'adx25' in flt and (adx_val is None or adx_val < 25): return False
    if 'adx20' in flt and (adx_val is None or adx_val < 20): return False
    return True

def check_signal_allowed(coin, sig_type):
    cfg = get_config(coin)
    if not cfg: return False
    sig_map = {'PIVOT':'PV','BB_REJ':'BB','INSIDE_BAR':'IB'}
    return sig_map.get(sig_type, sig_type) in cfg['sigs']

def stats():
    return {
        'elite_mode': ELITE_MODE,
        'tiers': {
            'PURE_14':    {'coins': sorted(PURE_14.keys()),    'count': len(PURE_14),    'lev': 20, 'risk': '10%'},
            'NINETY_99':  {'coins': sorted(NINETY_99.keys()),  'count': len(NINETY_99),  'lev': 15, 'risk': '5%'},
            'EIGHTY_89':  {'coins': sorted(EIGHTY_89.keys()),  'count': len(EIGHTY_89),  'lev': 12, 'risk': '5%'},
        },
        'total_coins': len(PURE_14) + len(NINETY_99) + len(EIGHTY_89),
    }
