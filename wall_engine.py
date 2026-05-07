"""wall_engine.py — Real-zone trading using live orderbook walls + liquidation feed.

Replaces candle-pattern zones with EVIDENCE-BACKED zones from 6-venue aggregated
order book heatmap (orderbook_ws) plus Binance liquidation cascades (liquidation_ws).

A "real wall" per orderbook_ws criteria:
  - Aggregate ≥ $500k across Bybit + Binance + OKX + Coinbase + Bitget + Kraken
  - Persisted in ≥ 5 of last 10 30s windows (5+ min stability)
  - Within 5% of mid price (bucketed at 0.1% precision)

Trade:
  BUY limit at: nearest_bid_wall.price + 0.05% (jump the queue)
  SL: nearest_bid_wall.price - 0.3% (wall breaks → resting orders eaten → exit)
  TP: nearest_ask_wall.price - 0.05% (target the other real wall)

  SELL limit at: nearest_ask_wall.price - 0.05%
  SL: nearest_ask_wall.price + 0.3%
  TP: nearest_bid_wall.price + 0.05%

Liquidation cascade modifier:
  If liquidation_ws.get_cascade(coin) shows recent long-liq cascade (forced sells):
    → boost BUY size (cascade exhaustion = bottom imminent)
    → cancel SELL setup (don't short into a flush bottom)
  Inverse for short-liq cascade.

If no walls on either side, no setup. Most coins most ticks have no setup.
That's correct — we're trading evidence, not noise.
"""
import os, time
from dataclasses import dataclass
from typing import Optional, List, Dict


@dataclass
class WallSetup:
    coin: str
    side: str           # 'BUY' or 'SELL'
    limit_price: float
    sl_price: float
    tp_price: float
    rr: float
    entry_wall: dict    # the wall we're trading at
    target_wall: dict   # the wall we're targeting
    cascade_boost: float = 1.0   # >1 if liquidation cascade aligns
    notes: str = ''


# Tunable params
WALL_QUEUE_JUMP_PCT = float(os.environ.get('WALL_QUEUE_JUMP_PCT', '0.0005'))   # 0.05% in front
WALL_SL_BUFFER_PCT  = float(os.environ.get('WALL_SL_BUFFER_PCT',  '0.003'))    # 0.3% past wall
MIN_WALL_USD        = float(os.environ.get('MIN_WALL_USD',        '500000'))   # $500k aggregate min
MIN_RR              = float(os.environ.get('WALL_MIN_RR',         '1.8'))
MAX_RR              = float(os.environ.get('WALL_MAX_RR',         '12.0'))
MAX_DISTANCE_PCT    = float(os.environ.get('WALL_MAX_DIST_PCT',   '4.0'))      # ignore walls > 4% from mid
COOLDOWN_S          = int(os.environ.get('WALL_COOLDOWN_S',       '1800'))     # 30min per setup


_FIRED: Dict = {}


def _fired_recently(coin, side, price, now):
    key = (coin, side, round(price, 8))
    if key in _FIRED and (now - _FIRED[key]) < COOLDOWN_S:
        return True
    return False


def _mark_fired(coin, side, price, now):
    _FIRED[(coin, side, round(price, 8))] = now


def evaluate(coin: str, mid_price: float,
             orderbook_ws, liquidation_ws=None) -> List[WallSetup]:
    """Returns 0-2 WallSetups based on real verified walls + liquidation context."""
    if mid_price <= 0:
        return []

    walls = orderbook_ws.get_walls(coin) if orderbook_ws else []
    if not walls:
        return []

    bids = sorted([w for w in walls if w.get('side') == 'bid'
                   and w.get('usd', 0) >= MIN_WALL_USD
                   and w.get('distance_pct', 99) <= MAX_DISTANCE_PCT],
                  key=lambda w: w['distance_pct'])  # nearest first
    asks = sorted([w for w in walls if w.get('side') == 'ask'
                   and w.get('usd', 0) >= MIN_WALL_USD
                   and w.get('distance_pct', 99) <= MAX_DISTANCE_PCT],
                  key=lambda w: w['distance_pct'])

    nearest_bid = bids[0] if bids else None
    nearest_ask = asks[0] if asks else None
    if not nearest_bid or not nearest_ask:
        return []

    now = time.time()
    setups = []

    # Cascade context (if available)
    cascade = liquidation_ws.get_cascade(coin) if liquidation_ws else None
    cascade_dir = cascade.get('fade_direction') if cascade else None

    # BUY setup
    if not _fired_recently(coin, 'BUY', nearest_bid['price'], now):
        buy_limit = nearest_bid['price'] * (1 + WALL_QUEUE_JUMP_PCT)
        buy_sl    = nearest_bid['price'] * (1 - WALL_SL_BUFFER_PCT)
        buy_tp    = nearest_ask['price'] * (1 - WALL_QUEUE_JUMP_PCT)
        if buy_limit < mid_price and buy_sl < buy_limit < buy_tp:
            risk = buy_limit - buy_sl
            reward = buy_tp - buy_limit
            if risk > 0:
                rr = reward / risk
                if MIN_RR <= rr <= MAX_RR:
                    boost = 1.0
                    if cascade_dir == 'BUY':
                        boost = 1.5  # long cascade exhausting = bias toward longs
                    setups.append(WallSetup(
                        coin=coin, side='BUY',
                        limit_price=buy_limit, sl_price=buy_sl, tp_price=buy_tp,
                        rr=rr, entry_wall=nearest_bid, target_wall=nearest_ask,
                        cascade_boost=boost,
                        notes=f"BUY@bid_wall ${nearest_bid['usd']:.0f} → ask_wall ${nearest_ask['usd']:.0f}" + (f" [cascade boost {boost}]" if boost > 1 else "")
                    ))
                    _mark_fired(coin, 'BUY', nearest_bid['price'], now)

    # SELL setup — but skip if there's a long-liq cascade (cascading down = don't short the bottom)
    skip_sell = (cascade_dir == 'BUY')  # long-liq cascade → fade with BUY → don't add SELL
    if not skip_sell and not _fired_recently(coin, 'SELL', nearest_ask['price'], now):
        sell_limit = nearest_ask['price'] * (1 - WALL_QUEUE_JUMP_PCT)
        sell_sl    = nearest_ask['price'] * (1 + WALL_SL_BUFFER_PCT)
        sell_tp    = nearest_bid['price'] * (1 + WALL_QUEUE_JUMP_PCT)
        if sell_limit > mid_price and sell_sl > sell_limit > sell_tp:
            risk = sell_sl - sell_limit
            reward = sell_limit - sell_tp
            if risk > 0:
                rr = reward / risk
                if MIN_RR <= rr <= MAX_RR:
                    boost = 1.0
                    if cascade_dir == 'SELL':
                        boost = 1.5
                    setups.append(WallSetup(
                        coin=coin, side='SELL',
                        limit_price=sell_limit, sl_price=sell_sl, tp_price=sell_tp,
                        rr=rr, entry_wall=nearest_ask, target_wall=nearest_bid,
                        cascade_boost=boost,
                        notes=f"SELL@ask_wall ${nearest_ask['usd']:.0f} → bid_wall ${nearest_bid['usd']:.0f}" + (f" [cascade boost {boost}]" if boost > 1 else "")
                    ))
                    _mark_fired(coin, 'SELL', nearest_ask['price'], now)

    return setups
