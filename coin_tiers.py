"""coin_tiers.py — Per-coin wall threshold scaling for full HL universe.

Tiered by 24h notional volume (auto-generated from HL metaAndAssetCtxs):
  Tier 1 (mega):    >$500M/24h    $500k floor, 5p persistence
  Tier 2 (large):   $100M-$500M   $250k floor, 5p
  Tier 3 (mid):     $20M-$100M    $100k floor, 4p
  Tier 4 (small):   $5M-$20M      $50k  floor, 4p
  Tier 5 (micro):   $1M-$5M       $25k  floor, 3p
  Tier 6 (illiquid):$0.1M-$1M     $10k  floor, 3p
  Tier 7 (dead):    <$0.1M        SKIP (not in dict)

Polling cadence (in tick_runner):
  Tier 1-3 (13 coins): every tick (~30s)
  Tier 4   (17 coins): every 2nd tick (~60s)
  Tier 5   (43 coins): every 4th tick (~120s)
  Tier 6   (79 coins): every 8th tick (~240s)
"""
from collections import defaultdict, deque
from typing import Optional, Dict, List

# Auto-generated tier config from HL universe (152 coins)
COIN_TIERS = {
    # Tier 1 — mega cap
    'BTC': (500000, 5),
    'ETH': (500000, 5),
    # Tier 2 — large cap
    'HYPE': (250000, 5),
    'SOL': (250000, 5),
    'TON': (250000, 5),
    'ZEC': (250000, 5),
    # Tier 3 — mid cap
    'DOGE': (100000, 4),
    'JTO': (100000, 4),
    'NEAR': (100000, 4),
    'PUMP': (100000, 4),
    'VVV': (100000, 4),
    'XRP': (100000, 4),
    'kPEPE': (100000, 4),
    # Tier 4 — small cap
    'AAVE': (50000, 4),
    'BNB': (50000, 4),
    'ENA': (50000, 4),
    'FARTCOIN': (50000, 4),
    'LINK': (50000, 4),
    'LIT': (50000, 4),
    'MON': (50000, 4),
    'ONDO': (50000, 4),
    'PAXG': (50000, 4),
    'PENDLE': (50000, 4),
    'PENGU': (50000, 4),
    'SUI': (50000, 4),
    'TAO': (50000, 4),
    'VIRTUAL': (50000, 4),
    'WIF': (50000, 4),
    'XMR': (50000, 4),
    'XPL': (50000, 4),
    # Tier 5 — micro cap
    'ADA': (25000, 3), 'AIXBT': (25000, 3), 'ALGO': (25000, 3),
    'APE': (25000, 3), 'APT': (25000, 3), 'AR': (25000, 3),
    'ARB': (25000, 3), 'ASTER': (25000, 3), 'AVAX': (25000, 3),
    'BCH': (25000, 3), 'BIO': (25000, 3), 'CHIP': (25000, 3),
    'CRV': (25000, 3), 'DASH': (25000, 3), 'DYDX': (25000, 3),
    'FET': (25000, 3), 'FIL': (25000, 3), 'ICP': (25000, 3),
    'IO': (25000, 3), 'IP': (25000, 3), 'JUP': (25000, 3),
    'LTC': (25000, 3), 'MEGA': (25000, 3), 'MORPHO': (25000, 3),
    'NIL': (25000, 3), 'NOT': (25000, 3), 'OP': (25000, 3),
    'POPCAT': (25000, 3), 'RUNE': (25000, 3), 'SEI': (25000, 3),
    'SPX': (25000, 3), 'STRK': (25000, 3), 'STX': (25000, 3),
    'TIA': (25000, 3), 'TRUMP': (25000, 3), 'TRX': (25000, 3),
    'UNI': (25000, 3), 'WLD': (25000, 3), 'WLFI': (25000, 3),
    'ZK': (25000, 3), 'ZRO': (25000, 3),
    'kBONK': (25000, 3), 'kLUNC': (25000, 3),
    # Tier 6 — illiquid
    '0G': (10000, 3), '2Z': (10000, 3), 'ACE': (10000, 3),
    'AERO': (10000, 3), 'ANIME': (10000, 3), 'APEX': (10000, 3),
    'ARK': (10000, 3), 'ATOM': (10000, 3), 'AVNT': (10000, 3),
    'AXS': (10000, 3), 'AZTEC': (10000, 3), 'BABY': (10000, 3),
    'BERA': (10000, 3), 'BIGTIME': (10000, 3), 'BLUR': (10000, 3),
    'BOME': (10000, 3), 'BRETT': (10000, 3), 'CAKE': (10000, 3),
    'CC': (10000, 3), 'CELO': (10000, 3), 'CFX': (10000, 3),
    'CHILLGUY': (10000, 3), 'COMP': (10000, 3), 'DOT': (10000, 3),
    'EIGEN': (10000, 3), 'ETC': (10000, 3), 'ETHFI': (10000, 3),
    'FOGO': (10000, 3), 'GALA': (10000, 3), 'GOAT': (10000, 3),
    'GRASS': (10000, 3), 'GRIFFAIN': (10000, 3), 'HBAR': (10000, 3),
    'HEMI': (10000, 3), 'HMSTR': (10000, 3), 'HYPER': (10000, 3),
    'IMX': (10000, 3), 'INIT': (10000, 3), 'INJ': (10000, 3),
    'KAITO': (10000, 3), 'KAS': (10000, 3), 'LDO': (10000, 3),
    'LINEA': (10000, 3), 'MELANIA': (10000, 3), 'MEME': (10000, 3),
    'MERL': (10000, 3), 'MET': (10000, 3), 'MNT': (10000, 3),
    'MOODENG': (10000, 3), 'NXPC': (10000, 3), 'ORDI': (10000, 3),
    'PNUT': (10000, 3), 'POL': (10000, 3), 'PURR': (10000, 3),
    'PYTH': (10000, 3), 'RENDER': (10000, 3), 'RESOLV': (10000, 3),
    'REZ': (10000, 3), 'S': (10000, 3), 'SAND': (10000, 3),
    'SKY': (10000, 3), 'SNX': (10000, 3), 'SOPH': (10000, 3),
    'STABLE': (10000, 3), 'SUPER': (10000, 3), 'SYRUP': (10000, 3),
    'TRB': (10000, 3), 'TST': (10000, 3), 'TURBO': (10000, 3),
    'VINE': (10000, 3), 'W': (10000, 3), 'WCT': (10000, 3),
    'XLM': (10000, 3), 'YGG': (10000, 3), 'ZEN': (10000, 3),
    'ZETA': (10000, 3),
    'kFLOKI': (10000, 3), 'kNEIRO': (10000, 3), 'kSHIB': (10000, 3),
}

# Polling cadence — every Nth tick. Higher cadence for liquid coins.
COIN_TICK_CADENCE = {}
for c, (floor, _) in COIN_TIERS.items():
    if floor >= 250000:   COIN_TICK_CADENCE[c] = 1   # every tick
    elif floor >= 100000: COIN_TICK_CADENCE[c] = 1   # every tick
    elif floor >= 50000:  COIN_TICK_CADENCE[c] = 2   # every 2nd tick
    elif floor >= 25000:  COIN_TICK_CADENCE[c] = 4   # every 4th tick
    else:                 COIN_TICK_CADENCE[c] = 8   # every 8th tick

DEFAULT_TIER = (100000, 4)
DEFAULT_CADENCE = 4

ALL_COINS = sorted(COIN_TIERS.keys())


def get_tier(coin: str):
    return COIN_TIERS.get(coin.upper(), DEFAULT_TIER)


def get_cadence(coin: str) -> int:
    return COIN_TICK_CADENCE.get(coin.upper(), DEFAULT_CADENCE)


def coins_for_tick(tick_n: int, all_coins: Optional[List[str]] = None) -> List[str]:
    """Return list of coins to scan on this tick number, based on cadence."""
    if all_coins is None:
        all_coins = ALL_COINS
    return [c for c in all_coins if tick_n % get_cadence(c) == 0]


class DepthBaseline:
    """Tracks per-coin recent typical bucket size for dynamic threshold adjustment."""
    def __init__(self, window: int = 20, multiplier: float = 8.0):
        self.window = window
        self.multiplier = multiplier
        self.history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=window))

    def record(self, coin: str, all_buckets_usd: List[float]):
        if not all_buckets_usd: return
        sorted_b = sorted(all_buckets_usd)
        median = sorted_b[len(sorted_b) // 2]
        self.history[coin].append(median)

    def baseline(self, coin: str) -> Optional[float]:
        h = self.history.get(coin)
        if not h or len(h) < 5: return None
        sorted_h = sorted(h)
        med = sorted_h[len(sorted_h) // 2]
        return med * self.multiplier

    def threshold(self, coin: str) -> tuple:
        tier_usd, tier_p = get_tier(coin)
        dyn = self.baseline(coin)
        if dyn is None: return tier_usd, tier_p
        return max(tier_usd, dyn), tier_p

    def stats(self, coin: str) -> dict:
        h = self.history.get(coin)
        tier_usd, tier_p = get_tier(coin)
        dyn = self.baseline(coin)
        return {
            'tier_floor': tier_usd, 'tier_persistence': tier_p,
            'dynamic_baseline': dyn, 'samples': len(h) if h else 0,
            'effective_threshold': max(tier_usd, dyn) if dyn is not None else tier_usd,
        }
