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
            self.on_log("native_runner: boot starting")
            
            # 1. Load universe
            self.universe = self._load_universe()
            self.on_log(f"native_runner: universe = {len(self.universe)} coins")
            
            # 2. Build detectors
            self.detectors = {c: SMCDetector(c) for c in self.universe}
            
            # 3. Bootstrap candle history (this takes ~217/5 = 43 seconds throttled)
            self.on_log("native_runner: bootstrapping candle history…")
            warmup_detectors(self.detectors, throttle_per_sec=5, on_log=self.on_log)
            
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
            
            # 5. Start feed
            self.feed = CandleFeed(
                self.universe,
                interval='15m',
                on_setup=wrapped_setup,
                detector_kwargs={},   # use validated defaults
                on_log=self.on_log,
            )
            self.feed.start()
            
            self._boot_complete = True
            self.on_log("native_runner: boot complete, feed live")
        except Exception as e:
            log.exception(f"native_runner: boot failed: {e}")
    
    def _load_universe(self):
        """Fetch HL meta, exclude majors, return list."""
        EXCLUDED = {
            'BTC','ETH','BNB','SOL','BCH','LTC','XRP','ADA',
            'DOGE','AVAX','DOT','TRX','TON',
        }
        try:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
            info = Info(constants.MAINNET_API_URL, skip_ws=True)
            meta = info.meta()
            return [u['name'] for u in meta.get('universe', [])
                    if u.get('name') and u['name'] not in EXCLUDED]
        except Exception as e:
            log.exception(f"_load_universe failed: {e}")
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
