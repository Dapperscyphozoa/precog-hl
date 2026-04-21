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
    'AAVE':      {'sigs':['BB','IB'],'flt':'ema200+adx25','RH':70,'RL':25,'TP':0.01,'SL':0.05},  # 100% WR / 8 trades
    'ADA':       {'sigs':['PV','BB'],'flt':'ema200','RH':70,'RL':30,'TP':0.006,'SL':0.05},  # 100% WR / 13 trades
    'AERO':      {'sigs':['PV','BB'],'flt':'ema200+adx25','RH':75,'RL':30,'TP':0.025,'SL':0.05},  # 100% WR / 7 trades
    'AR':        {'sigs':['BB','IB'],'flt':'ema200','RH':70,'RL':25,'TP':0.01,'SL':0.05},  # 100% WR / 19 trades
    'ATOM':      {'sigs':['IB'],'flt':'adx25','RH':70,'RL':25,'TP':0.025,'SL':0.05},  # 100% WR / 5 trades
    'CHILLGUY':  {'sigs':['PV','BB'],'flt':'ema200','RH':70,'RL':30,'TP':0.015,'SL':0.05},  # 100% WR / 6 trades
    'COMP':      {'sigs':['BB','IB'],'flt':'ema200','RH':70,'RL':30,'TP':0.01,'SL':0.05},  # 100% WR / 20 trades
    'DOT':       {'sigs':['IB'],'flt':'adx25','RH':70,'RL':25,'TP':0.025,'SL':0.05},  # 100% WR / 7 trades
    'DYDX':      {'sigs':['IB'],'flt':'ema200','RH':70,'RL':25,'TP':0.015,'SL':0.05},  # 100% WR / 12 trades
    'DYM':       {'sigs':['BB','IB'],'flt':'ema200','RH':75,'RL':30,'TP':0.015,'SL':0.05},  # 100% WR / 14 trades
    'ENS':       {'sigs':['BB'],'flt':'ema200+adx20','RH':70,'RL':25,'TP':0.01,'SL':0.05},  # 100% WR / 5 trades
    'FARTCOIN':  {'sigs':['BB'],'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.01,'SL':0.05},  # 100% WR / 8 trades
    'FIL':       {'sigs':['BB','IB'],'flt':'ema200+adx20','RH':70,'RL':25,'TP':0.015,'SL':0.05},  # 100% WR / 10 trades
    'INJ':       {'sigs':['BB','IB'],'flt':'ema200+adx25','RH':70,'RL':25,'TP':0.01,'SL':0.05},  # 100% WR / 5 trades
    'LDO':       {'sigs':['IB'],'flt':'adx25','RH':70,'RL':25,'TP':0.025,'SL':0.05},  # 100% WR / 9 trades
    'LIT':       {'sigs':['PV','BB'],'flt':'ema200','RH':70,'RL':25,'TP':0.015,'SL':0.05},  # 100% WR / 11 trades
    'MON':       {'sigs':['PV','BB'],'flt':'ema200+adx20','RH':70,'RL':30,'TP':0.006,'SL':0.05},  # 100% WR / 12 trades
    'MOODENG':   {'sigs':['BB'],'flt':'ema200','RH':75,'RL':30,'TP':0.015,'SL':0.05},  # 100% WR / 6 trades
    'MORPHO':    {'sigs':['BB','IB'],'flt':'ema200','RH':70,'RL':30,'TP':0.01,'SL':0.05},  # 100% WR / 21 trades
    'OP':        {'sigs':['PV'],'flt':'ema200','RH':70,'RL':30,'TP':0.015,'SL':0.05},  # 100% WR / 9 trades
    'ORDI':      {'sigs':['BB','IB'],'flt':'ema200+adx20','RH':70,'RL':25,'TP':0.006,'SL':0.05},  # 100% WR / 9 trades
    'PENDLE':    {'sigs':['PV','BB'],'flt':'ema200','RH':75,'RL':25,'TP':0.015,'SL':0.05},  # 100% WR / 5 trades
    'PENGU':     {'sigs':['PV','BB'],'flt':'ema200','RH':70,'RL':30,'TP':0.006,'SL':0.05},  # 100% WR / 12 trades
    'POL':       {'sigs':['IB'],'flt':'ema200','RH':70,'RL':25,'TP':0.025,'SL':0.05},  # 100% WR / 8 trades
    'SOL':       {'sigs':['PV','BB'],'flt':'ema200','RH':70,'RL':30,'TP':0.015,'SL':0.05},  # 100% WR / 9 trades
    'SPX':       {'sigs':['BB','IB'],'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.006,'SL':0.05},  # 100% WR / 18 trades
    'TIA':       {'sigs':['PV','BB'],'flt':'ema200','RH':70,'RL':25,'TP':0.015,'SL':0.05},  # 100% WR / 7 trades
    'TON':       {'sigs':['PV'],'flt':'ema200','RH':70,'RL':30,'TP':0.025,'SL':0.05},  # 100% WR / 8 trades
    'TURBO':     {'sigs':['PV','BB'],'flt':'ema200','RH':70,'RL':30,'TP':0.025,'SL':0.05},  # 100% WR / 5 trades
    'UMA':       {'sigs':['PV'],'flt':'ema200','RH':75,'RL':30,'TP':0.01,'SL':0.05},  # 100% WR / 5 trades
    'UNI':       {'sigs':['PV'],'flt':'none','RH':75,'RL':25,'TP':0.006,'SL':0.05},  # 100% WR / 24 trades
    'WIF':       {'sigs':['PV','BB'],'flt':'ema200+adx20','RH':70,'RL':25,'TP':0.025,'SL':0.05},  # 100% WR / 5 trades
    'WLD':       {'sigs':['BB','IB'],'flt':'ema200+adx20','RH':70,'RL':25,'TP':0.01,'SL':0.05},  # 100% WR / 13 trades
    'ANIME':     {'sigs':['PV','IB'],'flt':'adx25','RH':80,'RL':25,'TP':0.025,'SL':0.03},  # 100% WR / 14 trades
    'APEX':      {'sigs':['IB'],'flt':'none','RH':65,'RL':20,'TP':0.015,'SL':0.05},  # 100% WR / 15 trades
    'AXS':       {'sigs':['PV','BB'],'flt':'ema200','RH':70,'RL':30,'TP':0.01,'SL':0.05},  # 100% WR / 15 trades
    'BCH':       {'sigs':['PV','IB'],'flt':'adx30','RH':70,'RL':30,'TP':0.005,'SL':0.05},  # 100% WR / 18 trades
    'CC':        {'sigs':['PV'],'flt':'adx20','RH':75,'RL':25,'TP':0.007,'SL':0.05},  # 100% WR / 31 trades
    'CELO':      {'sigs':['PV','IB'],'flt':'adx25','RH':80,'RL':25,'TP':0.007,'SL':0.05},  # 100% WR / 29 trades
    'GMT':       {'sigs':['PV','BB'],'flt':'adx30','RH':75,'RL':30,'TP':0.007,'SL':0.05},  # 100% WR / 33 trades
    'HEMI':      {'sigs':['BB','IB'],'flt':'none','RH':80,'RL':30,'TP':0.005,'SL':0.05},  # 100% WR / 54 trades
    'INIT':      {'sigs':['PV','BB'],'flt':'ema200','RH':75,'RL':35,'TP':0.005,'SL':0.05},  # 100% WR / 20 trades
    'KAS':       {'sigs':['PV'],'flt':'adx30','RH':80,'RL':35,'TP':0.007,'SL':0.05},  # 100% WR / 19 trades
    'MANTA':     {'sigs':['PV','BB','IB'],'flt':'ema200','RH':65,'RL':35,'TP':0.007,'SL':0.05},  # 100% WR / 27 trades
    'MET':       {'sigs':['PV','IB'],'flt':'ema200+adx25','RH':75,'RL':35,'TP':0.025,'SL':0.05},  # 100% WR / 9 trades
    'NXPC':      {'sigs':['PV'],'flt':'adx30','RH':75,'RL':30,'TP':0.025,'SL':0.03},  # 100% WR / 12 trades
    'POPCAT':    {'sigs':['BB'],'flt':'adx25','RH':80,'RL':30,'TP':0.025,'SL':0.05},  # 100% WR / 17 trades
    'RESOLV':    {'sigs':['BB','IB'],'flt':'ema200','RH':65,'RL':35,'TP':0.007,'SL':0.05},  # 100% WR / 43 trades
    'REZ':       {'sigs':['PV'],'flt':'none','RH':80,'RL':20,'TP':0.01,'SL':0.05},  # 100% WR / 17 trades
    'RUNE':      {'sigs':['PV','BB','IB'],'flt':'adx25','RH':75,'RL':30,'TP':0.025,'SL':0.05},  # 100% WR / 12 trades
    'SNX':       {'sigs':['PV'],'flt':'adx20','RH':80,'RL':25,'TP':0.01,'SL':0.05},  # 100% WR / 20 trades
    'STABLE':    {'sigs':['PV','BB','IB'],'flt':'none','RH':65,'RL':25,'TP':0.01,'SL':0.05},  # 100% WR / 48 trades
    'STBL':      {'sigs':['PV'],'flt':'ema200+adx25','RH':70,'RL':30,'TP':0.015,'SL':0.03},  # 100% WR / 20 trades
    'STX':       {'sigs':['BB','IB'],'flt':'ema200','RH':65,'RL':30,'TP':0.007,'SL':0.05},  # 100% WR / 19 trades
    'YZY':       {'sigs':['BB'],'flt':'adx20','RH':65,'RL':20,'TP':0.025,'SL':0.03},  # 100% WR / 5 trades
    'ZEC':       {'sigs':['IB'],'flt':'adx30','RH':65,'RL':20,'TP':0.02,'SL':0.05},  # 100% WR / 12 trades
    'kNEIRO':    {'sigs':['PV'],'flt':'ema200','RH':70,'RL':25,'TP':0.015,'SL':0.05},  # 100% WR / 8 trades


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
    'BLUR':      {'sigs':['IB'],'flt':'none','RH':70,'RL':25,'TP':0.015,'SL':0.05},  # 97% WR / 31 trades
    'BNB':       {'sigs':['PV'],'flt':'adx25','RH':75,'RL':30,'TP':0.01,'SL':0.05},  # 94% WR / 18 trades
    'BTC':       {'sigs':['IB'],'flt':'adx25','RH':70,'RL':25,'TP':0.015,'SL':0.05},  # 90% WR / 10 trades
    'JUP':       {'sigs':['BB','IB'],'flt':'adx25','RH':75,'RL':30,'TP':0.025,'SL':0.05},  # 94% WR / 17 trades
    'LTC':       {'sigs':['PV'],'flt':'none','RH':75,'RL':30,'TP':0.006,'SL':0.05},  # 93% WR / 27 trades
    'MAV':       {'sigs':['BB','IB'],'flt':'adx25','RH':70,'RL':25,'TP':0.01,'SL':0.05},  # 95% WR / 20 trades
    'MEW':       {'sigs':['PV'],'flt':'ema200+adx25','RH':75,'RL':25,'TP':0.015,'SL':0.05},  # 92% WR / 12 trades
    'SAND':      {'sigs':['PV'],'flt':'none','RH':75,'RL':25,'TP':0.015,'SL':0.05},  # 92% WR / 24 trades
    'SUSHI':     {'sigs':['PV'],'flt':'none','RH':75,'RL':30,'TP':0.006,'SL':0.05},  # 97% WR / 33 trades
    'TAO':       {'sigs':['PV'],'flt':'adx25','RH':75,'RL':25,'TP':0.015,'SL':0.05},  # 95% WR / 22 trades
    'kBONK':     {'sigs':['BB'],'flt':'none','RH':75,'RL':30,'TP':0.025,'SL':0.05},  # 90% WR / 20 trades
    'HMSTR':     {'sigs':['PV'],'flt':'none','RH':75,'RL':35,'TP':0.01,'SL':0.05},  # 91% WR / 56 trades
    'NOT':       {'sigs':['PV'],'flt':'adx20','RH':80,'RL':25,'TP':0.01,'SL':0.05},  # 92% WR / 52 trades


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
    'APT':       {'sigs':['BB','IB'],'flt':'none','RH':75,'RL':30,'TP':0.025,'SL':0.05},  # 85% WR / 20 trades
    'AVAX':      {'sigs':['PV','BB'],'flt':'none','RH':75,'RL':25,'TP':0.025,'SL':0.05},  # 89% WR / 19 trades
    'BRETT':     {'sigs':['BB','IB'],'flt':'adx25','RH':75,'RL':25,'TP':0.025,'SL':0.05},  # 89% WR / 18 trades
    'ETC':       {'sigs':['BB','IB'],'flt':'adx25','RH':70,'RL':25,'TP':0.015,'SL':0.05},  # 89% WR / 19 trades
    'GALA':      {'sigs':['BB','IB'],'flt':'none','RH':70,'RL':25,'TP':0.025,'SL':0.05},  # 86% WR / 21 trades
    'LINK':      {'sigs':['BB'],'flt':'none','RH':75,'RL':25,'TP':0.025,'SL':0.05},  # 89% WR / 9 trades
    'MEME':      {'sigs':['PV'],'flt':'ema200+adx20','RH':75,'RL':25,'TP':0.015,'SL':0.05},  # 80% WR / 5 trades
    'SUI':       {'sigs':['BB','IB'],'flt':'none','RH':75,'RL':30,'TP':0.025,'SL':0.05},  # 88% WR / 17 trades
    'TRX':       {'sigs':['PV','BB'],'flt':'ema200+adx20','RH':75,'RL':30,'TP':0.015,'SL':0.05},  # 80% WR / 5 trades
    'VVV':       {'sigs':['BB','IB'],'flt':'none','RH':75,'RL':25,'TP':0.025,'SL':0.05},  # 83% WR / 36 trades
    'XRP':       {'sigs':['BB','IB'],'flt':'ema200','RH':70,'RL':25,'TP':0.025,'SL':0.05},  # 86% WR / 7 trades

}


# ─── TIER 4: 70-79% WR (12x × 5% risk + 3-FILTER STACK) ───
# Filters required: signal_persistence (2-bar confirm) + wall_confluence HARD gate + BTC correlation strict
SEVENTY_79 = {

}

# Per-tier position sizing
# Reduced from 10/5/5/5 → 5/3/3/3 based on regime-aware OOS:
#   PROD 10/5/5/5: +123% gain, 21.8% MaxDD (14d)
#   5/3/3/3:       +174% gain, 11.2% MaxDD  ← HALF the DD, 51pp more gain
#   3/2/2/2:       +185% gain, 9.6% MaxDD   ← borderline (high signal count, fee drag risk)
# Rationale: smaller positions = more concurrent slots = capture more signals
TIER_SIZING = {
    'PURE':      {'leverage': 20, 'risk_pct': 0.05},
    'NINETY_99': {'leverage': 15, 'risk_pct': 0.03},
    'EIGHTY_89': {'leverage': 12, 'risk_pct': 0.03},
    'SEVENTY_79': {'leverage': 12, 'risk_pct': 0.03},
}

ELITE_MODE = True

def get_tier(coin):
    """Returns tier name for coin, or None if not in any tier."""
    if coin in PURE_14: return 'PURE'
    if coin in NINETY_99: return 'NINETY_99'
    if coin in EIGHTY_89: return 'EIGHTY_89'
    if coin in SEVENTY_79: return 'SEVENTY_79'
    return None

def is_elite(coin):
    """Is this coin in ANY elite tier?"""
    return get_tier(coin) is not None

def get_config(coin):
    """Return per-coin config dict, regime-aware if regime detector loaded.
    
    Tries regime-tuned config first (if regime_configs + regime_detector available),
    falls back to base PURE_14/NINETY_99/EIGHTY_89/SEVENTY_79 config."""
    # Try regime-aware first
    try:
        import regime_detector
        import regime_configs
        regime = regime_detector.get_regime()
        if regime:
            cfg, _ = regime_configs.get_config_with_fallback(coin, regime)
            if cfg: return cfg
    except Exception:
        pass  # silent fallback to base config
    
    # Base config fallback
    if coin in PURE_14: return PURE_14[coin]
    if coin in NINETY_99: return NINETY_99[coin]
    if coin in EIGHTY_89: return EIGHTY_89[coin]
    if coin in SEVENTY_79: return SEVENTY_79[coin]
    return None

def get_config_static(coin):
    """Original static config lookup (no regime). Used by tuner and OOS scripts."""
    if coin in PURE_14: return PURE_14[coin]
    if coin in NINETY_99: return NINETY_99[coin]
    if coin in EIGHTY_89: return EIGHTY_89[coin]
    if coin in SEVENTY_79: return SEVENTY_79[coin]
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


def needs_extra_filters(coin):
    """70-79% tier requires extra filter stack (signal_persistence + wall_confluence + btc_strict)."""
    return get_tier(coin) == 'SEVENTY_79'

def stats():
    return {
        'elite_mode': ELITE_MODE,
        'tiers': {
            'PURE_14':    {'coins': sorted(PURE_14.keys()),    'count': len(PURE_14),    'lev': 20, 'risk': '10%'},
            'NINETY_99':  {'coins': sorted(NINETY_99.keys()),  'count': len(NINETY_99),  'lev': 15, 'risk': '5%'},
            'EIGHTY_89':  {'coins': sorted(EIGHTY_89.keys()),  'count': len(EIGHTY_89),  'lev': 12, 'risk': '5%'},
            'SEVENTY_79': {'coins': sorted(SEVENTY_79.keys()), 'count': len(SEVENTY_79), 'lev': 12, 'risk': '5%', 'extra_filters': ['signal_persistence','wall_confluence','btc_corr_strict']},
        },
        'total_coins': len(PURE_14) + len(NINETY_99) + len(EIGHTY_89) + len(SEVENTY_79),
    }
