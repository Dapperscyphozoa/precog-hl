"""Hard bounds for every tunable parameter.

The tuner cannot push a param outside these bounds no matter what an
agent proposes. This is the guardrail that prevents a single bad
forensic run from nuking a coin's config.

Format:
    (default, min, max, max_step_per_tune, min_samples_between_tunes)

- default: starting value if DB has no override (also the fallback value
  signal engines use if param read fails)
- min/max: absolute hard bounds, never cross these
- max_step_per_tune: the maximum delta the tuner can apply in one run
- min_samples_between_tunes: N closes on this component before it can
  tune again (prevents oscillation)
"""

# Component → param_name → bounds tuple
BOUNDS = {
    'rsi': {
        'buy_threshold':  (25.0, 10.0, 45.0, 3.0, 5),   # RSI below which BUY allowed
        'sell_threshold': (75.0, 55.0, 90.0, 3.0, 5),
        'period':         (14.0,  5.0, 30.0, 1.0, 10),
    },
    'pivot': {
        'lookback':       (5.0,  2.0, 20.0, 1.0, 10),
    },
    'cooldown': {
        'cd_seconds':     (900.0, 60.0, 7200.0, 300.0, 5),  # 15min default
    },
    'bollinger': {
        'period':         (20.0, 10.0, 50.0, 2.0, 10),
        'std_mult':       (2.0,  1.0,  3.5, 0.25, 10),
        'rsi_buffer':     (5.0,  0.0, 15.0, 1.0, 5),
    },
    'adx': {
        'threshold':      (25.0, 15.0, 40.0, 2.0, 10),
        'period':         (14.0,  7.0, 28.0, 1.0, 10),
    },
    'ema': {
        'fast_period':    (21.0, 10.0, 50.0, 2.0, 10),
        'slow_period':    (200.0, 50.0, 400.0, 10.0, 15),
        'min_distance_pct': (0.01, 0.001, 0.05, 0.002, 5),
    },
    'ob': {  # order block
        'max_age_bars':   (20.0,  5.0, 80.0, 3.0, 5),
        'min_size_pct':   (0.002, 0.0005, 0.01, 0.0005, 5),
        'require_fresh':  (0.0, 0.0, 1.0, 1.0, 5),  # boolean-like
    },
    'wall': {  # liquidity wall
        'min_usd':        (50000.0, 10000.0, 500000.0, 10000.0, 5),
        'distance_pct':   (0.005, 0.001, 0.02, 0.001, 5),
    },
    'cvd': {
        'veto_threshold': (2000.0, 500.0, 20000.0, 500.0, 5),
        'window_bars':    (20.0, 5.0, 60.0, 2.0, 10),
    },
    'fvg': {  # fair value gap
        'min_size_pct':   (0.003, 0.001, 0.02, 0.0005, 5),
        'max_age_bars':   (15.0, 3.0, 50.0, 2.0, 5),
        'require_unfilled': (1.0, 0.0, 1.0, 1.0, 5),
    },
    'fib': {
        'min_retrace':    (0.5,  0.382, 0.786, 0.05, 5),
        'max_retrace':    (0.786, 0.618, 1.0, 0.05, 5),
    },
    'sr': {  # support/resistance
        'buffer_pct':     (0.003, 0.0005, 0.02, 0.0005, 5),
        'touch_count_min': (2.0, 1.0, 5.0, 1.0, 5),
    },
    'structure': {  # BOS/CHoCH
        'min_displacement_pct': (0.005, 0.001, 0.03, 0.001, 5),
        'require_recent':  (1.0, 0.0, 1.0, 1.0, 5),
    },
    'funding': {
        'neg_threshold_bps': (-5.0, -50.0, 0.0, 1.0, 5),
        'pos_threshold_bps': (5.0,  0.0,  50.0, 1.0, 5),
    },
    'session': {
        'asian_mult':     (1.0, 0.3, 1.5, 0.1, 5),
        'london_mult':    (1.0, 0.5, 1.5, 0.1, 5),
        'ny_mult':        (1.0, 0.5, 1.5, 0.1, 5),
    },
    'oi': {
        'min_usd':        (1000000.0, 100000.0, 50000000.0, 100000.0, 5),
        'change_pct_window': (0.05, 0.01, 0.3, 0.01, 5),
    },
    'whale': {
        'min_notional':   (100000.0, 10000.0, 5000000.0, 20000.0, 5),
    },
    'liq': {  # liquidation
        'recent_window_sec': (300.0, 60.0, 1800.0, 60.0, 5),
        'min_notional':   (500000.0, 50000.0, 10000000.0, 100000.0, 5),
    },
    'regime': {
        'vol_low_threshold_atr': (0.5, 0.1, 2.0, 0.1, 5),
        'vol_high_threshold_atr': (2.0, 1.0, 5.0, 0.2, 5),
    },
    'sl': {
        'pct':            (0.02, 0.003, 0.05, 0.003, 5),   # 2% default, bounded 0.3%-5%
    },
    'tp': {
        'pct':            (0.06, 0.004, 0.20, 0.003, 5),   # 6% default, bounded 0.4%-20%
    },
}


def get_bounds(component, param_name):
    """Return (default, min, max, max_step, min_samples) or None if unknown."""
    return BOUNDS.get(component, {}).get(param_name)


def get_default(component, param_name):
    b = get_bounds(component, param_name)
    return b[0] if b else None


def clamp_delta(component, param_name, current_value, proposed_value):
    """Clamp a proposed new value to stay within bounds AND within max_step."""
    b = get_bounds(component, param_name)
    if not b: return None
    default, lo, hi, max_step, _min_samples = b
    base = current_value if current_value is not None else default
    # clamp step size
    delta = proposed_value - base
    if abs(delta) > max_step:
        delta = max_step if delta > 0 else -max_step
    new_val = base + delta
    # clamp absolute bounds
    new_val = max(lo, min(hi, new_val))
    return new_val


def components_list():
    return list(BOUNDS.keys())


def params_for(component):
    return list(BOUNDS.get(component, {}).keys())
