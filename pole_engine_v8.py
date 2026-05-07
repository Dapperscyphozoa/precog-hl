"""V8 — Wall-based pole-to-pole + spoof breakout. NO chart-pattern fiction.

Reads LIVE multi-venue order book aggregation (Bybit + Binance + OKX + Coinbase
+ Bitget + Kraken) via HTTP from production /orderbook/<coin>. Real walls only.

Sub-engines:
  1. WallBounceEngine — limit at verified walls, TP at opposite wall
  2. SpoofBreakoutEngine — market entry WITH spoof breakout direction

Wall validation gates:
  - >= MIN_WALL_USD aggregate ($500k default)
  - >= MIN_PERSISTENCE polls (5 polls × 30s = 150s persistence)
  - within MAX_DIST_PCT of mid (2.5% default)

Spoof defense gates (pre-place):
  - Wall must pass all wall-validation gates
  - Wall has not shrunk >40% in last 90s (active spoof candidate)
  - No active spoof signal on this coin in last 5 min

Spoof attack (post-detect):
  - Wall has shrunk >= 70% from peak persistence-window USD
  - Price has crossed wall price by >= 0.05% (confirmed breakout, not just shrink)
  - Direction: WITH the breakout (bid pulled = SELL, ask pulled = BUY)
"""
import time
from dataclasses import dataclass
from typing import Optional, List, Dict
from collections import defaultdict, deque


@dataclass
class Wall:
    coin: str
    side: str               # 'bid' or 'ask'
    price: float            # mid of cluster
    low: float
    high: float
    usd: float
    distance_pct: float
    persistence_polls: int = 1
    first_seen_t: float = 0
    last_seen_t: float = 0


@dataclass
class Setup:
    coin: str
    side: str               # 'BUY' or 'SELL'
    kind: str               # 'WALL_BOUNCE' or 'SPOOF_BREAKOUT'
    order_type: str         # 'LIMIT' or 'MARKET'
    entry_price: float
    sl_price: float
    tp_price: float
    rr: float
    notes: str = ''


def cluster_walls(orders: List[dict], mid: float, side: str,
                   bucket_pct: float = 0.0005,
                   min_usd: float = 500_000,
                   max_dist_pct: float = 0.025) -> List[Wall]:
    """Aggregate ladder orders into 5bp buckets. Return clusters >= min_usd."""
    if not orders or mid <= 0: return []
    buckets: Dict[float, dict] = {}
    for o in orders:
        if abs(o['price'] - mid) / mid > max_dist_pct: continue
        b = round(o['price'] / mid / bucket_pct) * bucket_pct
        info = buckets.setdefault(b, {'usd': 0.0, 'low': 9e18, 'high': 0.0})
        info['usd'] += o['usd']
        info['low'] = min(info['low'], o['price'])
        info['high'] = max(info['high'], o['price'])
    walls = []
    for info in buckets.values():
        if info['usd'] >= min_usd:
            mid_px = (info['low'] + info['high']) / 2
            dist = abs(mid_px - mid) / mid
            walls.append(Wall(coin='', side=side, price=mid_px, low=info['low'],
                               high=info['high'], usd=info['usd'], distance_pct=dist))
    return walls


class WallTracker:
    """Tracks wall persistence + size history across polls.

    Identifies walls by (coin, side, 5bp price bucket). Maintains size deque
    per wall key. Used for both persistence count and spoof detection.
    """
    def __init__(self, max_history: int = 30):
        # key: 'coin|side|bucket' -> deque[(ts, usd)]
        self.history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=max_history))
        self.last_seen: Dict[str, float] = {}

    @staticmethod
    def _key(coin: str, side: str, price: float, mid: float) -> str:
        bucket = round(price / mid / 0.0005) * 0.0005
        return f"{coin}|{side}|{bucket:.5f}"

    def update(self, coin: str, walls: List[Wall], mid: float, ts: float) -> List[Wall]:
        """Record this poll's walls. Returns walls with persistence_polls populated."""
        seen = set()
        for w in walls:
            w.coin = coin
            k = self._key(coin, w.side, w.price, mid)
            self.history[k].append((ts, w.usd))
            self.last_seen[k] = ts
            seen.add(k)
            w.persistence_polls = len(self.history[k])
            w.first_seen_t = self.history[k][0][0]
            w.last_seen_t = ts
        # Decay: drop entries not seen in >180s
        for k in list(self.last_seen.keys()):
            if k.startswith(f"{coin}|") and k not in seen and ts - self.last_seen[k] > 180:
                self.history.pop(k, None)
                self.last_seen.pop(k, None)
        return walls

    def shrink_pct(self, coin: str, side: str, price: float, mid: float,
                    window_s: float = 90) -> Optional[float]:
        """Returns shrinkage % vs peak in last `window_s`. Negative = shrunk."""
        k = self._key(coin, side, price, mid)
        h = self.history.get(k)
        if not h or len(h) < 3: return None
        now_ts = h[-1][0]
        cutoff = now_ts - window_s
        within = [usd for ts, usd in h if ts >= cutoff]
        if not within: return None
        peak = max(within)
        if peak <= 0: return None
        return 1.0 - (h[-1][1] / peak)

    def all_keys_for(self, coin: str) -> List[str]:
        return [k for k in self.history.keys() if k.startswith(f"{coin}|")]


class WallBounceEngine:
    """Sub-engine 1: limit orders at verified walls. Direction-agnostic."""

    def __init__(self, min_persistence_polls: int = 5,
                  min_rr: float = 1.5, sl_atr_mult: float = 0.5,
                  sl_buffer_pct: float = 0.0010, cooldown_s: int = 2 * 3600,
                  spoof_filter_shrink: float = 0.40):
        self.min_persistence = min_persistence_polls
        self.min_rr = min_rr
        self.sl_atr_mult = sl_atr_mult
        self.sl_buffer_pct = sl_buffer_pct
        self.cooldown_s = cooldown_s
        self.spoof_filter_shrink = spoof_filter_shrink
        self._fired: Dict = {}

    def evaluate(self, coin: str, walls: List[Wall], tracker: WallTracker,
                  mid: float, atr_v: float, now_ts: Optional[float] = None) -> List[Setup]:
        if now_ts is None: now_ts = time.time()
        if not walls or mid <= 0 or atr_v <= 0: return []

        # Verified walls only
        verified = [w for w in walls if w.persistence_polls >= self.min_persistence]
        # Spoof defense: drop walls that have shrunk >40% in last 90s
        defended = []
        for w in verified:
            shrink = tracker.shrink_pct(coin, w.side, w.price, mid, window_s=90)
            if shrink is None or shrink < self.spoof_filter_shrink:
                defended.append(w)
        if not defended: return []

        bids = sorted([w for w in defended if w.side == 'bid'], key=lambda w: -w.price)
        asks = sorted([w for w in defended if w.side == 'ask'], key=lambda w: w.price)
        if not bids or not asks: return []

        nearest_bid = bids[0]
        nearest_ask = asks[0]
        setups = []

        # BUY at nearest_bid wall (limit at TOP of wall = first contact going down)
        bk = (coin, 'BUY', round(nearest_bid.price, 8))
        if bk not in self._fired or (now_ts - self._fired[bk]) > self.cooldown_s:
            buy_limit = nearest_bid.high
            buy_sl = nearest_bid.low - max(self.sl_atr_mult * atr_v, mid * self.sl_buffer_pct)
            buy_tp = nearest_ask.low - mid * 0.0005
            if buy_limit < mid and buy_sl < buy_limit < buy_tp:
                risk = buy_limit - buy_sl
                reward = buy_tp - buy_limit
                if risk > 0:
                    rr = reward / risk
                    if self.min_rr <= rr <= 12:
                        setups.append(Setup(
                            coin=coin, side='BUY', kind='WALL_BOUNCE', order_type='LIMIT',
                            entry_price=buy_limit, sl_price=buy_sl, tp_price=buy_tp, rr=rr,
                            notes=(f"BID ${nearest_bid.usd/1000:.0f}k@{nearest_bid.price:.6f}"
                                    f"({nearest_bid.persistence_polls}p) → "
                                    f"ASK ${nearest_ask.usd/1000:.0f}k@{nearest_ask.price:.6f}"),
                        ))
                        self._fired[bk] = now_ts

        # SELL at nearest_ask wall (limit at BOTTOM of wall = first contact going up)
        sk = (coin, 'SELL', round(nearest_ask.price, 8))
        if sk not in self._fired or (now_ts - self._fired[sk]) > self.cooldown_s:
            sell_limit = nearest_ask.low
            sell_sl = nearest_ask.high + max(self.sl_atr_mult * atr_v, mid * self.sl_buffer_pct)
            sell_tp = nearest_bid.high + mid * 0.0005
            if sell_limit > mid and sell_tp < sell_limit < sell_sl:
                risk = sell_sl - sell_limit
                reward = sell_limit - sell_tp
                if risk > 0:
                    rr = reward / risk
                    if self.min_rr <= rr <= 12:
                        setups.append(Setup(
                            coin=coin, side='SELL', kind='WALL_BOUNCE', order_type='LIMIT',
                            entry_price=sell_limit, sl_price=sell_sl, tp_price=sell_tp, rr=rr,
                            notes=(f"ASK ${nearest_ask.usd/1000:.0f}k@{nearest_ask.price:.6f}"
                                    f"({nearest_ask.persistence_polls}p) → "
                                    f"BID ${nearest_bid.usd/1000:.0f}k@{nearest_bid.price:.6f}"),
                        ))
                        self._fired[sk] = now_ts

        return setups


class SpoofBreakoutEngine:
    """Sub-engine 2: market entry WITH spoof breakout direction."""

    def __init__(self, min_withdraw_pct: float = 0.70,
                  min_peak_usd: float = 500_000,
                  price_cross_pct: float = 0.0005,
                  sl_buffer_pct: float = 0.0020,
                  atr_buffer_mult: float = 0.2,
                  cooldown_s: int = 300):
        self.min_withdraw = min_withdraw_pct
        self.min_peak_usd = min_peak_usd
        self.price_cross_pct = price_cross_pct
        self.sl_buffer_pct = sl_buffer_pct
        self.atr_buffer_mult = atr_buffer_mult
        self.cooldown_s = cooldown_s
        self._fired: Dict = {}

    def evaluate(self, coin: str, walls: List[Wall], tracker: WallTracker,
                  mid: float, atr_v: float, now_ts: Optional[float] = None) -> List[Setup]:
        if now_ts is None: now_ts = time.time()
        if mid <= 0 or atr_v <= 0: return []
        # Per-coin cooldown
        if coin in self._fired and (now_ts - self._fired[coin]) < self.cooldown_s:
            return []

        # Scan tracker for shrunk walls on this coin
        for k in tracker.all_keys_for(coin):
            h = tracker.history.get(k)
            if not h or len(h) < 4: continue
            try:
                _, side, bucket_str = k.split('|')
                bucket_pct = float(bucket_str)
                bucket_price = bucket_pct * mid
            except (ValueError, IndexError):
                continue

            peak_usd = max(usd for ts, usd in h)
            recent_usd = h[-1][1]
            if peak_usd < self.min_peak_usd: continue
            shrink = 1.0 - (recent_usd / peak_usd)
            if shrink < self.min_withdraw: continue

            # Confirm price has crossed the level
            if side == 'bid':
                # Bid wall pulled. Was support — gone. Bearish breakout.
                if mid >= bucket_price * (1 - self.price_cross_pct): continue
                # SL: back inside the cleared level (above the spoofed bid)
                sl = bucket_price * (1 + self.sl_buffer_pct) + self.atr_buffer_mult * atr_v
                # TP: nearest remaining bid wall below
                bids_left = sorted(
                    [w for w in walls if w.side == 'bid' and w.price < mid and w.persistence_polls >= 3],
                    key=lambda w: -w.price)
                if not bids_left: continue
                target = bids_left[0]
                tp = target.high + mid * 0.0005
                if not (sl > mid > tp): continue
                risk = sl - mid; reward = mid - tp
                if risk <= 0 or reward <= 0: continue
                rr = reward / risk
                if rr < 1.0 or rr > 10: continue
                self._fired[coin] = now_ts
                return [Setup(
                    coin=coin, side='SELL', kind='SPOOF_BREAKOUT', order_type='MARKET',
                    entry_price=mid, sl_price=sl, tp_price=tp, rr=rr,
                    notes=(f"BID spoof @{bucket_price:.6f} "
                            f"peak ${peak_usd/1000:.0f}k → ${recent_usd/1000:.0f}k (-{shrink*100:.0f}%)"),
                )]
            elif side == 'ask':
                if mid <= bucket_price * (1 + self.price_cross_pct): continue
                sl = bucket_price * (1 - self.sl_buffer_pct) - self.atr_buffer_mult * atr_v
                asks_left = sorted(
                    [w for w in walls if w.side == 'ask' and w.price > mid and w.persistence_polls >= 3],
                    key=lambda w: w.price)
                if not asks_left: continue
                target = asks_left[0]
                tp = target.low - mid * 0.0005
                if not (sl < mid < tp): continue
                risk = mid - sl; reward = tp - mid
                if risk <= 0 or reward <= 0: continue
                rr = reward / risk
                if rr < 1.0 or rr > 10: continue
                self._fired[coin] = now_ts
                return [Setup(
                    coin=coin, side='BUY', kind='SPOOF_BREAKOUT', order_type='MARKET',
                    entry_price=mid, sl_price=sl, tp_price=tp, rr=rr,
                    notes=(f"ASK spoof @{bucket_price:.6f} "
                            f"peak ${peak_usd/1000:.0f}k → ${recent_usd/1000:.0f}k (-{shrink*100:.0f}%)"),
                )]
        return []
