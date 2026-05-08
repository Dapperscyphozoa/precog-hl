"""V10 — SMC framework engine.

Top-down framework:
  Stage 1: HTF (4H) unmitigated OB detection — multi-candle consolidation
  Stage 2: Price at/near unmitigated 4H zone
  Stage 3: MTF (1H) structure validation (HH/HL or LH/LL chain intact)
  Stage 4: LTF (5m) sweep + MSS + retest of LTF OB
  Stage 5: Wall confluence (multi-venue verified, spoof-filtered) — added live
  Stage 6: Limit entry at OB body edge, SL 1 tick PAST OB WICK EXTREME

Critical mechanic from user correction:
  - OB has TWO regions:
      body_top / body_bottom = the range of candle bodies during consolidation
      wick_top / wick_bottom = the absolute deepest wicks during consolidation
  - Entry is at the body edge (where the actual orders sit)
  - SL is 1 tick past the WICK EXTREME, not past the body
  - The wick extreme is the deepest stop-hunt point during accumulation —
    if price returns past it, the OB itself failed and thesis is dead
"""
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict
import time


@dataclass
class OBZone:
    """Multi-candle order block with body region + wick extreme."""
    type: str             # 'demand' or 'supply'
    timeframe: str        # '4h' / '1h' / '5m'
    body_top: float       # highest body close in consolidation
    body_bottom: float    # lowest body open in consolidation
    wick_top: float       # highest wick during consolidation
    wick_bottom: float    # lowest wick during consolidation
    impulse_size_pct: float  # displacement strength (signal quality)
    consolidation_bars: int
    created_t: int
    created_idx: int
    mitigated: bool = False
    mitigated_idx: Optional[int] = None

    @property
    def near_edge_for_long(self) -> float:
        """For demand zone: top of body region. Where longs enter on retest."""
        return self.body_top

    @property
    def far_wick_for_long(self) -> float:
        """For demand zone: deepest wick. SL goes BELOW this + buffer."""
        return self.wick_bottom

    @property
    def near_edge_for_short(self) -> float:
        """For supply zone: bottom of body region. Where shorts enter on retest."""
        return self.body_bottom

    @property
    def far_wick_for_short(self) -> float:
        """For supply zone: highest wick. SL goes ABOVE this + buffer."""
        return self.wick_top


@dataclass
class Setup:
    coin: str
    side: str
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    rr_to_tp1: float
    rr_to_tp2: float
    htf_zone_top: float
    htf_zone_bottom: float
    ltf_ob_body_top: float
    ltf_ob_body_bottom: float
    ltf_ob_wick_top: float
    ltf_ob_wick_bottom: float
    sweep_wick: float
    sl_distance_pct: float
    notes: str = ''


def find_pivots(bars, left=2, right=2):
    pivots = []
    for i in range(left, len(bars) - right):
        h, l = bars[i]['h'], bars[i]['l']
        if all(h >= bars[j]['h'] for j in range(i-left, i)) and all(h >= bars[j]['h'] for j in range(i+1, i+right+1)):
            pivots.append((i, 'H', h))
        if all(l <= bars[j]['l'] for j in range(i-left, i)) and all(l <= bars[j]['l'] for j in range(i+1, i+right+1)):
            pivots.append((i, 'L', l))
    return sorted(pivots, key=lambda x: x[0])


def atr(bars, period=14):
    if len(bars) < period+1: return 0
    trs = []
    for i in range(len(bars)-period, len(bars)):
        if i == 0: continue
        tr = max(bars[i]['h']-bars[i]['l'], abs(bars[i]['h']-bars[i-1]['c']), abs(bars[i]['l']-bars[i-1]['c']))
        trs.append(tr)
    return sum(trs)/len(trs) if trs else 0


def detect_consolidation_obs(bars, timeframe: str,
                              displacement_atr_mult: float = 1.5,
                              min_consol_bars: int = 2,
                              max_consol_bars: int = 8,
                              max_consol_range_pct: float = 0.025,
                              max_age_bars: int = 120) -> List[OBZone]:
    """Detect MULTI-CANDLE consolidation OBs with body/wick separation.

    Demand OB:
      - Consolidation range of N candles (range < max_consol_range_pct)
      - Followed by upward impulse > 1.5 × ATR
      - body_top = highest body close in consolidation (where orders rest)
      - body_bottom = lowest body open
      - wick_top / wick_bottom = absolute price extremes touched during consolidation
      - Unmitigated: no later candle has CLOSED below wick_bottom

    Supply OB: mirror.
    """
    zones = []
    if len(bars) < 30: return zones
    a = atr(bars, 14)
    if a == 0: return zones

    i = 0
    while i < len(bars) - max_consol_bars - 4:
        found = False
        for consol_len in range(max_consol_bars, min_consol_bars - 1, -1):
            if i + consol_len + 3 >= len(bars): continue
            window = bars[i:i + consol_len]
            wmin_low = min(b['l'] for b in window)
            wmax_high = max(b['h'] for b in window)
            ref = window[-1]['c']
            range_pct = (wmax_high - wmin_low) / ref
            if range_pct > max_consol_range_pct: continue

            after = bars[i + consol_len:i + consol_len + 3]
            if not after: continue

            # Bullish impulse → demand OB
            up_disp = max(b['h'] for b in after) - wmax_high
            up_disp_pct = up_disp / wmax_high if wmax_high > 0 else 0
            if up_disp > displacement_atr_mult * a:
                bodies_high = max(max(b['o'], b['c']) for b in window)
                bodies_low = min(min(b['o'], b['c']) for b in window)
                # Mitigation: candle CLOSE below the WICK BOTTOM
                mit_idx = None
                for j in range(i + consol_len + 3, len(bars)):
                    if bars[j]['c'] < wmin_low:
                        mit_idx = j; break
                age = len(bars) - 1 - i
                if age <= max_age_bars:
                    zones.append(OBZone(
                        type='demand', timeframe=timeframe,
                        body_top=bodies_high, body_bottom=bodies_low,
                        wick_top=wmax_high, wick_bottom=wmin_low,
                        impulse_size_pct=up_disp_pct,
                        consolidation_bars=consol_len,
                        created_t=window[0]['t'], created_idx=i,
                        mitigated=mit_idx is not None, mitigated_idx=mit_idx,
                    ))
                i += consol_len + 1
                found = True
                break

            # Bearish impulse → supply OB
            dn_disp = wmin_low - min(b['l'] for b in after)
            dn_disp_pct = dn_disp / wmin_low if wmin_low > 0 else 0
            if dn_disp > displacement_atr_mult * a:
                bodies_high = max(max(b['o'], b['c']) for b in window)
                bodies_low = min(min(b['o'], b['c']) for b in window)
                # Mitigation: candle CLOSE above the WICK TOP
                mit_idx = None
                for j in range(i + consol_len + 3, len(bars)):
                    if bars[j]['c'] > wmax_high:
                        mit_idx = j; break
                age = len(bars) - 1 - i
                if age <= max_age_bars:
                    zones.append(OBZone(
                        type='supply', timeframe=timeframe,
                        body_top=bodies_high, body_bottom=bodies_low,
                        wick_top=wmax_high, wick_bottom=wmin_low,
                        impulse_size_pct=dn_disp_pct,
                        consolidation_bars=consol_len,
                        created_t=window[0]['t'], created_idx=i,
                        mitigated=mit_idx is not None, mitigated_idx=mit_idx,
                    ))
                i += consol_len + 1
                found = True
                break

        if not found:
            i += 1

    return zones


def get_unmitigated_zone_at(zones: List[OBZone], current_idx: int, price: float,
                              proximity_pct: float = 0.005) -> Tuple[Optional[OBZone], Optional[str]]:
    """Find unmitigated zone where price currently sits (or is approaching).

    Returns (zone, bias) or (None, None).
    bias: 'long' if at demand zone, 'short' if at supply zone.
    """
    cands = []
    for z in zones:
        if z.created_idx >= current_idx: continue
        if z.mitigated and z.mitigated_idx is not None and z.mitigated_idx <= current_idx: continue
        prox_top = z.wick_top * (1 + proximity_pct)
        prox_bot = z.wick_bottom * (1 - proximity_pct)
        if prox_bot <= price <= prox_top:
            cands.append(z)
    if not cands: return None, None
    z = max(cands, key=lambda x: x.created_idx)
    return z, ('long' if z.type == 'demand' else 'short')


def mtf_structure_intact(bars_1h: List[dict], bias: str, lookback: int = 6) -> bool:
    if len(bars_1h) < 30: return False
    pivots = find_pivots(bars_1h, 3, 3)
    if len(pivots) < 4: return True
    recent = pivots[-lookback:] if len(pivots) >= lookback else pivots
    Hs = [(i, p) for i, t, p in recent if t == 'H']
    Ls = [(i, p) for i, t, p in recent if t == 'L']
    if bias == 'long':
        if len(Hs) >= 2 and Hs[-1][1] < Hs[-2][1] * 0.998: return False
        if len(Ls) >= 2 and Ls[-1][1] < Ls[-2][1] * 0.998: return False
        return True
    else:
        if len(Hs) >= 2 and Hs[-1][1] > Hs[-2][1] * 1.002: return False
        if len(Ls) >= 2 and Ls[-1][1] > Ls[-2][1] * 1.002: return False
        return True


def detect_ltf_setup(bars_5m: List[dict], bias: str,
                       hl_pool_lookback: int = 25) -> Optional[dict]:
    """Sweep + MSS + LTF OB sequence on 5m.

    Returns dict with sweep, MSS, and LTF OB body/wick extremes.
    """
    if len(bars_5m) < 30: return None
    pivots = find_pivots(bars_5m, 2, 2)
    if len(pivots) < 4: return None
    a5 = atr(bars_5m, 14)
    if a5 == 0: return None

    if bias == 'long':
        recent_lows = [(i, p) for i, t, p in pivots[-hl_pool_lookback:] if t == 'L']
        if len(recent_lows) < 2: return None
        ssl_idx, ssl_price = recent_lows[-2]

        sweep_idx = None
        sweep_wick = None
        for j in range(ssl_idx + 1, len(bars_5m)):
            b = bars_5m[j]
            if b['l'] < ssl_price and b['c'] > ssl_price:
                sweep_idx = j; sweep_wick = b['l']; break
        if sweep_idx is None: return None

        prior_highs = [p for i, t, p in pivots if t == 'H' and ssl_idx - 15 <= i <= sweep_idx]
        if not prior_highs: return None
        last_lh = max(prior_highs)

        mss_idx = None
        for j in range(sweep_idx + 1, min(sweep_idx + 20, len(bars_5m))):
            b = bars_5m[j]
            body = abs(b['c'] - b['o']); rng = b['h'] - b['l']
            if body == 0 or rng == 0: continue
            if b['c'] > last_lh and body > 0.6 * rng and body > 0.5 * a5:
                mss_idx = j; break
        if mss_idx is None: return None

        # LTF OB consolidation: bearish/sideways bars right before MSS impulse
        ob_window_indices = []
        for j in range(mss_idx - 1, max(0, mss_idx - 6), -1):
            b = bars_5m[j]
            if b['c'] < b['o'] or abs(b['c'] - b['o']) < 0.3 * a5:
                ob_window_indices.insert(0, j)
            else:
                break
        if not ob_window_indices: return None

        ob_bars = [bars_5m[j] for j in ob_window_indices]
        body_top = max(max(b['o'], b['c']) for b in ob_bars)
        body_bottom = min(min(b['o'], b['c']) for b in ob_bars)
        wick_top = max(b['h'] for b in ob_bars)
        wick_bottom = min(b['l'] for b in ob_bars)

        # Sweep wick may extend below the OB consolidation — use the lower of the two
        true_wick_bottom = min(wick_bottom, sweep_wick)

        return {
            'side': 'BUY',
            'sweep_wick': sweep_wick, 'sweep_idx': sweep_idx,
            'mss_idx': mss_idx,
            'ob_body_top': body_top, 'ob_body_bottom': body_bottom,
            'ob_wick_top': wick_top, 'ob_wick_bottom': true_wick_bottom,
            'ob_bars': len(ob_window_indices),
            'ssl_swept': ssl_price, 'mss_break': last_lh,
            'mss_t': bars_5m[mss_idx]['t'],
        }

    else:  # short
        recent_highs = [(i, p) for i, t, p in pivots[-hl_pool_lookback:] if t == 'H']
        if len(recent_highs) < 2: return None
        bsl_idx, bsl_price = recent_highs[-2]

        sweep_idx = None
        sweep_wick = None
        for j in range(bsl_idx + 1, len(bars_5m)):
            b = bars_5m[j]
            if b['h'] > bsl_price and b['c'] < bsl_price:
                sweep_idx = j; sweep_wick = b['h']; break
        if sweep_idx is None: return None

        prior_lows = [p for i, t, p in pivots if t == 'L' and bsl_idx - 15 <= i <= sweep_idx]
        if not prior_lows: return None
        last_hl = min(prior_lows)

        mss_idx = None
        for j in range(sweep_idx + 1, min(sweep_idx + 20, len(bars_5m))):
            b = bars_5m[j]
            body = abs(b['c'] - b['o']); rng = b['h'] - b['l']
            if body == 0 or rng == 0: continue
            if b['c'] < last_hl and body > 0.6 * rng and body > 0.5 * a5:
                mss_idx = j; break
        if mss_idx is None: return None

        ob_window_indices = []
        for j in range(mss_idx - 1, max(0, mss_idx - 6), -1):
            b = bars_5m[j]
            if b['c'] > b['o'] or abs(b['c'] - b['o']) < 0.3 * a5:
                ob_window_indices.insert(0, j)
            else:
                break
        if not ob_window_indices: return None

        ob_bars = [bars_5m[j] for j in ob_window_indices]
        body_top = max(max(b['o'], b['c']) for b in ob_bars)
        body_bottom = min(min(b['o'], b['c']) for b in ob_bars)
        wick_top = max(b['h'] for b in ob_bars)
        wick_bottom = min(b['l'] for b in ob_bars)

        true_wick_top = max(wick_top, sweep_wick)

        return {
            'side': 'SELL',
            'sweep_wick': sweep_wick, 'sweep_idx': sweep_idx,
            'mss_idx': mss_idx,
            'ob_body_top': body_top, 'ob_body_bottom': body_bottom,
            'ob_wick_top': true_wick_top, 'ob_wick_bottom': wick_bottom,
            'ob_bars': len(ob_window_indices),
            'bsl_swept': bsl_price, 'mss_break': last_hl,
            'mss_t': bars_5m[mss_idx]['t'],
        }


def find_next_liquidity(bars_1h: List[dict], side: str, current_price: float) -> Optional[float]:
    """TP1: nearest opposite liquidity pool from 1H pivots."""
    pivots = find_pivots(bars_1h, 3, 3)
    if side == 'BUY':
        recent_highs = sorted([p for i, t, p in pivots[-30:] if t == 'H' and p > current_price * 1.001])
        return recent_highs[0] if recent_highs else None
    else:
        recent_lows = sorted([p for i, t, p in pivots[-30:] if t == 'L' and p < current_price * 0.999], reverse=True)
        return recent_lows[0] if recent_lows else None


def htf_target(zones_4h: List[OBZone], side: str, current_price: float) -> Optional[float]:
    """TP2: nearest opposite-side unmitigated 4H zone."""
    candidates = []
    for z in zones_4h:
        if z.mitigated: continue
        if side == 'BUY' and z.type == 'supply' and z.body_bottom > current_price:
            candidates.append(z.body_bottom)
        elif side == 'SELL' and z.type == 'demand' and z.body_top < current_price:
            candidates.append(z.body_top)
    if not candidates: return None
    return min(candidates) if side == 'BUY' else max(candidates)


def build_setup(coin: str, ltf: dict, htf_zone: OBZone,
                  bars_1h: List[dict], bars_4h: List[dict],
                  zones_4h: List[OBZone],
                  tick_size: float = 0.0001,
                  sl_buffer_ticks: int = 2,
                  sl_min_buffer_pct: float = 0.0005,
                  min_rr_to_tp1: float = 2.0) -> Optional[Setup]:
    """Construct a Setup with SL past OB wick extreme.

    SL placement: wick_extreme ± max(sl_buffer_ticks × tick_size, sl_min_buffer_pct × wick).
    Whichever is larger. Absorbs spread + 2 ticks of noise. Visual-equivalent of
    "just past the wick" without false precision.

    BUY:  sl = wick_bottom - max(2 × tick_size, 0.0005 × wick_bottom)
    SELL: sl = wick_top + max(2 × tick_size, 0.0005 × wick_top)
    """
    side = ltf['side']
    if side == 'BUY':
        entry = ltf['ob_body_top']
        wick = ltf['ob_wick_bottom']
        buffer = max(sl_buffer_ticks * tick_size, sl_min_buffer_pct * wick)
        sl = wick - buffer
    else:
        entry = ltf['ob_body_bottom']
        wick = ltf['ob_wick_top']
        buffer = max(sl_buffer_ticks * tick_size, sl_min_buffer_pct * wick)
        sl = wick + buffer

    tp1 = find_next_liquidity(bars_1h, side, entry)
    tp2 = htf_target(zones_4h, side, entry)
    if tp1 is None and tp2 is None: return None
    if tp1 is None: tp1 = tp2
    if tp2 is None: tp2 = tp1

    if side == 'BUY' and (tp1 <= entry or tp2 <= entry): return None
    if side == 'SELL' and (tp1 >= entry or tp2 >= entry): return None

    risk_pct = abs(entry - sl) / entry
    if risk_pct == 0: return None
    rr1 = abs(tp1 - entry) / entry / risk_pct
    rr2 = abs(tp2 - entry) / entry / risk_pct
    if rr1 < min_rr_to_tp1: return None
    if risk_pct > 0.10: return None  # cap risk per setup at 10% (alts can have wide OBs, but 10% is the ceiling)

    return Setup(
        coin=coin, side=side, entry_price=entry, sl_price=sl,
        tp1_price=tp1, tp2_price=tp2,
        rr_to_tp1=rr1, rr_to_tp2=rr2,
        htf_zone_top=htf_zone.body_top, htf_zone_bottom=htf_zone.body_bottom,
        ltf_ob_body_top=ltf['ob_body_top'], ltf_ob_body_bottom=ltf['ob_body_bottom'],
        ltf_ob_wick_top=ltf['ob_wick_top'], ltf_ob_wick_bottom=ltf['ob_wick_bottom'],
        sweep_wick=ltf['sweep_wick'],
        sl_distance_pct=risk_pct,
        notes=f"LTF-OB body[{ltf['ob_body_bottom']:.6f}-{ltf['ob_body_top']:.6f}] wick[{ltf['ob_wick_bottom']:.6f}-{ltf['ob_wick_top']:.6f}] {ltf['ob_bars']}bars",
    )
