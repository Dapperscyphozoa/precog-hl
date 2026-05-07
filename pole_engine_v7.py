"""V7 — MM-mimicking limit orders at zone edges. Direction-agnostic.

The thesis (corrected): the MM fills orders by walking price to unfilled
order zones. We don't chase the sweep — we place limit orders AT the zone
edges and get filled WITH the MM. SL is past the entire zone clearing.
TP is the next nearest unfilled zone.

Per cycle (every N min):
  1. Map all unfilled zones
  2. Identify nearest unfilled zone above price (HIGH) and below price (LOW)
  3. Compute pending limit setups:
     - LONG limit at top of LOW zone (we provide liquidity to MM's stop-run sellers)
       SL = bottom of LOW zone - ATR_buffer
       TP = bottom of HIGH zone (the next unfilled zone above)
     - SHORT limit at bottom of HIGH zone (we provide liquidity to MM's stop-run buyers)
       SL = top of HIGH zone + ATR_buffer
       TP = top of LOW zone (next unfilled below)
  4. Both setups returned simultaneously — caller places both as limit orders.
     Price fills one or the other. Whichever fills, the OTHER must be cancelled.
"""
import time
from dataclasses import dataclass
from typing import Optional, List, Tuple


@dataclass
class Zone:
    kind: str; side: str
    top: float; bottom: float; price: float
    created_t: int; timeframe: str
    filled: bool = False; partial: bool = False; fill_t: int = 0


@dataclass
class LimitSetup:
    """A pending limit order setup."""
    side: str                # 'BUY' or 'SELL'
    limit_price: float       # where to place the limit entry
    sl_price: float
    tp_price: float
    rr: float
    entry_zone: Zone         # zone we're entering at
    target_zone: Zone        # zone we're targeting
    notes: str = ''


def find_pivots(bars, left=3, right=3):
    H, L = [], []
    for i in range(left, len(bars) - right):
        h, l = bars[i]['h'], bars[i]['l']
        if all(h >= bars[j]['h'] for j in range(i-left, i)) and \
           all(h >= bars[j]['h'] for j in range(i+1, i+right+1)): H.append(i)
        if all(l <= bars[j]['l'] for j in range(i-left, i)) and \
           all(l <= bars[j]['l'] for j in range(i+1, i+right+1)): L.append(i)
    return H, L


def atr(bars, period=14):
    if len(bars) < period+1: return 0.0
    trs = []
    for i in range(len(bars)-period, len(bars)):
        if i == 0: continue
        tr = max(bars[i]['h']-bars[i]['l'], abs(bars[i]['h']-bars[i-1]['c']), abs(bars[i]['l']-bars[i-1]['c']))
        trs.append(tr)
    return sum(trs)/len(trs) if trs else 0.0


def detect_fvgs(bars, tf, min_h_pct=0.0015):
    zones = []
    for i in range(1, len(bars)-1):
        prev, nxt = bars[i-1], bars[i+1]
        ref = bars[i]['c']
        if prev['h'] < nxt['l']:
            h = (nxt['l']-prev['h'])/ref
            if h >= min_h_pct:
                zones.append(Zone('FVG','low',nxt['l'],prev['h'],(nxt['l']+prev['h'])/2,bars[i]['t'],tf))
        if prev['l'] > nxt['h']:
            h = (prev['l']-nxt['h'])/ref
            if h >= min_h_pct:
                zones.append(Zone('FVG','high',prev['l'],nxt['h'],(prev['l']+nxt['h'])/2,bars[i]['t'],tf))
    return zones


def detect_obs(bars, tf, disp_pct=0.005, min_h_pct=0.002):
    zones = []
    if len(bars) < 10: return zones
    H, L = find_pivots(bars, 2, 2)
    for i in range(2, len(bars)-2):
        cur, nxt = bars[i], bars[i+1]
        if cur['c'] < cur['o']:
            d = (nxt['c']-nxt['o'])/nxt['o']
            if d > disp_pct and nxt['c'] > cur['h']:
                p = [bars[j]['h'] for j in H if j < i]
                if p and nxt['c'] > max(p[-3:]):
                    if (cur['h']-cur['l'])/cur['c'] >= min_h_pct:
                        zones.append(Zone('OB','low',cur['h'],cur['l'],(cur['h']+cur['l'])/2,cur['t'],tf))
        if cur['c'] > cur['o']:
            d = (nxt['o']-nxt['c'])/nxt['o']
            if d > disp_pct and nxt['c'] < cur['l']:
                p = [bars[j]['l'] for j in L if j < i]
                if p and nxt['c'] < min(p[-3:]):
                    if (cur['h']-cur['l'])/cur['c'] >= min_h_pct:
                        zones.append(Zone('OB','high',cur['h'],cur['l'],(cur['h']+cur['l'])/2,cur['t'],tf))
    return zones


def detect_pivots_z(bars, tf):
    z = []
    if len(bars) < 20: return z
    H, L = find_pivots(bars, 3, 3)
    for i in H[-30:]: z.append(Zone('swing_H','high',bars[i]['h'],bars[i]['h'],bars[i]['h'],bars[i]['t'],tf))
    for i in L[-30:]: z.append(Zone('swing_L','low', bars[i]['l'],bars[i]['l'],bars[i]['l'],bars[i]['t'],tf))
    return z


def detect_equal_z(bars, tf, tol=0.0015):
    z = []
    if len(bars) < 20: return z
    H, L = find_pivots(bars, 3, 3)
    sh = [(i,bars[i]['h']) for i in H[-30:]]
    sl = [(i,bars[i]['l']) for i in L[-30:]]
    uh, ul = set(), set()
    for i,(ia,ha) in enumerate(sh):
        if ia in uh: continue
        for ib,hb in sh[i+1:]:
            if ib in uh: continue
            if abs(ha-hb)/ha <= tol:
                z.append(Zone('equal_H','high',max(ha,hb),max(ha,hb),max(ha,hb),bars[max(ia,ib)]['t'],tf))
                uh.add(ia); uh.add(ib); break
    for i,(ia,la) in enumerate(sl):
        if ia in ul: continue
        for ib,lb in sl[i+1:]:
            if ib in ul: continue
            if abs(la-lb)/la <= tol:
                z.append(Zone('equal_L','low',min(la,lb),min(la,lb),min(la,lb),bars[max(ia,ib)]['t'],tf))
                ul.add(ia); ul.add(ib); break
    return z


def update_fills(zones, bars_after):
    for z in zones:
        if z.filled: continue
        for b in bars_after:
            if b['t'] <= z.created_t: continue
            if z.top != z.bottom:
                if z.bottom <= b['c'] <= z.top: z.partial = True
                if z.side == 'low' and b['c'] < z.bottom: z.filled=True; z.fill_t=b['t']; break
                if z.side == 'high' and b['c'] > z.top:  z.filled=True; z.fill_t=b['t']; break
            else:
                if z.side == 'high' and b['h'] > z.price: z.filled=True; z.fill_t=b['t']; break
                if z.side == 'low' and b['l'] < z.price:  z.filled=True; z.fill_t=b['t']; break
    return zones


def nearest_zone(zones, price, side):
    cands = [z for z in zones if not z.filled and z.side == side]
    if side == 'high':
        cands = [z for z in cands if z.price > price]
        return min(cands, key=lambda z: z.price) if cands else None
    cands = [z for z in cands if z.price < price]
    return max(cands, key=lambda z: z.price) if cands else None


class PoleScannerV7:
    def __init__(self, min_rr=1.8, atr_sl_mult=0.8,
                 max_zone_age_h=24, cooldown_s=2*3600):
        self.min_rr = min_rr
        self.atr_sl_mult = atr_sl_mult
        self.max_zone_age_ms = max_zone_age_h * 3600 * 1000
        self.cooldown_s = cooldown_s
        self._fired = {}

    def _build_zones(self, b1h, b15, now_ms):
        zs = []
        zs += detect_obs(b1h, '1h', 0.005, 0.002)
        zs += detect_fvgs(b1h, '1h', 0.0015)
        zs += detect_obs(b15, '15m', 0.004, 0.0015)
        zs += detect_fvgs(b15, '15m', 0.0010)
        zs += detect_pivots_z(b1h, '1h')
        zs += detect_equal_z(b1h, '1h', 0.0015)
        if len(b1h) >= 24:
            prior = b1h[-48:-24] if len(b1h) >= 48 else b1h[:-24]
            if prior:
                zs.append(Zone('PDH','high',max(b['h'] for b in prior),max(b['h'] for b in prior),max(b['h'] for b in prior),prior[0]['t'],'1d'))
                zs.append(Zone('PDL','low', min(b['l'] for b in prior),min(b['l'] for b in prior),min(b['l'] for b in prior),prior[0]['t'],'1d'))
        # Drop stale zones
        zs = [z for z in zs if (now_ms - z.created_t) < self.max_zone_age_ms]
        return zs

    def evaluate(self, coin, b1h, b15, b5, now_ms=None) -> List[LimitSetup]:
        """Returns up to TWO LimitSetups: one BUY (at low zone) + one SELL (at high zone)."""
        if not all([b1h, b15, b5]): return []
        if now_ms is None: now_ms = int(time.time()*1000)
        if len(b1h) < 30 or len(b15) < 30: return []

        last_px = b5[-1]['c']
        zones = self._build_zones(b1h, b15, now_ms)
        zones = update_fills(zones, b15 + b5)

        nearest_low = nearest_zone(zones, last_px, 'low')
        nearest_high = nearest_zone(zones, last_px, 'high')
        if not nearest_low or not nearest_high: return []

        atr_15 = atr(b15, 14)
        if atr_15 == 0: return []
        setups = []

        # BUY setup at top edge of nearest LOW zone
        # Limit at LOW zone top — first contact of price coming down
        ckey_b = (coin, 'BUY', nearest_low.bottom, nearest_low.top)
        if ckey_b not in self._fired or (now_ms - self._fired[ckey_b])/1000 >= self.cooldown_s:
            buy_limit = nearest_low.top
            buy_sl = nearest_low.bottom - self.atr_sl_mult * atr_15
            buy_tp = nearest_high.bottom * 0.999  # bottom edge of next zone above
            # Sanity: limit must be below current price (real limit, not chase)
            if buy_limit < last_px and buy_sl < buy_limit:
                risk = buy_limit - buy_sl
                reward = buy_tp - buy_limit
                if risk > 0 and reward > 0:
                    rr = reward / risk
                    if self.min_rr <= rr <= 12:
                        setups.append(LimitSetup(
                            side='BUY', limit_price=buy_limit,
                            sl_price=buy_sl, tp_price=buy_tp, rr=rr,
                            entry_zone=nearest_low, target_zone=nearest_high,
                            notes=f"BUY@{nearest_low.kind}({buy_limit:.4f}) → {nearest_high.kind}({buy_tp:.4f})"
                        ))

        # SELL setup at bottom edge of nearest HIGH zone
        ckey_s = (coin, 'SELL', nearest_high.bottom, nearest_high.top)
        if ckey_s not in self._fired or (now_ms - self._fired[ckey_s])/1000 >= self.cooldown_s:
            sell_limit = nearest_high.bottom
            sell_sl = nearest_high.top + self.atr_sl_mult * atr_15
            sell_tp = nearest_low.top * 1.001
            if sell_limit > last_px and sell_sl > sell_limit:
                risk = sell_sl - sell_limit
                reward = sell_limit - sell_tp
                if risk > 0 and reward > 0:
                    rr = reward / risk
                    if self.min_rr <= rr <= 12:
                        setups.append(LimitSetup(
                            side='SELL', limit_price=sell_limit,
                            sl_price=sell_sl, tp_price=sell_tp, rr=rr,
                            entry_zone=nearest_high, target_zone=nearest_low,
                            notes=f"SELL@{nearest_high.kind}({sell_limit:.4f}) → {nearest_low.kind}({sell_tp:.4f})"
                        ))

        # Mark fired
        for s in setups:
            ck = (coin, s.side, s.entry_zone.bottom, s.entry_zone.top)
            self._fired[ck] = now_ms

        return setups
