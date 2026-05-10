"""SMC v1.0 — locked configuration."""
import os

SMC_CONFIG = {
    'mode': 'confirmation',
    'swing_lookback': 5,
    'sweep_strictness': 'Loose',
    'mss_volume_mult': 1.5,
    'displace_atr': 1.5,
    'fvg_min_atr': 0.3,
    'sl_atr_mult': 2.0,
    'setup_expiry_bars': 20,
    'min_rr_to_take': 1.0,             # was 2.0 — honor real pivots even at 1R
    'long_only': True,
    'skip_session_utc': (0, 5),
    'be_trigger_r': 1.5,               # was 2.5 — earlier BE
    'be_buffer_r': 0.1,                # was 0.2 — tighter BE pad
    'time_stop_hours': 10,             # was 24 — tighter timeout
    'time_stop_progress_r': 1.5,       # was 1.0 — match BE trigger
    'btc_trend_lookback_4h_bars': 40,
    'funding_max_adverse_per_hour': 0.00005,
    'dedupe_window_seconds': 300,
    'force_notional_usd': float(os.environ.get('FORCE_NOTIONAL_USD', os.environ.get('SMC_NOTIONAL_USD', '25'))),
    'max_concurrent_positions': 20,
    'order_type': 'taker_aggressive',  # was 'maker_only' — fix 0% fill rate
    'taker_fallback': True,            # was False
    'limit_expiry_minutes': 300,
    'cooldown_consecutive_losses': None,
    'short_signal_action': 'alert_and_halt',
    'excluded_majors': [
        'BTC','ETH','BNB','SOL','BCH','LTC','XRP','ADA',
        'DOGE','AVAX','DOT','TRX','TON',
    ],
}

REQUIRED_PAYLOAD_FIELDS = [
    'alert_id', 'coin', 'side',
    'sweep_wick', 'ob_top', 'sl_price', 'tp2',
    'atr14', 'rr_to_tp2',
]
