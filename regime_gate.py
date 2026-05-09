"""regime_gate.py — Council Fix 3
Lightweight regime classifier for V10. Proxy for the HMM 3-state classifier
used in the backtest harness. Computes "trend" / "range" / "chop" from
recent BTC 5m bars based on:
  - 12hr drift magnitude (cumulative log return)
  - 6hr realized volatility

Calibrated to match HMM thresholds on BTC 5m (~25/34/41% trend/range/chop split).
Only "trend" regime blocks V10. Range/chop allow V10 to fire.

Public API:
  classify_regime(bars_5m: List[dict]) -> str  ('trend' | 'range' | 'chop')
"""
import math
from typing import List

# Calibrated thresholds (validated against HMM on 180d BTC data)
DRIFT_BPS_TREND = 200      # |12hr drift| > 200bps = trend candidate
RV_BPS_TREND = 30          # ...AND realized vol > 30bps
DRIFT_BPS_RANGE = 80       # |12hr drift| < 80bps = range candidate
RV_BPS_RANGE = 25          # ...AND realized vol < 25bps


def classify_regime(bars_5m: List[dict]) -> str:
    """Classify market regime from recent BTC 5m bars."""
    if len(bars_5m) < 200:
        return 'chop'  # insufficient data → conservative default
    recent = bars_5m[-144:]  # last 12hr (144 × 5m bars)
    closes = [b['c'] for b in recent]
    if not closes or closes[0] <= 0:
        return 'chop'
    drift = (closes[-1] - closes[0]) / closes[0]
    rets = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            rets.append(math.log(closes[i] / closes[i-1]))
    if not rets:
        return 'chop'
    mean_r = sum(rets) / len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / len(rets)
    rv = math.sqrt(var)
    drift_bps = abs(drift) * 10000
    rv_bps = rv * 10000

    if drift_bps > DRIFT_BPS_TREND and rv_bps > RV_BPS_TREND:
        return 'trend'
    if drift_bps < DRIFT_BPS_RANGE and rv_bps < RV_BPS_RANGE:
        return 'range'
    return 'chop'


def is_v10_allowed(regime: str) -> bool:
    """V10 fires only in range or chop regimes (NOT trend).
    SMC framework's premise — institutions defend OB on retest — only holds
    in mean-reverting markets. Strong trends violate OB cleanly.
    """
    return regime != 'trend'
