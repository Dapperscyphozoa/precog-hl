"""Timeframe isolation contract — per-TF independence + HTF advisory layer.

RULES:

1. Each TF trade is independent. 15m trades exit on 15m TP/SL only.
   1h trades exit on 1h TP/SL only. 4h trades exit on 4h TP/SL only.

2. Lower TF signals MUST NOT close higher TF trades.

3. Higher TF signals may:
   - block lower TF entries (gating)
   - modify lower TF sizing (boost or cut)
   - NEVER force exits

4. If multiple TFs align → increase position size (not duplicate trades).

---

IMPLEMENTATION CONTRACT

Position metadata tag added at entry: `tf` ∈ {'15m','1h','4h'}.
Close operations must check incoming TF vs position's TF:

    - TP/SL fill on exchange: authorized regardless of TF (it's THIS TF's own TP/SL)
    - Signal-based close: only allowed if incoming_tf == position_tf
    - Queue reversal: same rule — only queue if incoming matches

HTF alignment signal arrives via check_htf_alignment(coin) returning:
    { 'htf_bias': 'BUY' | 'SELL' | 'NEUTRAL',
      'strength': 0-1,
      'timeframe': '4h' | '1h' }

Size multiplier rules:
- HTF aligned with signal → 1.5x size
- HTF neutral → 1.0x (base)
- HTF opposing → 0.5x or block (configurable per strength)

No existing exits are disabled; this module ADDS:
- per-TF authorization gating
- multi-TF alignment bonus
"""
import os, json, time, threading

# Valid timeframes this system recognizes
VALID_TFS = ('15m', '1h', '4h')

# Size multipliers for HTF alignment
ALIGN_MULT = 1.5        # same direction
NEUTRAL_MULT = 1.0      # no strong HTF bias
OPPOSING_MULT = 0.5     # HTF against signal
STRONG_OPPOSING_BLOCK = True   # if HTF strength > 0.8 against signal, reject entry

# Violation counters
_VIOLATIONS = {'cross_tf_close_attempts': 0, 'cross_tf_queue_attempts': 0}
_LOCK = threading.Lock()
_LOG_PREFIX = '[tf_isolation]'


def can_close_cross_tf(position_tf, incoming_tf, reason):
    """Determine if an incoming signal from incoming_tf may close a position
    tagged with position_tf. TP/SL fills always allowed.

    Returns True if authorized, False if cross-TF violation.
    """
    # TP/SL fills from the exchange always authorized — they are the position's own exits
    if reason in ('tp_fill_confirmed', 'sl_fill_confirmed',
                  'init_tp_sl_failure', 'kill_switch_manual',
                  'kill_switch_cb', 'kill_switch_liq'):
        return True

    # Signal-based close: must match TF
    if position_tf and incoming_tf and position_tf != incoming_tf:
        with _LOCK:
            _VIOLATIONS['cross_tf_close_attempts'] += 1
        print(f"{_LOG_PREFIX} REJECT cross-TF close: position={position_tf} "
              f"incoming={incoming_tf} reason={reason}", flush=True)
        return False

    return True


def can_queue_reversal_cross_tf(position_tf, incoming_tf):
    """Determine if a signal reversal from incoming_tf may queue to flip a
    position tagged with position_tf.

    RULE: cannot queue reversal across timeframes. A 15m flip cannot queue a
    reversal of a 4h position. Each TF owns its own reversal queue.
    """
    if position_tf and incoming_tf and position_tf != incoming_tf:
        with _LOCK:
            _VIOLATIONS['cross_tf_queue_attempts'] += 1
        print(f"{_LOG_PREFIX} REJECT cross-TF queue: position={position_tf} "
              f"incoming={incoming_tf}", flush=True)
        return False
    return True


def compute_alignment_multiplier(signal_side, htf_bias, htf_strength=0.5):
    """Compute size multiplier based on signal vs HTF bias alignment.

    Args:
        signal_side: 'BUY' or 'SELL'
        htf_bias: 'BUY' | 'SELL' | 'NEUTRAL'
        htf_strength: 0-1 (how strong the HTF bias is)

    Returns:
        (multiplier, action): multiplier in [0, 1.5], action in
        {'block', 'reduce', 'normal', 'boost'}
    """
    if htf_bias == 'NEUTRAL' or htf_strength < 0.2:
        return (NEUTRAL_MULT, 'normal')

    if htf_bias == signal_side:
        # Aligned — scale boost with strength
        mult = NEUTRAL_MULT + (ALIGN_MULT - NEUTRAL_MULT) * htf_strength
        return (mult, 'boost')

    # Opposing
    if STRONG_OPPOSING_BLOCK and htf_strength > 0.8:
        return (0.0, 'block')
    # Weak-medium opposition → cut size
    mult = NEUTRAL_MULT - (NEUTRAL_MULT - OPPOSING_MULT) * htf_strength
    return (max(mult, OPPOSING_MULT), 'reduce')


def derive_htf_bias(candles_4h, candles_1h=None):
    """Derive a coarse HTF bias from 4h candles (and optional 1h for strength).

    Uses EMA20 distance + recent direction. Returns:
        { 'bias': 'BUY' | 'SELL' | 'NEUTRAL',
          'strength': 0-1,
          'source_tf': '4h' }
    """
    if not candles_4h or len(candles_4h) < 30:
        return {'bias': 'NEUTRAL', 'strength': 0.0, 'source_tf': '4h'}

    closes = [float(c[4]) if isinstance(c, list) else float(c.get('c', 0))
              for c in candles_4h[-30:]]
    if not closes or closes[-1] == 0:
        return {'bias': 'NEUTRAL', 'strength': 0.0, 'source_tf': '4h'}

    # EMA20 on 4h
    ema = sum(closes[:20]) / 20
    k = 2 / (20 + 1)
    for c in closes[20:]:
        ema = c * k + ema * (1 - k)

    price = closes[-1]
    dist_pct = (price - ema) / ema

    # Recent slope (last 5 bars)
    slope = (closes[-1] - closes[-5]) / closes[-5]

    # Bias
    if dist_pct > 0.005 and slope > 0:
        bias = 'BUY'
    elif dist_pct < -0.005 and slope < 0:
        bias = 'SELL'
    else:
        bias = 'NEUTRAL'

    # Strength: distance + slope magnitude, normalized
    strength = min(1.0, abs(dist_pct) * 10 + abs(slope) * 5)

    return {'bias': bias, 'strength': round(strength, 3), 'source_tf': '4h'}


def status():
    with _LOCK:
        v = dict(_VIOLATIONS)
    return {
        'valid_timeframes': list(VALID_TFS),
        'align_multiplier': ALIGN_MULT,
        'neutral_multiplier': NEUTRAL_MULT,
        'opposing_multiplier': OPPOSING_MULT,
        'strong_opposing_blocks': STRONG_OPPOSING_BLOCK,
        'violations': v,
        'rules': {
            '1': 'Each TF trade is independent — its TP/SL govern only',
            '2': 'LTF signals cannot close HTF trades',
            '3_a': 'HTF signals may BLOCK LTF entries',
            '3_b': 'HTF signals may MODIFY LTF sizing',
            '3_c': 'HTF signals CANNOT force exits on LTF trades',
            '4': 'Multi-TF alignment → boosted position size (not duplicate trades)',
        },
    }
