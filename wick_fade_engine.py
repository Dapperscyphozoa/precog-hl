"""
wick_fade_engine.py — Liquidation-wick fade signal generator for HL alts.

Setup logic:
  Bar with extreme wick on one side fades the move next bar.
  - upper wick > 2.5 × body AND > 1.0 × ATR(14) → SHORT (fade liquidated longs)
  - lower wick > 2.5 × body AND > 1.0 × ATR(14) → LONG (fade liquidated shorts)

Validated config (60d in-sample + 60d walk-forward, fees included):
  wick/body=2.5, wick/ATR=1.0, SL=2× ATR (anchored beyond wick), TP=2.5× ATR.
  WR ~46% with 2.5:2 RR → +EV after 0.09% round-trip taker fees.

Payload matches smc_native_engine output so handle_smc_alert can route both
through the same gate sequence and execution path.

Source: derived from full multi-strategy backtest 2026-05-08 against 47-coin
HL alt universe over 120d at 15m. See chat log.
"""
import logging
from collections import deque

log = logging.getLogger(__name__)


def _atr(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(len(closes) - n, len(closes)):
        if i == 0: continue
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


class WickFadeDetector:
    """Per-coin streaming wick-fade detector. on_close(candle) → setup or None."""

    def __init__(self, coin,
                 wick_body_mult=2.5,
                 wick_atr_mult=1.0,
                 sl_atr_mult=2.0,
                 tp_atr_mult=2.5,
                 sl_buffer_atr=0.3,
                 cooldown_bars=24,
                 long_only=True,
                 max_buffer=300):
        self.coin = coin
        self.wick_body_mult = wick_body_mult
        self.wick_atr_mult = wick_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult
        self.sl_buffer_atr = sl_buffer_atr
        self.cooldown_bars = cooldown_bars
        self.long_only = long_only

        # We don't share the SMC state machine — emit on every qualifying bar.
        self.candles = deque(maxlen=max_buffer)
        self.bar_idx = 0
        self.cooldown = 0
        self.state = 'WICK'   # static label for the dashboard

    def on_close(self, candle):
        """Returns SMC-shaped payload on fire, else None."""
        self.candles.append(candle)
        self.bar_idx += 1

        if self.cooldown > 0:
            self.cooldown -= 1
            return None
        if len(self.candles) < 20:
            return None

        cs = list(self.candles)
        highs = [c['h'] for c in cs]
        lows = [c['l'] for c in cs]
        closes = [c['c'] for c in cs]
        opens = [c['o'] for c in cs]

        a = _atr(highs, lows, closes, 14)
        if a is None or a <= 0:
            return None

        op = opens[-1]; cl = closes[-1]; h = highs[-1]; l = lows[-1]
        body = abs(cl - op)
        upper_wick = h - max(cl, op)
        lower_wick = min(cl, op) - l
        if body <= 0:
            body = 1e-9   # tiny doji — treat any wick as huge

        wb = self.wick_body_mult
        wa = self.wick_atr_mult * a

        # Decide direction (or no signal)
        is_long = None
        if upper_wick > wb * body and upper_wick > wa:
            is_long = False   # fade upper wick = SHORT
        elif lower_wick > wb * body and lower_wick > wa:
            is_long = True    # fade lower wick = LONG

        if is_long is None:
            return None

        # Long-only filter (precog-hl is currently long-only on the SMC engine)
        if self.long_only and not is_long:
            return None

        # Entry on next-bar open behaviour: handler enters at submit price.
        # We use current close as the reference entry (live engine submits
        # immediately on alert, so this is the best estimate of fill).
        entry = cl

        if is_long:
            sl = l - self.sl_buffer_atr * a   # below the wick
            tp = entry + self.tp_atr_mult * a
        else:
            sl = h + self.sl_buffer_atr * a
            tp = entry - self.tp_atr_mult * a

        risk = abs(entry - sl)
        if risk <= 0:
            return None
        rr = abs(tp - entry) / risk

        self.cooldown = self.cooldown_bars

        ts = candle['t']
        return {
            'coin': self.coin,
            'side': 'BUY' if is_long else 'SELL',
            'tf': '15',
            'engine': 'WICK_FADE_v1',
            'sweep_wick': h if not is_long else l,
            'ob_top': entry,
            'ob_bot': None,
            'sl_price': sl,
            'tp1': entry + (self.tp_atr_mult * 0.5 * a) * (1 if is_long else -1),
            'tp2': tp,
            'atr14': a,
            'rr_to_tp2': rr,
            'mss_close_ms': ts,
            'alert_id': f"wkf-{self.coin}-{ts}-{'LONG' if is_long else 'SHORT'}",
        }
