"""
smc_native_engine.py — Validated SMC engine, streaming version.

Pure functions over candle history. No HL dependency. No state.
Caller passes in the closed candle list per coin and gets back either:
  - None  (no setup yet)
  - a setup dict {alert_id, coin, side, sweep_wick, ob_top, sl_price, tp1, tp2,
                  atr14, rr_to_tp2, mss_close_ms}
  - or an internal state machine update

State machine per coin:
  NONE → WATCH (zone tagged) → SWEPT (liquidity sweep on a swing) → ARMED (MSS confirmation)

The ARMED transition emits the alert payload. Caller passes that to submit_smc_trade.

Validated config (locked, do NOT change):
    swing_lookback        = 5
    sweep_strictness      = 'Loose'  (vol >= 1.0× SMA20)
    mss_volume_mult       = 1.5
    displace_atr          = 1.5
    fvg_min_atr           = 0.3
    sl_atr_mult           = 2.0
    setup_expiry_bars     = 20
    swept_timeout         = 20
    watch_timeout         = 20
    min_rr_to_take        = 2.0
"""
from collections import deque
from datetime import datetime, timezone

# ─── Indicators ───────────────────────────────────────────────────────────

def atr(highs, lows, closes, period=14):
    n = len(closes)
    if n < 2:
        return [0.0] * n
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1]),
        )
        trs.append(tr)
    out = []
    s = 0.0
    for i, tr in enumerate(trs):
        if i < period:
            s += tr
            out.append(s / (i + 1))
        else:
            out.append((out[-1] * (period - 1) + tr) / period)
    return out


def sma(values, period):
    out = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        out.append(s / min(i + 1, period))
    return out


# ─── Per-coin streaming detector ───────────────────────────────────────────

class SMCDetector:
    """One detector instance per coin. Feed it closed candles in order.
    
    Each candle is a dict: {'t': ms, 'o': float, 'h': float, 'l': float,
                            'c': float, 'v': float}
    
    Call detector.on_close(candle) for every closed bar.
    Returns None or a setup dict on the bar where MSS fires.
    """
    
    def __init__(self, coin,
                 swing_lookback=5,
                 sweep_strictness='Loose',
                 mss_volume_mult=1.5,
                 displace_atr=1.5,
                 fvg_min_atr=0.3,
                 sl_atr_mult=2.0,
                 setup_expiry_bars=20,
                 swept_timeout=20,
                 watch_timeout=20,
                 min_rr_to_take=2.0,
                 long_only=True,
                 buffer_size=200):
        self.coin = coin
        self.swing_lookback = swing_lookback
        self.sweep_vol_mult = {'Loose': 1.0, 'Standard': 1.2, 'Strict': 1.5}[sweep_strictness]
        self.mss_volume_mult = mss_volume_mult
        self.displace_atr = displace_atr
        self.fvg_min_atr = fvg_min_atr
        self.sl_atr_mult = sl_atr_mult
        self.setup_expiry_bars = setup_expiry_bars
        self.swept_timeout = swept_timeout
        self.watch_timeout = watch_timeout
        self.min_rr_to_take = min_rr_to_take
        self.long_only = long_only
        self.buffer_size = buffer_size
        
        # Rolling candle buffer
        self.candles = deque(maxlen=buffer_size)
        # Pivots (rolling)
        self.swing_highs = deque(maxlen=10)   # (idx, price)
        self.swing_lows  = deque(maxlen=10)
        # Active zones (Order Blocks, FVGs)
        self.obs = deque(maxlen=20)            # {top, bot, is_bull}
        self.fvgs = deque(maxlen=20)
        # State machine
        self.state = 'NONE'
        self.state_bar_idx = 0
        self.setup = {}
        # Index counter (incremented per closed candle)
        self.bar_idx = 0
    
    def on_close(self, candle):
        """Process one closed candle. Returns a setup dict on MSS bar, else None."""
        self.candles.append(candle)
        self.bar_idx += 1
        
        # Need enough bars for ATR + pivots
        if len(self.candles) < max(20, self.swing_lookback * 2 + 1):
            return None
        
        return self._evaluate()
    
    def _evaluate(self):
        i = self.bar_idx - 1   # global index of latest candle
        # Local index in deque
        local_i = len(self.candles) - 1
        
        cs = list(self.candles)
        highs  = [c['h'] for c in cs]
        lows   = [c['l'] for c in cs]
        closes = [c['c'] for c in cs]
        opens  = [c['o'] for c in cs]
        vols   = [c['v'] for c in cs]
        
        atr14 = atr(highs, lows, closes, 14)
        vol_avg = sma(vols, 20)
        
        a = atr14[local_i]
        v = vols[local_i]
        va = vol_avg[local_i] if local_i < len(vol_avg) else 0
        h, l, cl, op = highs[local_i], lows[local_i], closes[local_i], opens[local_i]
        
        # ── 1. Detect new pivots (using completed candle 'swing_lookback' back) ──
        ci = local_i - self.swing_lookback
        if ci >= self.swing_lookback:
            ph = highs[ci]
            pl = lows[ci]
            is_ph = all(ph > highs[ci - k] and ph > highs[ci + k]
                        for k in range(1, self.swing_lookback + 1))
            is_pl = all(pl < lows[ci - k] and pl < lows[ci + k]
                        for k in range(1, self.swing_lookback + 1))
            if is_ph:
                self.swing_highs.append((self.bar_idx - self.swing_lookback - 1, ph))
            if is_pl:
                self.swing_lows.append((self.bar_idx - self.swing_lookback - 1, pl))
        
        # ── 2. Detect new OBs (displacement) ──
        if local_i >= 1 and a > 0:
            disp = self.displace_atr * a
            sb = (cl > op) and (cl - op) > disp
            sbe = (cl < op) and (op - cl) > disp
            if sb and closes[local_i - 1] < opens[local_i - 1] and cl > highs[local_i - 1]:
                self.obs.append({'top': opens[local_i - 1], 'bot': lows[local_i - 1], 'is_bull': True})
            if sbe and closes[local_i - 1] > opens[local_i - 1] and cl < lows[local_i - 1]:
                self.obs.append({'top': highs[local_i - 1], 'bot': opens[local_i - 1], 'is_bull': False})
        
        # ── 3. Detect new FVGs ──
        if local_i >= 2 and a > 0:
            ms = self.fvg_min_atr * a
            if lows[local_i] > highs[local_i - 2] and (lows[local_i] - highs[local_i - 2]) >= ms:
                self.fvgs.append({'top': lows[local_i], 'bot': highs[local_i - 2], 'is_bull': True})
            if highs[local_i] < lows[local_i - 2] and (lows[local_i - 2] - highs[local_i]) >= ms:
                self.fvgs.append({'top': lows[local_i - 2], 'bot': highs[local_i], 'is_bull': False})
        
        # ── 4. Mitigate (remove) zones price has retraced through ──
        self.obs = deque(
            (z for z in self.obs if not ((z['is_bull'] and l <= z['bot']) or (not z['is_bull'] and h >= z['top']))),
            maxlen=20,
        )
        self.fvgs = deque(
            (z for z in self.fvgs if not ((z['is_bull'] and l <= z['bot']) or (not z['is_bull'] and h >= z['top']))),
            maxlen=20,
        )
        
        # ── 5. State machine ──
        if self.state == 'NONE' and (self.obs or self.fvgs):
            tagged = False
            if self.obs:
                lo = self.obs[-1]
                if l <= lo['top'] and h >= lo['bot']:
                    tagged = True
            if not tagged and self.fvgs:
                lf = self.fvgs[-1]
                if l <= lf['top'] and h >= lf['bot']:
                    tagged = True
            if tagged:
                self.state = 'WATCH'
                self.state_bar_idx = self.bar_idx
                self.setup = {}
        
        if self.state == 'WATCH':
            last_sh = self.swing_highs[-1][1] if self.swing_highs else None
            last_sl = self.swing_lows[-1][1] if self.swing_lows else None
            
            vol_ok = v >= va * self.sweep_vol_mult
            bull_sweep = (last_sl is not None and l < last_sl and cl > last_sl and vol_ok)
            bear_sweep = (last_sh is not None and h > last_sh and cl < last_sh and vol_ok)
            
            if bull_sweep:
                self.state = 'SWEPT'
                self.state_bar_idx = self.bar_idx
                self.setup = {'is_long': True, 'sweep_wick': l, 'sweep_idx': self.bar_idx, 'atr_at_sweep': a}
            elif bear_sweep:
                self.state = 'SWEPT'
                self.state_bar_idx = self.bar_idx
                self.setup = {'is_long': False, 'sweep_wick': h, 'sweep_idx': self.bar_idx, 'atr_at_sweep': a}
            elif (self.bar_idx - self.state_bar_idx) > self.watch_timeout:
                self.state = 'NONE'
        
        elif self.state == 'SWEPT':
            last_sh = self.swing_highs[-1][1] if self.swing_highs else None
            last_sl = self.swing_lows[-1][1] if self.swing_lows else None
            
            vol_ok = v >= va * self.mss_volume_mult
            disp = self.displace_atr * a
            body = abs(cl - op)
            body_ok = body > disp * 0.4
            
            mss_fired = False
            if self.setup.get('is_long'):
                if last_sh is not None and cl > last_sh and cl > op and vol_ok and body_ok:
                    mss_fired = True
            else:
                if last_sl is not None and cl < last_sl and cl < op and vol_ok and body_ok:
                    mss_fired = True
            
            if mss_fired:
                # Build the setup
                setup = self._build_setup(a)
                ok, reason = self._validate_setup(setup, candle_time_ms=self.candles[-1]['t'])
                self.state = 'NONE'   # transition out regardless
                if ok:
                    return setup
            
            if (self.bar_idx - self.state_bar_idx) > self.swept_timeout and self.state == 'SWEPT':
                self.state = 'NONE'
        
        return None
    
    def _build_setup(self, atr_now):
        """Return setup dict ready for submit_smc_trade."""
        is_long = self.setup['is_long']
        sweep_wick = self.setup['sweep_wick']
        # Find nearest matching OB
        if is_long:
            best_top = None
            best_d = float('inf')
            for z in self.obs:
                if z['is_bull']:
                    d = abs(z['top'] - sweep_wick)
                    if d < best_d:
                        best_d = d
                        best_top = z['top']
            entry = max(best_top, sweep_wick) if best_top else sweep_wick
            sl_ = sweep_wick - atr_now * self.sl_atr_mult
            tp1_ = self.swing_highs[-1][1] if self.swing_highs else None
            tp2_ = self.swing_highs[-2][1] if len(self.swing_highs) >= 2 else None
        else:
            best_bot = None
            best_d = float('inf')
            for z in self.obs:
                if not z['is_bull']:
                    d = abs(z['bot'] - sweep_wick)
                    if d < best_d:
                        best_d = d
                        best_bot = z['bot']
            entry = min(best_bot, sweep_wick) if best_bot else sweep_wick
            sl_ = sweep_wick + atr_now * self.sl_atr_mult
            tp1_ = self.swing_lows[-1][1] if self.swing_lows else None
            tp2_ = self.swing_lows[-2][1] if len(self.swing_lows) >= 2 else None
        
        # Defaults if no swing available
        if tp1_ is None:
            tp1_ = entry + (entry - sl_) * 2 if is_long else entry - (sl_ - entry) * 2
        if tp2_ is None:
            tp2_ = entry + (entry - sl_) * 4 if is_long else entry - (sl_ - entry) * 4
        
        # Clamp TP2 if backward
        if (is_long and tp2_ <= entry) or (not is_long and tp2_ >= entry):
            risk = abs(entry - sl_)
            tp2_ = entry + risk * 3 if is_long else entry - risk * 3
        if (is_long and tp1_ <= entry) or (not is_long and tp1_ >= entry):
            risk = abs(entry - sl_)
            tp1_ = entry + risk * 1.5 if is_long else entry - risk * 1.5
        
        risk = abs(entry - sl_)
        rr = abs(tp2_ - entry) / risk if risk > 0 else 0
        
        return {
            'coin': self.coin,
            'side': 'BUY' if is_long else 'SELL',
            'tf': '15',
            'sweep_wick': sweep_wick,
            'ob_top': entry,
            'ob_bot': None,
            'sl_price': sl_,
            'tp1': tp1_,
            'tp2': tp2_,
            'atr14': atr_now,
            'rr_to_tp2': rr,
            'mss_close_ms': self.candles[-1]['t'],
            'alert_id': f"native-{self.coin}-{self.candles[-1]['t']}-{'LONG' if is_long else 'SHORT'}",
        }
    
    def _validate_setup(self, setup, candle_time_ms):
        is_long = setup['side'] == 'BUY'
        if is_long and setup['ob_top'] <= setup['sl_price']:
            return False, 'entry below SL'
        if not is_long and setup['ob_top'] >= setup['sl_price']:
            return False, 'entry above SL'
        if setup['rr_to_tp2'] < self.min_rr_to_take:
            return False, f"RR {setup['rr_to_tp2']:.2f} < min"
        if is_long and self.long_only is False:
            pass  # allow
        return True, 'ok'


# ─── Self-test ────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Synthetic data: simulate a sweep + MSS
    import random
    random.seed(42)
    
    det = SMCDetector('TEST')
    
    # Generate baseline candles (ranging)
    candles = []
    px = 100.0
    t = 1700000000000
    for i in range(30):
        op = px
        cl = px + random.uniform(-0.5, 0.5)
        h = max(op, cl) + random.uniform(0, 0.3)
        l = min(op, cl) - random.uniform(0, 0.3)
        candles.append({'t': t + i*900000, 'o': op, 'h': h, 'l': l, 'c': cl, 'v': 1000.0})
        px = cl
    
    fired = 0
    for c in candles:
        result = det.on_close(c)
        if result:
            fired += 1
            print(f"Setup fired: {result}")
    
    print(f"Total setups: {fired}, final state: {det.state}, candles processed: {det.bar_idx}")
    print(f"Detector ready. Pivots: highs={len(det.swing_highs)}, lows={len(det.swing_lows)}")
    print(f"Zones: OBs={len(det.obs)}, FVGs={len(det.fvgs)}")
