"""PreCog per-coin configs — 15m ENTERPRISE OOS tuned 2026-04-23.

57 coins passed enterprise gate (Wilson lb ≥ 0.50, fees+slip modeled).
Regime-specific configs in regime_configs.py override these per regime.
Base configs here provide fallback TP/SL and tier sizing.

Tier names preserved for compatibility with leverage_resolver, tier_killswitch.
"""

# ─── PURE_14: 9 coins ───
PURE_14 = {
    'DYM'         : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.035},
    'POL'         : {'sigs':['BB'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.06, 'SL':0.03},
    'ZRO'         : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.1, 'SL':0.04},
    'UNI'         : {'sigs':['BB'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.04, 'SL':0.02},
    'LAYER'       : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.07, 'SL':0.025},
    'ETHFI'       : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.1, 'SL':0.04},
    'CC'          : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.1, 'SL':0.04},
    'INJ'         : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.08, 'SL':0.035},
    'HBAR'        : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.1, 'SL':0.04},
}

# ─── NINETY_99: 13 coins ───
NINETY_99 = {
    'MOVE'        : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.1, 'SL':0.04},
    'JTO'         : {'sigs':['TR'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.06, 'SL':0.03},
    'STX'         : {'sigs':['PV'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.1, 'SL':0.04},
    'SUSHI'       : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.1, 'SL':0.04},
    'MEW'         : {'sigs':['MR'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.08, 'SL':0.03},
    'NOT'         : {'sigs':['PV'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.05, 'SL':0.025},
    'W'           : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.1, 'SL':0.04},
    'XRP'         : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.1, 'SL':0.04},
    'kFLOKI'      : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.035},
    'ORDI'        : {'sigs':['VS'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.08, 'SL':0.035},
    'SKR'         : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.08, 'SL':0.035},
    'WLFI'        : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'AAVE'        : {'sigs':['BB'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.1, 'SL':0.04},
}

# ─── EIGHTY_89: 35 coins ───
EIGHTY_89 = {
    'PUMP'        : {'sigs':['PV'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.05, 'SL':0.025},
    'HMSTR'       : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'TRB'         : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'BIGTIME'     : {'sigs':['BB'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.1, 'SL':0.04},
    'SUPER'       : {'sigs':['TR'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.08, 'SL':0.035},
    'SOL'         : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.07, 'SL':0.025},
    'WCT'         : {'sigs':['VS'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.1, 'SL':0.04},
    'JUP'         : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.1, 'SL':0.04},
    'ASTER'       : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.04, 'SL':0.02},
    'IP'          : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.1, 'SL':0.04},
    'MAV'         : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'FTT'         : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.1, 'SL':0.04},
    'ZEN'         : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.025},
    'ALT'         : {'sigs':['PV'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.1, 'SL':0.04},
    'SEI'         : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.03},
    'APE'         : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'SAND'        : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.1, 'SL':0.04},
    'PROVE'       : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.1, 'SL':0.04},
    'ENS'         : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.035},
    'LINK'        : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.1, 'SL':0.04},
    'ETC'         : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.1, 'SL':0.04},
    'TURBO'       : {'sigs':['BB'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.1, 'SL':0.04},
    'LINEA'       : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'TNSR'        : {'sigs':['PV'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.05, 'SL':0.02},
    'CAKE'        : {'sigs':['TR'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'ADA'         : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'OP'          : {'sigs':['VS'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'SNX'         : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.03},
    'BLAST'       : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.1, 'SL':0.04},
    'ZK'          : {'sigs':['VS'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.1, 'SL':0.04},
    'LDO'         : {'sigs':['TR'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'KAS'         : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.03},
    'MINA'        : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.035},
    'XLM'         : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.035},
    'ANIME'       : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.035},
}

# SEVENTY_79 deprecated
SEVENTY_79 = {}

# Edge-proof sizing: 0.5% uniform risk to survive any drawdown, capture every signal
TIER_SIZING = {
    'PURE':       {'leverage': 15, 'risk_pct': 0.005},
    'NINETY_99':  {'leverage': 12, 'risk_pct': 0.005},
    'EIGHTY_89':  {'leverage': 10, 'risk_pct': 0.005},
    'SEVENTY_79': {'leverage': 10, 'risk_pct': 0.005},
}

ELITE_MODE = True


def get_tier(coin):
    if coin in PURE_14: return 'PURE'
    if coin in NINETY_99: return 'NINETY_99'
    if coin in EIGHTY_89: return 'EIGHTY_89'
    if coin in SEVENTY_79: return 'SEVENTY_79'
    return None


def is_elite(coin):
    return get_tier(coin) is not None


def get_sizing(coin):
    tier = get_tier(coin)
    if not tier: return (10, 0.005)
    s = TIER_SIZING[tier]
    return (s['leverage'], s['risk_pct'])


def get_config(coin):
    """Regime-aware per-coin config. Returns None for non-elite coins."""
    base_cfg = None
    if coin in PURE_14: base_cfg = dict(PURE_14[coin])
    elif coin in NINETY_99: base_cfg = dict(NINETY_99[coin])
    elif coin in EIGHTY_89: base_cfg = dict(EIGHTY_89[coin])
    elif coin in SEVENTY_79: base_cfg = dict(SEVENTY_79[coin])
    if base_cfg is None: return None

    try:
        import regime_detector
        import regime_configs
        regime = regime_detector.get_regime()
        if regime:
            reg_cfg, reason = regime_configs.get_config_with_fallback(coin, regime)
            if reg_cfg:
                merged = dict(reg_cfg)
                merged['_regime'] = regime
                merged['_regime_source'] = reason
                merged['_base_tier'] = get_tier(coin)
                return merged
    except Exception:
        pass

    base_cfg['_base_tier'] = get_tier(coin)
    base_cfg['_regime_source'] = 'base_fallback'
    return base_cfg
