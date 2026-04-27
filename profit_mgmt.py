"""Profit management — partial-TP at TP1 + winner-extension on HTF agreement.

Was missing from codebase; precog.py was failing-soft on import. Result:
the partial-TP feature has been dead code. Only TRAIL_LADDER (SL tightening)
was active. This module implements the missing API.

Public API (used by precog.run_profit_management):
    check_partial_exit_tp1(coin, pos_state, mark) -> dict
    check_extend_winner(coin, pos_state, mark, htf_1h_bias, htf_4h_bias,
                        htf_4h_strength) -> dict
    mark_partial_done(coin) -> None

Conservative defaults — won't fire too eagerly. Tunable via env vars.
"""
import os
import time
import threading

# Per-coin state — partial taken? winners extended?
_STATE = {}  # coin → {'partial_done_at': ts, 'extended_at': ts}
_LOCK = threading.Lock()

# ─── tunables ────────────────────────────────────────────────────
# Partial-TP triggers when MFE reaches THIS fraction of TP target.
# Default 0.25 → fires at +1% on 4% TP. Matches System B's partial-TP at
# +1% raw (most common winner-pattern peaks at 1-1.5% before reverting).
# Was 0.5 (fired at +2%) — too late, most moves never reach.
PARTIAL_MFE_FRAC = float(os.environ.get('PARTIAL_MFE_FRAC', '0.25'))

# Fraction of position to close on partial. Default 0.33 = third (matches
# System B's confluence_worker partial-TP at +1% which closes 33%).
PARTIAL_CLOSE_FRACTION = float(os.environ.get('PARTIAL_CLOSE_FRACTION', '0.33'))

# After partial, remainder's SL moves to breakeven + this buffer.
# Default 0.1% → guarantee 0.1% min profit on remainder.
PARTIAL_NEW_SL_PCT = float(os.environ.get('PARTIAL_NEW_SL_PCT', '0.001'))

# Extension: minimum profit (MFE) required before considering extension.
# Default 1% — only extend on real winners.
EXTEND_MIN_MFE = float(os.environ.get('EXTEND_MIN_MFE', '0.010'))

# Extension: how much to widen TP. Multiplier on original TP.
# Default 1.5× → 4% TP becomes 6%.
EXTEND_TP_MULTIPLIER = float(os.environ.get('EXTEND_TP_MULTIPLIER', '1.5'))

# 4h HTF strength threshold for extension. 'strong' or 'medium' from mtf_context.
EXTEND_REQUIRE_STRENGTH = os.environ.get('EXTEND_REQUIRE_STRENGTH', 'strong')


def _is_partial_done(coin):
    with _LOCK:
        return coin in _STATE and 'partial_done_at' in _STATE[coin]


def _is_extended(coin):
    with _LOCK:
        return coin in _STATE and 'extended_at' in _STATE[coin]


def mark_partial_done(coin):
    """Mark `coin` as having taken its partial-TP. Idempotent."""
    if not coin:
        return
    with _LOCK:
        _STATE.setdefault(coin, {})['partial_done_at'] = time.time()


def mark_extended(coin):
    """Mark `coin` as having had its TP extended. Idempotent."""
    if not coin:
        return
    with _LOCK:
        _STATE.setdefault(coin, {})['extended_at'] = time.time()


def reset_coin(coin):
    """Clear state for a coin (e.g. on close/reopen)."""
    if not coin:
        return
    with _LOCK:
        _STATE.pop(coin, None)


def _favourable_pct(pos_state, mark):
    """Compute fav% from entry given position side."""
    try:
        entry = float(pos_state.get('entry', 0))
        if entry <= 0:
            return 0.0
        side = (pos_state.get('side') or '').upper()
        is_long = side in ('L', 'BUY', 'B') or pos_state.get('size', 0) > 0
        return ((mark - entry) / entry) if is_long else ((entry - mark) / entry)
    except Exception:
        return 0.0


def check_partial_exit_tp1(coin, pos_state, mark):
    """Return {'execute', 'reason', 'close_fraction', 'new_sl_pct'} for partial-TP.

    Fires when:
      - Partial not already done on this coin
      - MFE / TP_target >= PARTIAL_MFE_FRAC (default 50% of way to TP)
      - Position is in profit (mark > entry for BUY, mark < entry for SELL)

    Conservative: returns execute=False on any data issue.
    """
    if not pos_state or not mark or _is_partial_done(coin):
        return {'execute': False}

    tp_pct = pos_state.get('tp_pct')
    if not tp_pct or tp_pct <= 0:
        return {'execute': False}

    fav = _favourable_pct(pos_state, mark)
    if fav <= 0:
        return {'execute': False}

    threshold = float(tp_pct) * PARTIAL_MFE_FRAC
    if fav < threshold:
        return {'execute': False}

    return {
        'execute': True,
        'reason': f'fav={fav*100:.2f}% >= {threshold*100:.2f}% ({PARTIAL_MFE_FRAC*100:.0f}% of TP)',
        'close_fraction': PARTIAL_CLOSE_FRACTION,
        'new_sl_pct': PARTIAL_NEW_SL_PCT,
    }


def check_extend_winner(coin, pos_state, mark,
                         htf_1h_bias=None, htf_4h_bias=None, htf_4h_strength=None):
    """Return {'extend', 'reason', 'new_tp_pct', 'original_tp_pct'} for TP extension.

    Fires when:
      - Position MFE >= EXTEND_MIN_MFE (1% default — must be a real winner)
      - 1h HTF bias agrees with position direction
      - 4h HTF bias agrees with position direction
      - 4h strength meets EXTEND_REQUIRE_STRENGTH (default 'strong')
      - Not already extended for this coin

    Conservative: requires both timeframes + strength to agree.
    """
    if not pos_state or not mark or _is_extended(coin):
        return {'extend': False}

    fav = _favourable_pct(pos_state, mark)
    if fav < EXTEND_MIN_MFE:
        return {'extend': False}

    side = (pos_state.get('side') or '').upper()
    is_long = side in ('L', 'BUY', 'B') or pos_state.get('size', 0) > 0
    needed = 'up' if is_long else 'down'

    if htf_1h_bias != needed or htf_4h_bias != needed:
        return {'extend': False}

    # Strength check — accept exact match OR stronger
    strength_ranks = {'weak': 0, 'medium': 1, 'strong': 2}
    needed_rank = strength_ranks.get(EXTEND_REQUIRE_STRENGTH, 2)
    actual_rank = strength_ranks.get((htf_4h_strength or '').lower(), 0)
    if actual_rank < needed_rank:
        return {'extend': False}

    original_tp = float(pos_state.get('tp_pct', 0.04))
    new_tp = original_tp * EXTEND_TP_MULTIPLIER

    mark_extended(coin)
    return {
        'extend': True,
        'reason': f'mfe={fav*100:.2f}% + 1h/{needed} + 4h/{needed}/{htf_4h_strength}',
        'new_tp_pct': new_tp,
        'original_tp_pct': original_tp,
    }


def status():
    """Status for diagnostics."""
    with _LOCK:
        return {
            'tracked_coins': len(_STATE),
            'partial_done_count': sum(1 for v in _STATE.values() if 'partial_done_at' in v),
            'extended_count': sum(1 for v in _STATE.values() if 'extended_at' in v),
            'config': {
                'PARTIAL_MFE_FRAC': PARTIAL_MFE_FRAC,
                'PARTIAL_CLOSE_FRACTION': PARTIAL_CLOSE_FRACTION,
                'PARTIAL_NEW_SL_PCT': PARTIAL_NEW_SL_PCT,
                'EXTEND_MIN_MFE': EXTEND_MIN_MFE,
                'EXTEND_TP_MULTIPLIER': EXTEND_TP_MULTIPLIER,
                'EXTEND_REQUIRE_STRENGTH': EXTEND_REQUIRE_STRENGTH,
            },
        }
