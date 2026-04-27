"""PreCog per-coin configs — ENTERPRISE 15m OOS tuned.

71 coins total (57 original + 14 grid-expansion).

Original 57 (2026-04-23): Wilson lb≥0.50 after fees+slippage.
Grid expansion 14 (2026-04-25): same methodology on 90d 15m BB-rejection backtest.
Top-K=3 ensemble stored in regime_configs.py.

PURE_14    (score>=5.0):     7 coins - 15x lev, 0.5% risk
NINETY_99  (3.0-5.0):       20 coins - 12x lev, 0.5% risk
EIGHTY_89  (<3.0):          34 coins - 10x lev, 0.5% risk  (+4 STRICT 2026-04-25)
SEVENTY_79 (probationary):  10 coins - 10x lev, 0.5% risk  (RELAXED, monitor closely)
"""

# ─── PURE_14: 7 coins ───
PURE_14 = {
    'DYM'         : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.08, 'SL':0.035},
    'POL'         : {'sigs':['BB','PV'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.06, 'SL':0.03},  # +multi-engine 2026-04-25
    'ZRO'         : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.1, 'SL':0.04},
    'UNI'         : {'sigs':['BB'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.04, 'SL':0.02},
    'LAYER'       : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.07, 'SL':0.025},
    'ETHFI'       : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.1, 'SL':0.04},
    'CC'          : {'sigs':['BB','PV'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.1, 'SL':0.04},  # +multi-engine 2026-04-25
}

# ─── NINETY_99: 20 coins ───
NINETY_99 = {
    'INJ'         : {'sigs':['BB','IB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.08, 'SL':0.035},  # +multi-engine 2026-04-25
    'HBAR'        : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.1, 'SL':0.04},
    'MOVE'        : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.1, 'SL':0.04},
    'JTO'         : {'sigs':['TR'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.06, 'SL':0.03},
    'STX'         : {'sigs':['PV'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.1, 'SL':0.04},
    'SUSHI'       : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.1, 'SL':0.04},
    'MEW'         : {'sigs':['MR'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.08, 'SL':0.03},
    'NOT'         : {'sigs':['PV'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.05, 'SL':0.025},
    'W'           : {'sigs':['BB'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.1, 'SL':0.04},
    'XRP'         : {'sigs':['BB','PV'], 'flt':'none', 'RH':70, 'RL':30, 'TP':0.1, 'SL':0.04},  # +multi-engine 2026-04-25
    'kFLOKI'      : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.035},
    'ORDI'        : {'sigs':['VS'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.08, 'SL':0.035},
    'SKR'         : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.08, 'SL':0.035},
    'WLFI'        : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'AAVE'        : {'sigs':['BB'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.1, 'SL':0.04},
    'PUMP'        : {'sigs':['PV'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.05, 'SL':0.025},
    'HMSTR'       : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'TRB'         : {'sigs':['BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},
    'BIGTIME'     : {'sigs':['BB'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.1, 'SL':0.04},
    'SUPER'       : {'sigs':['TR'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.08, 'SL':0.035},
}

# ─── EIGHTY_89: 30 coins ───
EIGHTY_89 = {
    'SOL'         : {'sigs':['BB'], 'flt':'none', 'RH':68, 'RL':32, 'TP':0.07, 'SL':0.025},
    'WCT'         : {'sigs':['VS'], 'flt':'none', 'RH':78, 'RL':22, 'TP':0.1, 'SL':0.04},
    'JUP'         : {'sigs':['BB','PV'], 'flt':'none', 'RH':72, 'RL':28, 'TP':0.1, 'SL':0.04},  # +multi-engine 2026-04-25
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
    'CAKE'        : {'sigs':['TR','BB'], 'flt':'none', 'RH':65, 'RL':35, 'TP':0.1, 'SL':0.04},  # +multi-engine 2026-04-25
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
    # ─── Whitelist grid expansion 2026-04-25 (STRICT promotions) ───
    # Wilson_lb≥50%, PF≥1.3, n≥30 trades on 90d 15m BB-rejection backtest
    # Same standard as original whitelist promotions (2026-04-23).
    'UMA'         : {'sigs':['BB','PV'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.04},   # WR 65.1% PF 2.25 n=63  # +multi-engine 2026-04-25
    'MEGA'        : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.035},  # WR 65.4% PF 1.95 n=52
    'CRV'         : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.04},   # WR 63.5% PF 2.32 n=63
    'VVV'         : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.04},   # WR 64.0% PF 2.62 n=50
    # ─── Multi-engine grid expansion 2026-04-25 (Lever 3 STRICT, PV engine) ───
    # PV (pivot RSI) sweep STRICT: Wilson_lb≥50%, PF≥1.3, n≥30 on 90d 15m backtest.
    'RSR'         : {'sigs':['PV'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.04},   # WR 65.5% PF 1.89 n=84
    'BANANA'      : {'sigs':['PV'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.10, 'SL':0.035},  # WR 70.0% PF 2.86 n=40
    'STRK'        : {'sigs':['PV'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.04},   # WR 62.4% PF 1.53 n=85
    'NEAR'        : {'sigs':['PV'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.035},  # WR 67.6% PF 2.09 n=37
    'USTC'        : {'sigs':['PV'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.10, 'SL':0.04},   # WR 63.0% PF 1.76 n=73
    'ICP'         : {'sigs':['PV'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.10, 'SL':0.04},   # WR 60.2% PF 1.91 n=108
    'GRASS'       : {'sigs':['PV'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.04},   # WR 60.0% PF 1.63 n=100
}

# ─── SEVENTY_79: probationary tier (10x lev, 0.5% risk) ───
# Whitelist grid expansion 2026-04-25 (RELAXED promotions).
# Wilson_lb 45-50%, PF≥1.1, n≥30 trades. Live but flagged for monitoring.
# Yank if WR drops below 40% on >5 closed live trades (survival guard catches this).
SEVENTY_79 = {
    'NEO'         : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.10, 'SL':0.025},  # WR 66.7% PF 2.27 n=30
    'TRX'         : {'sigs':['BB','PV'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.04},   # WR 61.1% PF 2.08 n=54  # +multi-engine 2026-04-25
    'STABLE'      : {'sigs':['BB','PV'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.04},   # WR 59.4% PF 1.82 n=69  # +multi-engine 2026-04-25
    'S'           : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.04},   # WR 60.7% PF 1.72 n=56
    'APEX'        : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.04},   # WR 58.6% PF 1.41 n=70
    'MANTA'       : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.08, 'SL':0.035},  # WR 60.4% PF 1.60 n=48
    'POLYX'       : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.035},  # WR 60.4% PF 1.55 n=48
    'WIF'         : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.10, 'SL':0.035},  # WR 61.5% PF 2.20 n=39
    'LTC'         : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.035},  # WR 59.2% PF 1.88 n=49
    'BNB'         : {'sigs':['BB'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.025},  # WR 61.8% PF 2.21 n=34
    'TAO'         : {'sigs':['BB','PV'], 'flt':'none', 'RH':75, 'RL':25, 'TP':0.05, 'SL':0.025},  # backtest WR 100%/2 +4%/trade — probationary 2026-04-27, watch closely
}

# Enterprise sizing — 0.5% risk uniform (edge-proof phase)
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
    """Per-coin config, regime-aware.

    Base tier config provides default sigs/RH/RL/TP/SL.
    regime_configs.py overrides with enterprise top-1 ensemble pick when regime known.
    """
    base_cfg = None
    if coin in PURE_14: base_cfg = dict(PURE_14[coin])
    elif coin in NINETY_99: base_cfg = dict(NINETY_99[coin])
    elif coin in EIGHTY_89: base_cfg = dict(EIGHTY_89[coin])
    elif coin in SEVENTY_79: base_cfg = dict(SEVENTY_79[coin])
    if base_cfg is None:
        return None

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
