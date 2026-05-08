"""V9 — fresh wall + breakout sibling. Trade BOTH directions of every wall.

V8 only traded the bounce side per wall. When wall broke, V8 ate full SL.
V9 places paired orders per wall:
  1. Bounce limit AT the wall (V8 behavior preserved)
  2. Breakout trigger PAST the wall (fires market when price closes through)

When one fills, cancel the other. Whichever direction price chooses, we capture.

Wall lifecycle:
  ACTIVE       wall verified (>=5 polls, $500k+, multi-venue) but not yet armed
  ARMED        bounce limit + breakout trigger both placed
  BOUNCE_OPEN  bounce filled, breakout cancelled
  BREAKOUT_OPEN breakout fired, bounce cancelled
  EXPIRED      4h passed without trigger, both cancelled
  DEAD         wall destroyed/spoofed/touched-without-triggering, both cancelled
  TESTED       price has touched wall once → wall is dead to us until it disappears
                and reappears fresh
"""
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from collections import defaultdict, deque


@dataclass
class Wall:
    coin: str
    side: str               # 'bid' or 'ask'
    price: float
    low: float
    high: float
    usd: float
    distance_pct: float
    persistence_polls: int = 1
    first_seen_t: float = 0
    last_seen_t: float = 0
    times_tested: int = 0
    last_test_t: float = 0

    @property
    def wall_id(self) -> str:
        return f"{self.coin}|{self.side}|{round(self.price, 8)}"


@dataclass
class BounceSetup:
    coin: str
    wall_id: str
    side: str               # 'BUY' or 'SELL'
    entry_price: float
    sl_price: float
    tp_price: float
    rr: float
    tier: str = 'MED'       # HIGH / MED / LOW — drives runner-side size + filters
    sibling_breakout_id: Optional[str] = None
    notes: str = ''


@dataclass
class BreakoutTrigger:
    coin: str
    wall_id: str
    side: str               # 'BUY' or 'SELL' = direction of breakout
    trigger_price: float    # price at which to fire market
    sl_price: float         # back inside the wall (failed breakout = invalidation)
    tp_price: float         # next same-side wall in breakout direction
    rr: float
    tier: str = 'MED'
    armed_t: float = 0
    sibling_bounce_id: Optional[str] = None
    notes: str = ''


def cluster_walls(orders: List[dict], mid: float, side: str,
                   bucket_pct: float = 0.0005,
                   min_usd: float = 500_000,
                   max_dist_pct: float = 0.025,
                   return_all_buckets: bool = False):
    """If return_all_buckets=True, returns (walls, all_bucket_usd_list).
    Otherwise returns walls list only."""
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
    all_bucket_usd = []
    for info in buckets.values():
        all_bucket_usd.append(info['usd'])
        if info['usd'] >= min_usd:
            mid_px = (info['low'] + info['high']) / 2
            dist = abs(mid_px - mid) / mid
            walls.append(Wall(coin='', side=side, price=mid_px, low=info['low'],
                               high=info['high'], usd=info['usd'], distance_pct=dist))
    if return_all_buckets:
        return walls, all_bucket_usd
    return walls


class WallTracker:
    """Tracks wall persistence + size history + first-touch test count."""
    def __init__(self, max_history: int = 30):
        self.history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=max_history))
        self.last_seen: Dict[str, float] = {}
        self.touch_counts: Dict[str, int] = defaultdict(int)
        self.last_touch_t: Dict[str, float] = {}
        self.armed: Dict[str, float] = {}  # wall_id → armed_t

    @staticmethod
    def _key(coin: str, side: str, price: float, mid: float) -> str:
        bucket = round(price / mid / 0.0005) * 0.0005
        return f"{coin}|{side}|{bucket:.5f}"

    def update(self, coin: str, walls: List[Wall], mid: float, ts: float,
                last_low: float, last_high: float,
                decay_s: float = 180) -> List[Wall]:
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
            w.times_tested = self.touch_counts.get(k, 0)
            w.last_test_t = self.last_touch_t.get(k, 0)
            # Detect new touch only when price PENETRATED past the wall midpoint —
            # a wick brushing wall.high (bid) or wall.low (ask) is normal noise
            # for walls near mid and shouldn't kill the wall permanently.
            wall_mid = (w.low + w.high) / 2
            if w.side == 'bid' and last_low <= wall_mid:
                if ts - w.last_test_t > 300:
                    self.touch_counts[k] = w.times_tested + 1
                    self.last_touch_t[k] = ts
                    w.times_tested = self.touch_counts[k]
                    w.last_test_t = ts
            if w.side == 'ask' and last_high >= wall_mid:
                if ts - w.last_test_t > 300:
                    self.touch_counts[k] = w.times_tested + 1
                    self.last_touch_t[k] = ts
                    w.times_tested = self.touch_counts[k]
                    w.last_test_t = ts
        # Decay walls not seen in >decay_s
        for k in list(self.last_seen.keys()):
            if k.startswith(f"{coin}|") and k not in seen and ts - self.last_seen[k] > decay_s:
                self.history.pop(k, None)
                self.last_seen.pop(k, None)
                self.touch_counts.pop(k, None)
                self.last_touch_t.pop(k, None)
                self.armed.pop(k, None)
        return walls

    def shrink_pct(self, coin: str, side: str, price: float, mid: float,
                    window_s: float = 90) -> Optional[float]:
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


class PoleEngineV9:
    """Generates paired bounce + breakout orders per fresh wall.

    First-touch only. After wall is touched (price wicks into wall low/high),
    the wall is DEAD until it disappears (>180s gone) and reappears fresh.
    """

    def __init__(self,
                  min_persistence_polls: int = 5,
                  min_rr_bounce: float = 1.5,
                  min_rr_breakout: float = 1.5,
                  sl_atr_mult: float = 0.5,
                  sl_buffer_pct: float = 0.0010,
                  breakout_trigger_pct: float = 0.0015,
                  breakout_sl_inside_pct: float = 0.0050,
                  require_body_close: bool = True,
                  spoof_filter_shrink: float = 0.40,
                  min_r_pct: float = 0.0,
                  # Tiered setup thresholds — see classify_tier()
                  high_rr_threshold: float = 2.0,
                  med_rr_threshold: float = 1.5,
                  low_rr_sl_atr_mult: float = 0.2,
                  low_rr_sl_buffer_pct: float = 0.0005,
                  low_rr_min_persistence: int = 8,
                  low_rr_size_multiplier: float = 2.0,
                  cooldown_s: int = 4 * 3600):
        self.min_persistence = min_persistence_polls
        self.min_rr_bounce = min_rr_bounce
        self.min_rr_breakout = min_rr_breakout
        self.sl_atr_mult = sl_atr_mult
        self.sl_buffer_pct = sl_buffer_pct
        self.breakout_trigger_pct = breakout_trigger_pct
        self.breakout_sl_inside_pct = breakout_sl_inside_pct
        self.require_body_close = require_body_close
        self.spoof_filter_shrink = spoof_filter_shrink
        self.min_r_pct = min_r_pct
        self.high_rr_threshold = high_rr_threshold
        self.med_rr_threshold = med_rr_threshold
        self.low_rr_sl_atr_mult = low_rr_sl_atr_mult
        self.low_rr_sl_buffer_pct = low_rr_sl_buffer_pct
        self.low_rr_min_persistence = low_rr_min_persistence
        self.low_rr_size_multiplier = low_rr_size_multiplier
        self.cooldown_s = cooldown_s
        self._fired: Dict[str, float] = {}

    def classify_tier(self, natural_rr: float) -> str:
        """HIGH / MED / LOW / REJECT based on natural wall-to-wall RR."""
        if natural_rr >= self.high_rr_threshold: return 'HIGH'
        if natural_rr >= self.med_rr_threshold:  return 'MED'
        if natural_rr >= self.min_rr_bounce:     return 'LOW'
        return 'REJECT'

    def _too_close_to_armed(self, coin: str, side: str, trigger_price: float,
                              existing_armed: Optional[List[dict]],
                              dedup_pct: float = 0.005) -> bool:
        if not existing_armed: return False
        for t in existing_armed:
            if t.get('coin') != coin: continue
            if t.get('side') != side: continue
            ex_price = t.get('trigger_price', 0)
            if ex_price <= 0: continue
            if abs(trigger_price - ex_price) / trigger_price <= dedup_pct:
                return True
        return False

    def evaluate(self, coin: str, walls: List[Wall], tracker: WallTracker,
                  mid: float, atr_v: float,
                  now_ts: Optional[float] = None,
                  min_persistence_polls: Optional[int] = None,
                  existing_armed_triggers: Optional[List[dict]] = None
                  ) -> Tuple[List[BounceSetup], List[BreakoutTrigger]]:
        if now_ts is None: now_ts = time.time()
        if not walls or mid <= 0 or atr_v <= 0: return [], []

        # Verified walls only — passed persistence + spoof defense + first-touch
        min_p = min_persistence_polls if min_persistence_polls is not None else self.min_persistence
        verified = []
        for w in walls:
            if w.persistence_polls < min_p: continue
            if w.times_tested > 0: continue  # FIRST-TOUCH ONLY
            shrink = tracker.shrink_pct(coin, w.side, w.price, mid, window_s=90)
            if shrink is not None and shrink >= self.spoof_filter_shrink: continue
            verified.append(w)

        bids = sorted([w for w in verified if w.side == 'bid'], key=lambda w: -w.price)
        asks = sorted([w for w in verified if w.side == 'ask'], key=lambda w: w.price)

        # Need both sides for proper TP targeting
        if not bids or not asks: return [], []

        nearest_bid = bids[0]
        nearest_ask = asks[0]

        bounces = []
        breakouts = []

        # Skip if already fired on this wall in cooldown window
        def _can_fire(wall: Wall) -> bool:
            wid = wall.wall_id
            if wid in self._fired and (now_ts - self._fired[wid]) < self.cooldown_s:
                return False
            return True

        # === NEAREST BID WALL ===
        if _can_fire(nearest_bid):
            wid = nearest_bid.wall_id
            bounce_limit = nearest_bid.high
            # Compute natural wall-to-wall RR first to classify tier
            natural_sl = nearest_bid.low - max(self.sl_atr_mult * atr_v, mid * self.sl_buffer_pct)
            natural_reward = nearest_ask.low - bounce_limit
            natural_risk = bounce_limit - natural_sl
            natural_rr = natural_reward / natural_risk if natural_risk > 0 else 0
            tier = self.classify_tier(natural_rr)

            if tier != 'REJECT':
                # HIGH/MED keep structural TP at opposite wall.
                # LOW = scalp: tighter SL + 1R clean sweep.
                if tier in ('HIGH', 'MED'):
                    bounce_sl = natural_sl
                    bounce_tp = nearest_ask.low - mid * 0.0005  # structural
                else:  # LOW
                    bounce_sl = nearest_bid.low - max(self.low_rr_sl_atr_mult * atr_v,
                                                      mid * self.low_rr_sl_buffer_pct)
                    R = bounce_limit - bounce_sl
                    bounce_tp = bounce_limit + R  # 1R scalp on tightened R
                R = bounce_limit - bounce_sl
                r_pct = R / bounce_limit if bounce_limit > 0 else 0

                # LOW-tier extra filters: bigger wall, longer persistence, with-trend only
                low_tier_pass = True
                if tier == 'LOW':
                    if nearest_bid.persistence_polls < self.low_rr_min_persistence:
                        low_tier_pass = False
                    # Wall size must be 2x what was needed to qualify in the first place
                    # (relative threshold known by caller; we use a proxy: wall.usd vs others)
                    # Note: trend filter applied at runner layer

                if (bounce_limit < mid and bounce_sl < bounce_limit < bounce_tp
                      and r_pct >= self.min_r_pct and low_tier_pass):
                    br_risk = R
                    br_reward = bounce_tp - bounce_limit
                    if br_risk > 0:
                        rr = br_reward / br_risk
                    if self.min_rr_bounce <= rr <= 12:
                        # BREAKOUT: SELL trigger past bottom of bid wall
                        # SL = cluster top reclaim invalidation (structural).
                        # TP: HIGH/MED = next bid wall below (or 1% extension);
                        #     LOW = 1R clean sweep.
                        breakout_trigger = nearest_bid.low * (1 - self.breakout_trigger_pct)
                        breakout_sl = nearest_bid.low * (1 + self.breakout_sl_inside_pct)
                        bo_R = breakout_sl - breakout_trigger
                        bo_r_pct = bo_R / breakout_trigger if breakout_trigger > 0 else 0
                        if tier in ('HIGH', 'MED'):
                            further_bids = [w for w in walls if w.side == 'bid'
                                              and w.persistence_polls >= 3
                                              and w.price < nearest_bid.low * 0.998]
                            breakout_tp = (max(further_bids, key=lambda w: w.price).high
                                            if further_bids else nearest_bid.low * 0.99)
                        else:  # LOW
                            breakout_tp = breakout_trigger - bo_R  # 1R clean sweep
                        if (breakout_sl > breakout_trigger > breakout_tp
                              and bo_r_pct >= self.min_r_pct
                              and not self._too_close_to_armed(coin, 'SELL', breakout_trigger, existing_armed_triggers)):
                            bo_risk = bo_R
                            bo_reward = bo_R
                            if bo_risk > 0:
                                bo_rr = bo_reward / bo_risk
                                if self.min_rr_breakout <= bo_rr <= 12:
                                    bid_breakout = BreakoutTrigger(
                                        coin=coin, wall_id=wid + '|BO',
                                        side='SELL', trigger_price=breakout_trigger,
                                        sl_price=breakout_sl, tp_price=breakout_tp, rr=bo_rr,
                                        armed_t=now_ts,
                                        notes=f"BID-BREAK ${nearest_bid.usd/1000:.0f}k@{nearest_bid.price:.6f}"
                                    )
                                    bid_bounce = BounceSetup(
                                        coin=coin, wall_id=wid,
                                        side='BUY', entry_price=bounce_limit,
                                        sl_price=bounce_sl, tp_price=bounce_tp, rr=rr,
                                        tier=tier,
                                        sibling_breakout_id=bid_breakout.wall_id,
                                        notes=f"BID-BOUNCE [{tier}] ${nearest_bid.usd/1000:.0f}k@{nearest_bid.price:.6f}({nearest_bid.persistence_polls}p) natRR={natural_rr:.2f} → ASK ${nearest_ask.usd/1000:.0f}k@{nearest_ask.price:.6f}"
                                    )
                                    bid_breakout.tier = tier
                                    bid_breakout.sibling_bounce_id = wid
                                    bounces.append(bid_bounce)
                                    breakouts.append(bid_breakout)
                                    self._fired[wid] = now_ts

        # === NEAREST ASK WALL ===
        if _can_fire(nearest_ask):
            wid = nearest_ask.wall_id
            bounce_limit = nearest_ask.low
            # Compute natural wall-to-wall RR first to classify tier
            natural_sl = nearest_ask.high + max(self.sl_atr_mult * atr_v, mid * self.sl_buffer_pct)
            natural_reward = bounce_limit - nearest_bid.high
            natural_risk = natural_sl - bounce_limit
            natural_rr = natural_reward / natural_risk if natural_risk > 0 else 0
            tier = self.classify_tier(natural_rr)

            if tier != 'REJECT':
                if tier in ('HIGH', 'MED'):
                    bounce_sl = natural_sl
                    bounce_tp = nearest_bid.high + mid * 0.0005  # structural
                else:  # LOW
                    bounce_sl = nearest_ask.high + max(self.low_rr_sl_atr_mult * atr_v,
                                                       mid * self.low_rr_sl_buffer_pct)
                    R = bounce_sl - bounce_limit
                    bounce_tp = bounce_limit - R  # 1R scalp on tightened R
                R = bounce_sl - bounce_limit
                r_pct = R / bounce_limit if bounce_limit > 0 else 0

                low_tier_pass = True
                if tier == 'LOW':
                    if nearest_ask.persistence_polls < self.low_rr_min_persistence:
                        low_tier_pass = False

                if (bounce_limit > mid and bounce_tp < bounce_limit < bounce_sl
                      and r_pct >= self.min_r_pct and low_tier_pass):
                    br_risk = R
                    br_reward = bounce_limit - bounce_tp
                    if br_risk > 0:
                        rr = br_reward / br_risk
                        if self.min_rr_bounce <= rr <= 12:
                            # BREAKOUT: BUY trigger past top of ask wall
                            # SL = cluster bottom reclaim invalidation (structural).
                            # TP: HIGH/MED = next ask wall above (or 1% extension);
                            #     LOW = 1R clean sweep.
                            breakout_trigger = nearest_ask.high * (1 + self.breakout_trigger_pct)
                            breakout_sl = nearest_ask.high * (1 - self.breakout_sl_inside_pct)
                            bo_R = breakout_trigger - breakout_sl
                            bo_r_pct = bo_R / breakout_trigger if breakout_trigger > 0 else 0
                            if tier in ('HIGH', 'MED'):
                                further_asks = [w for w in walls if w.side == 'ask'
                                                  and w.persistence_polls >= 3
                                                  and w.price > nearest_ask.high * 1.002]
                                breakout_tp = (min(further_asks, key=lambda w: w.price).low
                                                if further_asks else nearest_ask.high * 1.01)
                            else:  # LOW
                                breakout_tp = breakout_trigger + bo_R  # 1R clean sweep
                            if (breakout_sl < breakout_trigger < breakout_tp
                                  and bo_r_pct >= self.min_r_pct
                                  and not self._too_close_to_armed(coin, 'BUY', breakout_trigger, existing_armed_triggers)):
                                bo_risk = bo_R
                                bo_reward = bo_R
                                if bo_risk > 0:
                                    bo_rr = bo_reward / bo_risk
                                    if self.min_rr_breakout <= bo_rr <= 12:
                                        ask_breakout = BreakoutTrigger(
                                            coin=coin, wall_id=wid + '|BO',
                                            side='BUY', trigger_price=breakout_trigger,
                                            sl_price=breakout_sl, tp_price=breakout_tp, rr=bo_rr,
                                            tier=tier, armed_t=now_ts,
                                            notes=f"ASK-BREAK [{tier}] ${nearest_ask.usd/1000:.0f}k@{nearest_ask.price:.6f}"
                                        )
                                        ask_bounce = BounceSetup(
                                            coin=coin, wall_id=wid,
                                            side='SELL', entry_price=bounce_limit,
                                            sl_price=bounce_sl, tp_price=bounce_tp, rr=rr,
                                            tier=tier,
                                            sibling_breakout_id=ask_breakout.wall_id,
                                            notes=f"ASK-BOUNCE [{tier}] ${nearest_ask.usd/1000:.0f}k@{nearest_ask.price:.6f}({nearest_ask.persistence_polls}p) natRR={natural_rr:.2f} → BID ${nearest_bid.usd/1000:.0f}k@{nearest_bid.price:.6f}"
                                        )
                                        ask_breakout.sibling_bounce_id = wid
                                        bounces.append(ask_bounce)
                                        breakouts.append(ask_breakout)
                                        self._fired[wid] = now_ts

        return bounces, breakouts


class SpoofBreakoutEngine:
    """Sub-engine 2: market entry WITH spoof breakout direction. Unchanged from V8."""

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

    def _too_close_to_armed(self, coin: str, side: str, trigger_price: float,
                              existing_armed: Optional[List[dict]],
                              dedup_pct: float = 0.005) -> bool:
        if not existing_armed: return False
        for t in existing_armed:
            if t.get('coin') != coin: continue
            if t.get('side') != side: continue
            ex_price = t.get('trigger_price', 0)
            if ex_price <= 0: continue
            if abs(trigger_price - ex_price) / trigger_price <= dedup_pct:
                return True
        return False

    def evaluate(self, coin: str, walls: List[Wall], tracker: WallTracker,
                  mid: float, atr_v: float, now_ts: Optional[float] = None) -> List:
        if now_ts is None: now_ts = time.time()
        if mid <= 0 or atr_v <= 0: return []
        if coin in self._fired and (now_ts - self._fired[coin]) < self.cooldown_s:
            return []

        for k in tracker.all_keys_for(coin):
            h = tracker.history.get(k)
            if not h or len(h) < 4: continue
            try:
                _, side, bucket_str = k.split('|')
                bucket_price = float(bucket_str) * mid
            except (ValueError, IndexError):
                continue
            peak_usd = max(usd for ts, usd in h)
            recent_usd = h[-1][1]
            if peak_usd < self.min_peak_usd: continue
            shrink = 1.0 - (recent_usd / peak_usd)
            if shrink < self.min_withdraw: continue

            if side == 'bid':
                if mid >= bucket_price * (1 - self.price_cross_pct): continue
                sl = bucket_price * (1 + self.sl_buffer_pct) + self.atr_buffer_mult * atr_v
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
                return [{
                    'coin': coin, 'side': 'SELL', 'kind': 'SPOOF_BREAKOUT',
                    'entry_price': mid, 'sl_price': sl, 'tp_price': tp, 'rr': rr,
                    'notes': f"BID spoof @{bucket_price:.6f} peak ${peak_usd/1000:.0f}k → ${recent_usd/1000:.0f}k (-{shrink*100:.0f}%)",
                }]
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
                return [{
                    'coin': coin, 'side': 'BUY', 'kind': 'SPOOF_BREAKOUT',
                    'entry_price': mid, 'sl_price': sl, 'tp_price': tp, 'rr': rr,
                    'notes': f"ASK spoof @{bucket_price:.6f} peak ${peak_usd/1000:.0f}k → ${recent_usd/1000:.0f}k (-{shrink*100:.0f}%)",
                }]
        return []
