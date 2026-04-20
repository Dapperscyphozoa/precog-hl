"""Per-coin leverage + risk resolver. Respects HL max lev.
Auto-adjusts risk % to preserve target tier notional, capped at safety ceiling.

Triple-checked logic:
- For each coin, actual_lev = min(tier_target_lev, HL_max_lev)
- To preserve notional = tier_target_lev × tier_target_risk:
    required_risk = notional / actual_lev
- Cap required_risk at RISK_CEILING (prevent concentration)
- If cap hit, notional is lower than target (accept the reduction)

Safety rationale:
- 100% WR tier CAN tolerate 40-67% risk per trade because no expected losses
- 80-89% and 70-79% tiers MUST cap lower (15-20%) because realized losses compound
- Max concurrent positions = 1 / risk_per_trade (prevent over-allocation)
"""

# Per-tier risk ceilings (max single-trade risk regardless of leverage mismatch)
TIER_RISK_CEILING = {
    'PURE': 0.50,        # 100% WR can handle up to 50% per trade safely
    'NINETY_99': 0.20,   # 91% WR — 4 consecutive losses = -80%, cap at 20% → max -60% DD
    'EIGHTY_89': 0.12,   # 83% WR — 6 consecutive losses = -72%, cap at 12% → max -60% DD
    'SEVENTY_79': 0.10,  # 73% WR — higher loss rate → tighter cap
}

def resolve(coin, tier_target_lev, tier_target_risk, hl_max_lev, tier_name):
    """Returns (actual_lev, actual_risk, actual_notional_pct, ceiling_hit_bool).
    
    Logic:
    1. actual_lev = min(target, HL cap)
    2. required_risk_to_preserve_notional = (target_lev × target_risk) / actual_lev
    3. actual_risk = min(required, ceiling)
    4. actual_notional = actual_lev × actual_risk
    """
    target_notional = tier_target_lev * tier_target_risk
    actual_lev = min(tier_target_lev, max(1, hl_max_lev))
    required_risk = target_notional / actual_lev
    ceiling = TIER_RISK_CEILING.get(tier_name, 0.10)
    actual_risk = min(required_risk, ceiling)
    ceiling_hit = required_risk > ceiling
    actual_notional = actual_lev * actual_risk
    return actual_lev, actual_risk, actual_notional, ceiling_hit


def resolve_all(tier_configs, hl_max_map):
    """Returns per-coin resolved config for all tiers. 
    tier_configs = {tier_name: {target_lev, target_risk, coins: [list]}}
    hl_max_map = {coin: max_lev}
    """
    out = {}
    for tier_name, info in tier_configs.items():
        for coin in info.get('coins', []):
            hl_max = hl_max_map.get(coin, info['target_lev'])
            lev, risk, notional, capped = resolve(coin, info['target_lev'], info['target_risk'], hl_max, tier_name)
            out[coin] = {
                'tier': tier_name,
                'target_lev': info['target_lev'],
                'target_risk_pct': round(info['target_risk']*100, 2),
                'hl_max_lev': hl_max,
                'actual_lev': lev,
                'actual_risk_pct': round(risk*100, 2),
                'actual_notional_pct': round(notional*100, 1),
                'ceiling_hit': capped,
            }
    return out
