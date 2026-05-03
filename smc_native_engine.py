"""
smc_native_engine.py — Streaming port of the validated original engine (ltf_preconf.py confirmation mode).

PRESERVES EXACT BEHAVIOR of the original BT engine. Same detection logic, same state machine,
same setup criteria. Difference: processes one closed candle at a time via on_close(candle).

State machine: NONE → WATCH → SWEPT → ARMED (waiting for retest fill) → returns setup on fill bar.

The setup payload returned matches the format expected by handle_smc_alert.

Validated config (locked):
  swing_lookback=5, sweep_strictness='Loose', mss_volume_mult=1.5,
  displace_atr=1.5, fvg_min_atr=0.3, sl_atr_mult=2.0,
  setup_expiry_bars=20, watch_timeout=20, swept_timeout=20,
  min_rr_to_take=2.0, max_armed_bars=20.
"""
from collections import deque


def _atr_streaming(highs, lows, closes, period=14):
    n = len(closes)
    if n < 2:
        return [0.0] * n
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
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


def _sma_streaming(values, period):
    out = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        out.append(s / min(i + 1, period))
    return out


SWEEP_VOL_MULT_MAP = {'Loose': 1.0, 'Standard': 1.2, 'Strict': 1.5}


class SMCDetector:
    """Streaming SMC detector. Validated against ltf_preconf.run_ltf_trigger (confirmation mode).

    Per-candle processing via on_close(candle). State machine matches original exactly.
    Returns a setup dict on the bar where ARMED→FILL transition occurs.
    """

    def __init__(self, coin,
                 swing_lookback=5,
                 sweep_strictness='Loose',
                 mss_volume_mult=1.5,
                 displace_atr=1.5,
                 fvg_min_atr=0.3,
                 sl_atr_mult=2.0,
                 setup_expiry_bars=20,
                 watch_timeout=20,
                 swept_timeout=20,
                 max_armed_bars=20,
                 min_rr_to_take=2.0,
                 long_only=True,
                 buffer_size=400):
        self.coin = coin
        self.swing_lookback = swing_lookback
        self.sweep_vol_mult = SWEEP_VOL_MULT_MAP[sweep_strictness]
        self.mss_volume_mult = mss_volume_mult
        self.displace_atr = displace_atr
        self.fvg_min_atr = fvg_min_atr
        self.sl_atr_mult = sl_atr_mult
        self.setup_expiry_bars = setup_expiry_bars
        self.watch_timeout = watch_timeout
        self.swept_timeout = swept_timeout
        self.max_armed_bars = max_armed_bars
        self.min_rr_to_take = min_rr_to_take
        self.long_only = long_only
        self.buffer_size = buffer_size

        # Rolling buffers
        self.candles = deque(maxlen=buffer_size)
        # Pivots: (idx, price)
        self.sh_prices = deque(maxlen=20)
        self.sl_prices = deque(maxlen=20)
        # Active zones
        self.obs = deque(maxlen=30)   # {top, bot, is_bull, idx}
        self.fvgs = deque(maxlen=30)
        # State machine
        self.state = 'NONE'
        self.state_bar = 0
        self.setup = {}
        self.armed_bar = 0
        # Global bar index
        self.bar_idx = 0

    def _last_sh(self):
        return self.sh_prices[-1][1] if self.sh_prices else None

    def _last_sl(self):
        return self.sl_prices[-1][1] if self.sl_prices else None

    def on_close(self, candle):
        """Process one closed candle. Returns setup dict on fill bar, else None."""
        self.candles.append(candle)
        self.bar_idx += 1

        # Need enough bars for ATR (20) + pivot (swing_lookback*2+1)
        min_bars = max(20, self.swing_lookback * 2 + 1)
        if len(self.candles) < min_bars:
            return None

        return self._process()

    def _process(self):
        i = self.bar_idx - 1   # global current
        local_i = len(self.candles) - 1

        cs = list(self.candles)
        highs = [c['h'] for c in cs]
        lows = [c['l'] for c in cs]
        closes = [c['c'] for c in cs]
        opens = [c['o'] for c in cs]
        vols = [c['v'] for c in cs]
        times = [c['t'] for c in cs]

        atr14 = _atr_streaming(highs, lows, closes, 14)
        vol_avg = _sma_streaming(vols, 20)

        a = atr14[local_i]
        v = vols[local_i]
        va = vol_avg[local_i] if local_i < len(vol_avg) else 0
        h = highs[local_i]; l = lows[local_i]; cl = closes[local_i]; op = opens[local_i]

        # ── 1. Detect new pivots ──
        ci = local_i - self.swing_lookback
        if ci >= self.swing_lookback:
            ph = highs[ci]
            pl = lows[ci]
            is_ph = all(ph > highs[ci - k] and ph > highs[ci + k]
                        for k in range(1, self.swing_lookback + 1))
            is_pl = all(pl < lows[ci - k] and pl < lows[ci + k]
                        for k in range(1, self.swing_lookback + 1))
            if is_ph:
                self.sh_prices.append((self.bar_idx - self.swing_lookback - 1, ph))
            if is_pl:
                self.sl_prices.append((self.bar_idx - self.swing_lookback - 1, pl))

        # ── 2. Detect new OBs (displacement-based) ──
        if local_i >= 1 and a > 0:
            disp = self.displace_atr * a
            sb = (cl > op) and (cl - op) > disp
            sbe = (cl < op) and (op - cl) > disp
            if sb and closes[local_i - 1] < opens[local_i - 1] and cl > highs[local_i - 1]:
                self.obs.append({'top': opens[local_i - 1], 'bot': lows[local_i - 1],
                                 'is_bull': True, 'idx': self.bar_idx - 1})
            if sbe and closes[local_i - 1] > opens[local_i - 1] and cl < lows[local_i - 1]:
                self.obs.append({'top': highs[local_i - 1], 'bot': opens[local_i - 1],
                                 'is_bull': False, 'idx': self.bar_idx - 1})

        # ── 3. Detect new FVGs ──
        if local_i >= 2 and a > 0:
            ms = self.fvg_min_atr * a
            if lows[local_i] > highs[local_i - 2] and (lows[local_i] - highs[local_i - 2]) >= ms:
                self.fvgs.append({'top': lows[local_i], 'bot': highs[local_i - 2],
                                  'is_bull': True, 'idx': self.bar_idx})
            if highs[local_i] < lows[local_i - 2] and (lows[local_i - 2] - highs[local_i]) >= ms:
                self.fvgs.append({'top': lows[local_i - 2], 'bot': highs[local_i],
                                  'is_bull': False, 'idx': self.bar_idx})

        # ── 4. Mitigate (remove) zones price has retraced through ──
        self.obs = deque(
            (z for z in self.obs if not ((z['is_bull'] and l <= z['bot']) or (not z['is_bull'] and h >= z['top']))),
            maxlen=30,
        )
        self.fvgs = deque(
            (z for z in self.fvgs if not ((z['is_bull'] and l <= z['bot']) or (not z['is_bull'] and h >= z['top']))),
            maxlen=30,
        )

        # ── 5. State machine ──
        # NONE → WATCH (zone tagged)
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
                self.state_bar = self.bar_idx
                self.setup = {}

        # WATCH → SWEPT (sweep detected)
        if self.state == 'WATCH':
            last_sh = self._last_sh()
            last_sl_v = self._last_sl()
            vok = v >= va * self.sweep_vol_mult
            bull_sweep = last_sl_v is not None and l < last_sl_v and cl > last_sl_v and vok
            bear_sweep = last_sh is not None and h > last_sh and cl < last_sh and vok

            if bull_sweep:
                self.state = 'SWEPT'
                self.state_bar = self.bar_idx
                self.setup = {'is_long': True, 'sweep_wick': l, 'sweep_idx': self.bar_idx, 'atr_at_sweep': a}
            elif bear_sweep:
                self.state = 'SWEPT'
                self.state_bar = self.bar_idx
                self.setup = {'is_long': False, 'sweep_wick': h, 'sweep_idx': self.bar_idx, 'atr_at_sweep': a}
            elif (self.bar_idx - self.state_bar) > self.watch_timeout:
                self.state = 'NONE'

        # SWEPT → ARMED (MSS confirmation)
        elif self.state == 'SWEPT':
            last_sh = self._last_sh()
            last_sl_v = self._last_sl()
            vok = v >= va * self.mss_volume_mult
            disp = self.displace_atr * a
            body = abs(cl - op)
            body_ok = body > disp * 0.4

            mss_fired = False
            if self.setup.get('is_long'):
                if last_sh is not None and cl > last_sh and cl > op and vok and body_ok:
                    mss_fired = True
            else:
                if last_sl_v is not None and cl < last_sl_v and cl < op and vok and body_ok:
                    mss_fired = True

            if mss_fired:
                # Build setup
                is_long = self.setup['is_long']
                sweep_wick = self.setup['sweep_wick']

                if is_long:
                    best_top = None; best_d = float('inf')
                    for z in self.obs:
                        if z['is_bull']:
                            d = abs(z['top'] - sweep_wick)
                            if d < best_d:
                                best_d = d; best_top = z['top']
                    entry = max(best_top, sweep_wick) if best_top else sweep_wick
                    sl_ = sweep_wick - a * self.sl_atr_mult
                    tp1_ = self.sh_prices[-1][1] if self.sh_prices else None
                    tp2_ = self.sh_prices[-2][1] if len(self.sh_prices) >= 2 else None
                else:
                    best_bot = None; best_d = float('inf')
                    for z in self.obs:
                        if not z['is_bull']:
                            d = abs(z['bot'] - sweep_wick)
                            if d < best_d:
                                best_d = d; best_bot = z['bot']
                    entry = min(best_bot, sweep_wick) if best_bot else sweep_wick
                    sl_ = sweep_wick + a * self.sl_atr_mult
                    tp1_ = self.sl_prices[-1][1] if self.sl_prices else None
                    tp2_ = self.sl_prices[-2][1] if len(self.sl_prices) >= 2 else None

                if tp1_ is None:
                    tp1_ = entry + (entry - sl_) * 2 if is_long else entry - (sl_ - entry) * 2
                if tp2_ is None:
                    tp2_ = entry + (entry - sl_) * 4 if is_long else entry - (sl_ - entry) * 4

                risk = abs(entry - sl_)
                rew = abs(tp2_ - entry)
                # Clamp TP2: backward OR rew/risk < 1.5 → set to 3R
                if (is_long and tp2_ <= entry) or (not is_long and tp2_ >= entry):
                    tp2_ = entry + risk * 3 if is_long else entry - risk * 3
                elif rew / risk < 1.5:
                    tp2_ = entry + risk * 3 if is_long else entry - risk * 3
                # Clamp TP1
                if (is_long and tp1_ <= entry) or (not is_long and tp1_ >= entry):
                    tp1_ = entry + risk * 1.5 if is_long else entry - risk * 1.5

                risk = abs(entry - sl_)
                rr = abs(tp2_ - entry) / risk if risk > 0 else 0
                if rr >= self.min_rr_to_take and risk > 0:
                    self.setup.update({
                        'entry': entry, 'sl': sl_, 'tp1': tp1_, 'tp2': tp2_,
                        'mss_idx': self.bar_idx, 'rr_tp2': rr, 'atr_at_arm': a,
                    })
                    self.state = 'ARMED'
                    self.state_bar = self.bar_idx
                    self.armed_bar = self.bar_idx
                else:
                    self.state = 'NONE'

            elif (self.bar_idx - self.state_bar) > self.swept_timeout and self.state == 'SWEPT':
                self.state = 'NONE'

        # ARMED → emit setup on retest fill (or expire)
        elif self.state == 'ARMED':
            # Time expiry
            if (self.bar_idx - self.armed_bar) > self.max_armed_bars:
                self.state = 'NONE'
                return None

            # Retest fill check
            is_long = self.setup.get('is_long')
            entry_px = self.setup['entry']
            retest_hit = (l <= entry_px) if is_long else (h >= entry_px)
            if retest_hit:
                # Build setup payload for handle_smc_alert
                payload = {
                    'coin': self.coin,
                    'side': 'BUY' if is_long else 'SELL',
                    'tf': '15',
                    'sweep_wick': self.setup['sweep_wick'],
                    'ob_top': entry_px,
                    'ob_bot': None,
                    'sl_price': self.setup['sl'],
                    'tp1': self.setup['tp1'],
                    'tp2': self.setup['tp2'],
                    'atr14': self.setup.get('atr_at_arm', a),
                    'rr_to_tp2': self.setup['rr_tp2'],
                    'mss_close_ms': self.candles[-1]['t'],
                    'alert_id': f"native-{self.coin}-{self.candles[-1]['t']}-{'LONG' if is_long else 'SHORT'}",
                }
                self.state = 'NONE'
                # Long-only filter
                if self.long_only and not is_long:
                    return None
                return payload

        return None
