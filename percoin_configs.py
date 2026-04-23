"""PreCog elite tier system — shipped Apr 20 2026.
Three tiers by OOS WR:
- PURE_14 (100% WR): 20x × 10% risk
- NINETY_99 (90-99% WR, 8 coins): 15x × 5% risk
- EIGHTY_89 (80-89% WR, 36 coins): 12x × 5% risk
All 58 coins validated over 17d OOS on 5m candles."""

# ─── TIER 1: 100% WR ELITE (20x × 10% risk) ───
PURE_14 = {
    'STABLE':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'AXS':    {'sigs':['BB'],'flt':'conv','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'ALT':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'HMSTR':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'MAV':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'MELANIA':    {'sigs':['BB'],'flt':'conv','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'BRETT':    {'sigs':['PV'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'USUAL':    {'sigs':['BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'SNX':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'POPCAT':    {'sigs':['BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'ASTER':    {'sigs':['BB'],'flt':'conv','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'IO':    {'sigs':['PV'],'flt':'conv','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'SUI':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'NXPC':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'SEI':    {'sigs':['BB'],'flt':'conv','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'HBAR':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'MEGA':    {'sigs':['PV'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'MANTA':    {'sigs':['PV'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'GALA':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'XMR':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'YZY':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'ANIME':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'POL':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'LTC':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'GMX':    {'sigs':['PV'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'TRX':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'BTC':    {'sigs':['PV','BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'PAXG':    {'sigs':['BB'],'flt':'conv','RH':70,'RL':30,'TP':0.06,'SL':0.03},
}

# ─── TIER 2: 90-99% WR (15x × 5% risk) ───
NINETY_99 = {
    'ZEREBRO':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'BABY':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'BANANA':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'REZ':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'RENDER':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'CHILLGUY':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'PENGU':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'FARTCOIN':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'MEW':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'UMA':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'PYTH':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'CAKE':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'ONDO':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'SUSHI':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'ARK':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'DOT':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'CRV':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'TURBO':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'APT':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'ME':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'STRK':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'BLAST':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'DOOD':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'WLFI':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'SAND':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'TNSR':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'FET':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'SAGA':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'SOL':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'STX':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'BIGTIME':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'ZK':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'ETH':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'STBL':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'XAI':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'MAVIA':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'TAO':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'XLM':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'ZORA':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'SPX':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'AVAX':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'NEAR':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'AR':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'ENS':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'ARB':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'BSV':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'INJ':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'XRP':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'MET':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'BCH':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'LINK':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'UNI':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'WIF':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'LINEA':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'ATOM':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'GMT':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
}

# ─── TIER 3: 80-89% WR (12x × 5% risk) ───
EIGHTY_89 = {
    'VVV':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'MOODENG':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'HEMI':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'SUPER':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'RSR':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'PROMPT':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'ZEC':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'NIL':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'DYM':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'MON':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'SCR':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'NOT':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'AERO':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'KAITO':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'WCT':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'PNUT':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'CC':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'MORPHO':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'TST':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'KAS':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'kBONK':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'VINE':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'JUP':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'CELO':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.03},
    'AAVE':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'W':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'MOVE':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'IMX':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'FIL':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'OP':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'MERL':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'FOGO':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'kNEIRO':    {'sigs':['BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
    'LIT':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.08,'SL':0.02},
    'ORDI':    {'sigs':['PV'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.03},
    'ETHFI':    {'sigs':['PV','BB'],'flt':'none','RH':70,'RL':30,'TP':0.06,'SL':0.02},
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
    # 2026-04-22: risk_pct reduced ~60% to prove % edge without dollar risk.
    # Leverage unchanged — 15x/12x/10x retain the % return profile, only the
    # capital allocated per trade drops. At $615 equity this means:
    #   PURE      2.0% × 15x = $185 notional (was $460)
    #   NINETY_99 1.2% × 12x = $88 notional  (was $220)
    #   EIGHTY_89 1.2% × 10x = $74 notional  (was $185)
    # Signals still fire on all coins, regime filter still sorts direction.
    # Same % returns measured, but dollar loss on -2% SL drops from ~$5 to $2.
    # Prove the bleed is stopped and regime filter correctly flips sides
    # BEFORE restoring risk_pct to the OOS-validated 5/3/3/3.
    'PURE':      {'leverage': 15, 'risk_pct': 0.02},
    'NINETY_99': {'leverage': 12, 'risk_pct': 0.012},
    'EIGHTY_89': {'leverage': 10, 'risk_pct': 0.012},
    'SEVENTY_79': {'leverage': 10, 'risk_pct': 0.012},
}
# Leverage rationale (OOS validated 14d):
#   5x  PURE: +164% gain, 11% MaxDD (safe but underleveraged)
#   10x PURE: +655% gain, 17% MaxDD
#   15x PURE: +1,676% gain, 27% MaxDD ← SWEET SPOT
#   20x PURE: -222% (LIQUIDATIONS — 4.5% liq line < 5% SL)
# At 15x leverage: liquidation line = 1/15 - 0.005 = 6.2%, safely BEYOND 5% SL.
# At 20x: liquidation = 1/20 - 0.005 = 4.5%, INSIDE 5% SL — every stopout = liquidation.
# Live trading 20x was silent disaster — every "SL hit" was actually liquidation.

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
    
    FIXED 2026-04-22: regime_configs.py was OOS-tuned on 5m noise, producing
    completely inverted R:R for swing trades. Every regime entry has TP=0.6-2.0%
    and SL=5.0% (R:R 0.12-0.40), which means every winning trade makes $1
    and every loss costs $5. 100% of entries fail MIN_RR=2.0. This is the
    5m-noise-tuner signature: high WR stats from micro-scalp TPs, but
    catastrophic expected value because losses are 5-10x larger than wins.
    
    Keep the regime tuner's LEGITIMATE insights (which signal engines
    work in which regime, which RSI thresholds, which filter stack) but
    FORCE TP and SL from the clean base configs which have swing-safe
    R:R ratios (2.0 to 4.0).
    
    Fallback chain:
      1. Regime-tuned sigs/RH/RL/flt + base TP/SL (primary path)
      2. Full base config (if no regime match)
      3. None (coin not in any tier)
    """
    # Base config (always needed for SL/TP override — these are swing-safe)
    base_cfg = None
    if coin in PURE_14: base_cfg = PURE_14[coin]
    elif coin in NINETY_99: base_cfg = NINETY_99[coin]
    elif coin in EIGHTY_89: base_cfg = EIGHTY_89[coin]
    elif coin in SEVENTY_79: base_cfg = SEVENTY_79[coin]
    
    # Try regime-aware enrichment
    try:
        import regime_detector
        import regime_configs
        regime = regime_detector.get_regime()
        if regime:
            reg_cfg, _ = regime_configs.get_config_with_fallback(coin, regime)
            if reg_cfg and base_cfg:
                # MERGE: regime provides engine selection, base provides risk params.
                # This gives us regime-adaptive signals with swing-safe R:R.
                merged = dict(reg_cfg)   # start with regime's sigs/RH/RL/flt
                merged['TP'] = base_cfg['TP']  # force swing-safe TP
                merged['SL'] = base_cfg['SL']  # force swing-safe SL
                # FORCE RSI SYMMETRY: the 5m-noise tuner often produced pairs
                # like RH=70, RL=22 — asymmetric thresholds that structurally
                # suppress BUYs (RSI rarely drops to 22 in a bull regime) while
                # letting SELLs through at standard 70. For a mean-reversion
                # signal, the thresholds must be mirror images: RH=70 pairs
                # with RL=30, RH=78 pairs with RL=22. Symmetry is a design
                # invariant, not an optimization target.
                rh = merged.get('RH', 70); rl = merged.get('RL', 30)
                if (rh, rl) not in [(70, 30), (78, 22), (75, 25), (72, 28)]:
                    # Map to nearest symmetric pair
                    merged['RL'] = 100 - rh  # 70→30, 78→22, 75→25, 72→28
                # Preserve regime diagnostics if the caller wants them
                merged['_regime'] = regime
                merged['_base_tier'] = get_tier(coin)
                return merged
            elif reg_cfg:
                # No base config (coin not in elite tiers) — use regime cfg but
                # override clearly-broken TP/SL with a sane default (R:R=3).
                merged = dict(reg_cfg)
                merged['TP'] = 0.06  # 6% — default swing TP
                merged['SL'] = 0.02  # 2% — tight swing SL, R:R=3
                # Force RSI symmetry (see comment above)
                rh = merged.get('RH', 70); rl = merged.get('RL', 30)
                if (rh, rl) not in [(70, 30), (78, 22), (75, 25), (72, 28)]:
                    merged['RL'] = 100 - rh
                merged['_regime'] = regime
                merged['_tp_sl_defaulted'] = True
                return merged
    except Exception:
        pass  # silent fallback to base config
    
    return base_cfg

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
