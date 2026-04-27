"""
funding_engine.py — Funding mean-reversion (counter-crowd fade) for chop regime.

EDGE
====
When perpetual funding is extreme, it indicates crowded directional positioning.
- Very positive funding: longs paying shorts heavily → over-leveraged longs →
  vulnerable to liquidation cascades down. Fade short.
- Very negative funding: shorts paying longs → squeeze setup. Fade long.

This is a CHOP-REGIME play. In trends, funding stays extreme for days as the
trend continues — going counter-crowd in a trend = getting steamrolled. So we
gate on regime=chop where the structural direction is unclear and crowded
positioning is more likely to revert than continue.

CROSS-VENUE CONFIRMATION
========================
Single-venue extreme funding can be local distortion (HL-specific position
imbalance). When BOTH HL and Binance show same-sign extreme funding, the crowd
is broad-market — the highest-confidence reversion setup.

If HL extreme but Binance opposite-sign → arb opportunity, not mean-reversion;
we skip (different strategy entirely).

THRESHOLDS
==========
HL funding is reported as hourly rate. 0.01%/hr = 0.24%/day = strong crowd.
Binance funding is 8h rate; we normalize to hourly for comparison.

Extreme threshold: 0.01%/hr (env: FUNDING_MR_THRESHOLD)
Cooldown: 4 hours/coin (env: FUNDING_MR_COOLDOWN)

Default DISABLED via FUNDING_MR_ENABLED=0. Enable after observing logged
signals during chop windows.

PUBLIC API
==========
check(coin, regime) -> (side: 'BUY'|'SELL'|None, context: dict)
status() -> diagnostics dict
"""

import os
import time
import threading

# Lazy import — funding_arb may not be ready at module import
_funding_arb = None


def _get_funding_arb():
    global _funding_arb
    if _funding_arb is None:
        try:
            import funding_arb as _f
            _funding_arb = _f
        except Exception:
            _funding_arb = False
    return _funding_arb or None


# ─── Configuration ───────────────────────────────────────────────────────
ENABLED              = os.environ.get('FUNDING_MR_ENABLED', '0') == '1'
EXTREME_THRESHOLD    = float(os.environ.get('FUNDING_MR_THRESHOLD', '0.0001'))   # 0.01%/hr = 0.24%/day
CONFIRM_THRESHOLD    = float(os.environ.get('FUNDING_MR_CONFIRM', '0.00005'))    # Binance must be > half of extreme
COOLDOWN_SEC         = int(os.environ.get('FUNDING_MR_COOLDOWN', '14400'))       # 4h/coin
REGIME_CHOP_ONLY     = os.environ.get('FUNDING_MR_CHOP_ONLY', '0') == '1'
# 2026-04-27: default flipped 1 → 0. FUNDING_MR has been our highest-WR
# engine (80%/5 in recent window). The chop_only gate sidelined it
# entirely when regime shifted to bull-calm — 702/702 checks rejected
# during the shift. Funding-rate-extreme mean-reversion is valid signal
# in any regime; chop is a confidence boost, not a strict prerequisite.
# Set FUNDING_MR_CHOP_ONLY=1 in env to revert to chop-only.
REQUIRE_CONFIRMATION = os.environ.get('FUNDING_MR_REQUIRE_CONFIRM', '0') == '1'  # require both venues agree

# ─── State ─────────────────────────────────────────────────────────────────
_LAST_FIRED = {}    # coin -> ts
_LOCK = threading.Lock()

_STATS = {
    'check_calls':           0,
    'no_funding_data':       0,
    'below_threshold':       0,
    'venue_disagreement':    0,   # HL vs Binance opposite signs
    'confirmation_missing':  0,
    'wrong_regime':          0,
    'on_cooldown':           0,
    'fires_hl_only':         0,
    'fires_confirmed':       0,
    'errors':                0,
}


import sys as _sys
def _log_err(msg):
    print(f"[funding_engine ERR] {msg}", file=_sys.stderr, flush=True)


def check(coin, regime='unknown'):
    """Evaluate funding mean-reversion signal for `coin`.

    Returns:
      (side: 'BUY'|'SELL', ctx: dict) when funding is extreme + regime aligned
      (None, None)                    otherwise
    """
    _STATS['check_calls'] += 1

    if not ENABLED:
        return None, None

    fa = _get_funding_arb()
    if fa is None:
        return None, None

    # Gate on regime — chop only by default
    if REGIME_CHOP_ONLY and regime != 'chop':
        _STATS['wrong_regime'] += 1
        return None, None

    # Cooldown
    if time.time() - _LAST_FIRED.get(coin, 0) < COOLDOWN_SEC:
        _STATS['on_cooldown'] += 1
        return None, None

    # Read both funding rates
    try:
        with fa._LOCK:
            hl_rate = fa._CACHE['hl'].get(coin)
            bn_rate_8h = fa._CACHE['binance'].get(coin)
    except Exception as e:
        _STATS['errors'] += 1
        _log_err(f"funding_arb access ({coin}): {type(e).__name__}: {e}")
        return None, None

    if hl_rate is None:
        _STATS['no_funding_data'] += 1
        return None, None

    hl_rate = float(hl_rate)

    # HL funding must be extreme
    if abs(hl_rate) < EXTREME_THRESHOLD:
        _STATS['below_threshold'] += 1
        return None, None

    # Cross-venue confirmation (Binance is 8h rate, normalize to hourly)
    bn_rate_hr = (float(bn_rate_8h) / 8.0) if bn_rate_8h is not None else None
    confidence = 'HL_ONLY'

    if bn_rate_hr is not None:
        # If venues disagree on sign → arb territory, not mean-reversion
        if (hl_rate > 0) != (bn_rate_hr > 0):
            _STATS['venue_disagreement'] += 1
            return None, None
        # If both extreme and same sign → CONFIRMED (broad crowd)
        if abs(bn_rate_hr) >= CONFIRM_THRESHOLD:
            confidence = 'CONFIRMED'

    if REQUIRE_CONFIRMATION and confidence != 'CONFIRMED':
        _STATS['confirmation_missing'] += 1
        return None, None

    # Direction: counter to funding sign
    side = 'SELL' if hl_rate > 0 else 'BUY'

    _LAST_FIRED[coin] = time.time()
    if confidence == 'CONFIRMED':
        _STATS['fires_confirmed'] += 1
    else:
        _STATS['fires_hl_only'] += 1

    ctx = {
        'hl_funding_hr_pct':      round(hl_rate * 100, 5),
        'hl_funding_daily_pct':   round(hl_rate * 24 * 100, 3),
        'bn_funding_hr_pct':      round(bn_rate_hr * 100, 5) if bn_rate_hr is not None else None,
        'confidence':             confidence,
        'reason':                 f"funding {'+' if hl_rate > 0 else ''}{hl_rate*24*100:.2f}%/day → fade {side}",
    }
    return side, ctx


def status():
    out = dict(_STATS)
    n = max(1, out['check_calls'])
    out['success_rate_pct'] = round((1 - out['errors']/n) * 100, 2)
    out.update({
        'enabled':              ENABLED,
        'extreme_threshold_hr_pct': EXTREME_THRESHOLD * 100,
        'extreme_threshold_daily_pct': EXTREME_THRESHOLD * 24 * 100,
        'cooldown_sec':         COOLDOWN_SEC,
        'regime_chop_only':     REGIME_CHOP_ONLY,
        'require_confirmation': REQUIRE_CONFIRMATION,
        'tracked_cooldowns':    len(_LAST_FIRED),
    })
    return out


def get_top_funding_extremes(n=10, universe=None):
    """Diagnostics: return the N coins with most extreme HL funding right now.
    If `universe` is provided (iterable of HL coin names), filter to that set —
    this is what would actually fire if FUNDING_MR_ENABLED=1, since the engine
    is only called from precog cascade for in-universe coins.
    Returns the global view if universe is None.
    """
    fa = _get_funding_arb()
    if fa is None:
        return []
    try:
        with fa._LOCK:
            hl = dict(fa._CACHE['hl'])
            bn = dict(fa._CACHE['binance'])
    except Exception as e:
        _STATS['errors'] += 1
        _log_err(f"diagnostics funding_arb snapshot: {type(e).__name__}: {e}")
        return []
    universe_set = set(universe) if universe else None
    rows = []
    for coin, rate in hl.items():
        if universe_set is not None and coin not in universe_set:
            continue
        try:
            r = float(rate)
        except Exception:
            continue
        bn_rate_hr = (float(bn.get(coin, 0)) / 8.0) if bn.get(coin) is not None else None
        rows.append({
            'coin': coin,
            'hl_hr_pct': round(r * 100, 5),
            'hl_daily_pct': round(r * 24 * 100, 3),
            'bn_hr_pct': round(bn_rate_hr * 100, 5) if bn_rate_hr is not None else None,
            'would_fire_side': ('SELL' if r > 0 else 'BUY') if abs(r) >= EXTREME_THRESHOLD else None,
        })
    rows.sort(key=lambda x: abs(x['hl_hr_pct']), reverse=True)
    return rows[:n]
