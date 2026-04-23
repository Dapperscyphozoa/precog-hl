"""PreCog OOS-tuned per-coin configs — 15m validated 2026-04-23.

55 coins passed walk-forward OOS (test_wr>=0.55, n>=10) on 52d HL history.
Regime-specific params in regime_configs.py override sigs/RH/RL/TP/SL per regime.
Tier names kept for compatibility with leverage_resolver.py, tier_killswitch.py, precog.py.
"""

# ─── PURE_14: 9 OOS-validated coins ───
PURE_14 = {
    'TNSR'        : {'sigs':['PV'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.03},
    'INJ'         : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.05, 'SL':0.025},
    'MEW'         : {'sigs':['PV'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.05, 'SL':0.025},
    'ZORA'        : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.03},
    'POL'         : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.03},
    'UNI'         : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.06, 'SL':0.02},
    'W'           : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.03},
    'ASTER'       : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.04, 'SL':0.02},
    'WLFI'        : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.03},
}

# ─── NINETY_99: 13 OOS-validated coins ───
NINETY_99 = {
    'CAKE'        : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.03},
    'LTC'         : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.03},
    'AXS'         : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.03},
    'MORPHO'      : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.03},
    'XRP'         : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.03},
    'STABLE'      : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.08, 'SL':0.03},
    'NOT'         : {'sigs':['PV'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.025},
    'LINEA'       : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.03},
    'VINE'        : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.03},
    'SUSHI'       : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.03},
    'DYM'         : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.03},
    'DOT'         : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.03},
    'MOVE'        : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.025},
}

# ─── EIGHTY_89: 33 OOS-validated coins ───
EIGHTY_89 = {
    'HMSTR'       : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.08, 'SL':0.03},
    'BLAST'       : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.03},
    'BCH'         : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.05, 'SL':0.025},
    'PYTH'        : {'sigs':['PV'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.03},
    'MANTA'       : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.03},
    'AVAX'        : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.03},
    'AAVE'        : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.03},
    'ALT'         : {'sigs':['PV'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.05, 'SL':0.025},
    'MAV'         : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.08, 'SL':0.03},
    'CC'          : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.08, 'SL':0.03},
    'ARK'         : {'sigs':['PV'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.05, 'SL':0.025},
    'SEI'         : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.05, 'SL':0.025},
    'YZY'         : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.03, 'SL':0.015},
    'GMT'         : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.03},
    'KAS'         : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.04, 'SL':0.02},
    'XMR'         : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.03},
    'SAND'        : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.08, 'SL':0.03},
    'BRETT'       : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.05, 'SL':0.025},
    'OP'          : {'sigs':['PV'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.08, 'SL':0.03},
    'XLM'         : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.08, 'SL':0.03},
    'BTC'         : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.03},
    'MEGA'        : {'sigs':['PV'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.03, 'SL':0.015},
    'AERO'        : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.03, 'SL':0.015},
    'GALA'        : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.03},
    'TRX'         : {'sigs':['PV'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.03, 'SL':0.015},
    'STX'         : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.04, 'SL':0.02},
    'STRK'        : {'sigs':['PV'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.03},
    'ATOM'        : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.08, 'SL':0.03},
    'kBONK'       : {'sigs':['PV'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.08, 'SL':0.03},
    'ME'          : {'sigs':['BB'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.05, 'SL':0.025},
    'UMA'         : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.08, 'SL':0.03},
    'NXPC'        : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.08, 'SL':0.03},
    'HBAR'        : {'sigs':['PV'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.03},
}

# SEVENTY_79 deprecated — zero coins passed OOS for this tier
SEVENTY_79 = {}

# Per-tier sizing (OOS-validated 15m, 2026-04-23)
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
    if not tier: return (10, 0.01)
    s = TIER_SIZING[tier]
    return (s['leverage'], s['risk_pct'])


def get_config(coin):
    """Per-coin config, regime-aware.

    Base tier config (PURE_14 / NINETY_99 / EIGHTY_89) provides the default
    sigs/RH/RL/TP/SL. regime_configs.py overrides when a regime-specific
    OOS-tuned config exists for this coin.

    Returns None if coin is not OOS-validated.
    """
    base_cfg = None
    if coin in PURE_14: base_cfg = dict(PURE_14[coin])
    elif coin in NINETY_99: base_cfg = dict(NINETY_99[coin])
    elif coin in EIGHTY_89: base_cfg = dict(EIGHTY_89[coin])
    elif coin in SEVENTY_79: base_cfg = dict(SEVENTY_79[coin])
    if base_cfg is None:
        return None

    # Regime enrichment
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
