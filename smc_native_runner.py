"""
smc_native_runner.py — Boot wiring for native SMC signal generation.

Responsibilities:
  1. At startup: fetch HL universe, exclude majors → coin list
  2. Bootstrap each coin's SMCDetector with 50 bars of history (REST snapshot)
  3. Start CandleFeed (WS subscribe to all coins × 15m)
  4. Wire on_setup → handle_smc_alert (full gate sequence)
  5. Expose status() for /smc/status endpoint

This module is OPTIONAL: enabled by env SMC_NATIVE=1.
When enabled, the Pine /smc/alert endpoint becomes redundant but still works
(useful for migration / fallback). Eventually disable Pine alerts on TradingView.
"""
import os
import time
import logging
import threading

from smc_native_engine import SMCDetector
from smc_native_feed import CandleFeed
from smc_native_bootstrap import warmup_detectors

log = logging.getLogger(__name__)


_runner = None


class NativeSMCRunner:
    def __init__(self, on_setup_callback, on_log=None):
        """on_setup_callback: callable(setup_dict) — typically handle_smc_alert"""
        self.on_setup_callback = on_setup_callback
        self.on_log = on_log or log.info
        
        self.universe = []
        self.detectors = {}
        self.feed = None
        self._boot_complete = False
        self._boot_started = False
        self._lock = threading.Lock()
    
    def boot(self):
        with self._lock:
            if self._boot_started:
                return
            self._boot_started = True
        
        # Run boot in background thread so it doesn't block gunicorn worker startup
        threading.Thread(target=self._boot_async, daemon=True, name='smc_native_boot').start()
    
    def _boot_async(self):
        try:
            # Stagger: wait so smc_monitors initial REST calls (refresh_universe, refresh_funding,
            # refresh_btc_trend) finish before we hit the same endpoints. Avoids 429 cascade.
            self.on_log("native_runner: boot delayed 45s to avoid REST 429 cascade with smc_monitors")
            time.sleep(45)
            self.on_log("native_runner: boot starting")
            
            # 1. Load universe
            self.universe = self._load_universe()
            self.on_log(f"native_runner: universe = {len(self.universe)} coins")
            
            # 2. Build detectors
            # 2. Build detectors with config from smc_config (so min_rr_to_take,
            # swing_lookback, displace_atr etc. all flow through to live engine
            # — not just defaults).
            from smc_config import SMC_CONFIG
            det_kwargs = {
                'swing_lookback':     SMC_CONFIG.get('swing_lookback', 5),
                'sweep_strictness':   SMC_CONFIG.get('sweep_strictness', 'Loose'),
                'mss_volume_mult':    SMC_CONFIG.get('mss_volume_mult', 1.5),
                'displace_atr':       SMC_CONFIG.get('displace_atr', 1.5),
                'fvg_min_atr':        SMC_CONFIG.get('fvg_min_atr', 0.3),
                'sl_atr_mult':        SMC_CONFIG.get('sl_atr_mult', 2.0),
                'setup_expiry_bars':  SMC_CONFIG.get('setup_expiry_bars', 20),
                'min_rr_to_take':     SMC_CONFIG.get('min_rr_to_take', 1.0),
                'long_only':          SMC_CONFIG.get('long_only', True),
            }
            self.detectors = {c: SMCDetector(c, **det_kwargs) for c in self.universe}

            # 2b. Build parallel wick-fade detectors. Same coins, separate state.
            from wick_fade_engine import WickFadeDetector
            self.wick_detectors = {c: WickFadeDetector(c) for c in self.universe}
            self.on_log(f"native_runner: built {len(self.wick_detectors)} wick-fade detectors")
            
            # 3. Bootstrap candle history (this takes ~217/5 = 43 seconds throttled)
            self.on_log("native_runner: bootstrapping candle history…")
            warmup_detectors(self.detectors, throttle_per_sec=3,
                             on_log=self.on_log,
                             also=[self.wick_detectors])
            
            # 4. Wrap callback to inject secret (so handle_smc_alert gate 1 passes)
            secret = os.environ.get('WEBHOOK_SECRET', '')
            def wrapped_setup(setup):
                payload = dict(setup)
                payload['secret'] = secret
                # alert_id already set by detector as native-{coin}-{ts}-{LONG|SHORT}
                try:
                    body, status = self.on_setup_callback(payload)
                    self.on_log(f"native_runner: setup→handle_smc_alert {setup['coin']} status={body.get('status') if isinstance(body, dict) else body}")
                except Exception as e:
                    log.exception(f"on_setup_callback raised: {e}")
            
            # 5. Start feed (pass pre-warmed detectors so live candles continue from warmup state)
            self.feed = CandleFeed(
                detectors=self.detectors,    # use pre-warmed detectors from step 3
                wick_detectors=self.wick_detectors,
                interval='15m',
                on_setup=wrapped_setup,
                on_log=self.on_log,
            )
            self.feed.start()
            
            self._boot_complete = True
            self.on_log("native_runner: boot complete, feed live")
        except Exception as e:
            log.exception(f"native_runner: boot failed: {e}")
    
    def _load_universe(self, max_retries=5):
        """Fetch HL meta, exclude majors, return list. Retries on 429."""
        EXCLUDED = {
            'BTC','ETH','BNB','SOL','BCH','LTC','XRP','ADA',
            'DOGE','AVAX','DOT','TRX','TON',
        }
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        
        for attempt in range(max_retries):
            try:
                info = Info(constants.MAINNET_API_URL, skip_ws=True)
                meta = info.meta()
                return [u['name'] for u in meta.get('universe', [])
                        if u.get('name') and u['name'] not in EXCLUDED]
            except Exception as e:
                wait = (2 ** attempt) * 5  # 5, 10, 20, 40, 80 sec
                self.on_log(f"_load_universe attempt {attempt+1}/{max_retries} failed: {str(e)[:200]}; retry in {wait}s")
                if attempt < max_retries - 1:
                    time.sleep(wait)
        log.error("_load_universe: all retries exhausted")
        return []
    
    def stop(self):
        if self.feed:
            self.feed.stop()
    
    def status(self):
        return {
            'enabled': True,
            'boot_started': self._boot_started,
            'boot_complete': self._boot_complete,
            'universe_size': len(self.universe),
            'detectors_count': len(self.detectors),
            'feed_status': self.feed.status() if self.feed else None,
        }


def init_native(on_setup_callback, on_log=None):
    """Entry point. Idempotent."""
    global _runner
    if _runner is None:
        _runner = NativeSMCRunner(on_setup_callback, on_log=on_log)
        _runner.boot()
    return _runner


def get_runner():
    return _runner
