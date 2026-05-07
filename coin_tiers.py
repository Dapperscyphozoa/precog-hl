"""coin_tiers.py — Per-coin wall threshold scaling.

Two-layer threshold system:
  1. Static tier floor (by market cap) — minimum wall USD a coin's market
     structure can produce
  2. Dynamic depth baseline — recent typical book depth observed live; walls
     must be Nx larger than this baseline to qualify

A wall qualifies if its USD ≥ MAX(tier_floor, dynamic_baseline × multiplier).

This adapts: when book is thin, walls qualify at smaller absolute size. When
book is heavy, the bar rises. No coin gets stuck at a static threshold that
doesn't match its current liquidity regime.
"""
from collections import defaultdict, deque
from typing import Optional, Dict, List


# Tier floors — derived from typical HL/cross-venue depth observation.
# Format: { coin: (tier_floor_usd, min_persistence_polls) }
COIN_TIERS = {
    # Tier 1 — mega cap (always-deep books)
    'BTC': (500_000, 5),
    'ETH': (500_000, 5),
    # Tier 2 — large cap
    'SOL': (250_000, 5),
    'BNB': (250_000, 5),
    'XRP': (250_000, 5),
    'DOGE': (250_000, 5),
    # Tier 3 — mid cap
    'LINK': (100_000, 4),
    'ADA': (100_000, 4),
    'AVAX': (100_000, 4),
    'LTC': (100_000, 4),
    'DOT': (100_000, 4),
    'NEAR': (100_000, 4),
    'ATOM': (100_000, 4),
    'APT': (100_000, 4),
    'SUI': (100_000, 4),
    'ARB': (100_000, 4),
    'OP': (100_000, 4),
    'INJ': (100_000, 4),
    'TIA': (100_000, 4),
    'SEI': (100_000, 4),
    'UNI': (100_000, 4),
    'LDO': (100_000, 4),
    'FET': (100_000, 4),
    'ENA': (100_000, 4),
    'ONDO': (100_000, 4),
    'JUP': (100_000, 4),
    'CRV': (100_000, 4),
    # Tier 4 — low cap / volatile
    'WIF': (50_000, 4),
    'PYTH': (50_000, 4),
    'RUNE': (50_000, 4),
    'PENDLE': (50_000, 4),
    'MNT': (50_000, 4),
    'TON': (50_000, 4),
    'DYDX': (50_000, 4),
    'HYPE': (50_000, 4),
}

# Default for any coin not explicitly tiered
DEFAULT_TIER = (100_000, 4)


def get_tier(coin: str):
    return COIN_TIERS.get(coin.upper(), DEFAULT_TIER)


class DepthBaseline:
    """Tracks per-coin recent typical bucket size to size-adjust wall thresholds.

    For each coin, on each poll: record the median bucket-USD across all
    buckets within 0.5% of mid. The dynamic threshold is `multiplier × median`
    over the last `window` polls.

    A wall qualifies dynamically if its USD ≥ multiplier × baseline_median.
    Combined with tier floor: final_threshold = max(tier_floor, dyn).
    """

    def __init__(self, window: int = 20, multiplier: float = 8.0):
        self.window = window
        self.multiplier = multiplier
        # coin -> deque of median-bucket-USD per poll
        self.history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=window))

    def record(self, coin: str, all_buckets_usd: List[float]):
        """Record this poll's median bucket size. Called once per coin per poll
        with the list of USD-per-bucket values across all buckets (bid+ask)
        within the inspection band."""
        if not all_buckets_usd:
            return
        # Use median, robust to wall outliers
        sorted_b = sorted(all_buckets_usd)
        median = sorted_b[len(sorted_b) // 2]
        self.history[coin].append(median)

    def baseline(self, coin: str) -> Optional[float]:
        """Return current baseline = median(history) × multiplier.
        None if insufficient data."""
        h = self.history.get(coin)
        if not h or len(h) < 5:
            return None
        sorted_h = sorted(h)
        med = sorted_h[len(sorted_h) // 2]
        return med * self.multiplier

    def threshold(self, coin: str) -> tuple:
        """Returns (min_usd, min_persistence) for this coin RIGHT NOW.
        Uses MAX of tier floor and dynamic baseline. Falls back to tier floor
        if dynamic baseline not yet computed."""
        tier_usd, tier_persistence = get_tier(coin)
        dyn = self.baseline(coin)
        if dyn is None:
            return tier_usd, tier_persistence
        # Use the LARGER of static floor and dynamic baseline
        return max(tier_usd, dyn), tier_persistence

    def stats(self, coin: str) -> dict:
        """Diagnostic info for logging."""
        h = self.history.get(coin)
        tier_usd, tier_p = get_tier(coin)
        dyn = self.baseline(coin)
        return {
            'tier_floor': tier_usd,
            'tier_persistence': tier_p,
            'dynamic_baseline': dyn,
            'samples': len(h) if h else 0,
            'effective_threshold': max(tier_usd, dyn) if dyn is not None else tier_usd,
        }
