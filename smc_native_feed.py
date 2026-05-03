"""
smc_native_feed.py — HL websocket candle feed (CORRECTED).

HL's candle channel updates the SAME bar in-place until a new bar opens.
We detect close by observing 't' (open_ms) changing.

Strategy:
  - For each coin, track current bar (latest snapshot of t/o/h/l/c/v).
  - When a msg arrives with a NEW 't' (different from current_bar['t']),
    the previous bar is now finalised. Feed it to the detector.
  - Replace current_bar with the new one.
  - On startup, the first msg per coin just initialises current_bar (no close yet).
"""
import json
import logging
import threading
import time
from collections import deque

import websocket

from smc_native_engine import SMCDetector

log = logging.getLogger(__name__)

HL_WS_URL = 'wss://api.hyperliquid.xyz/ws'


class CandleFeed:
    def __init__(self, coins, interval='15m', on_setup=None,
                 detector_kwargs=None, on_log=None):
        self.coins = list(coins)
        self.interval = interval
        self.on_setup = on_setup or (lambda s: log.info(f"setup: {s}"))
        self.detector_kwargs = detector_kwargs or {}
        self.log = on_log or log.info
        
        self.detectors = {c: SMCDetector(c, **self.detector_kwargs) for c in self.coins}
        # Latest in-progress bar per coin
        self.current_bar = {c: None for c in self.coins}
        # Track recent close times for stats
        self.last_close_t = {c: 0 for c in self.coins}
        
        self.stats = {
            'msgs': 0, 'closes_processed': 0, 'setups_fired': 0,
            'last_msg_ts': 0, 'reconnects': 0, 'errors': 0,
        }
        
        self._stop = False
        self._ws = None
        self._thread = None
        self._lock = threading.Lock()
    
    def _on_open(self, ws):
        self.log(f"smc_native_feed: WS open, subscribing {len(self.coins)} coins × {self.interval}")
        # Send subscriptions in batches with small delay to avoid burst-throttle
        for i, coin in enumerate(self.coins):
            sub = {
                'method': 'subscribe',
                'subscription': {'type': 'candle', 'coin': coin, 'interval': self.interval},
            }
            try:
                ws.send(json.dumps(sub))
            except Exception as e:
                self.log(f"sub err {coin}: {e}")
            if i % 50 == 49:
                time.sleep(0.5)   # throttle every 50 subs
    
    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            self.stats['errors'] += 1
            return
        
        if msg.get('channel') != 'candle':
            return
        
        data = msg.get('data') or {}
        coin = data.get('s')
        if coin not in self.detectors:
            return
        
        with self._lock:
            self.stats['msgs'] += 1
            self.stats['last_msg_ts'] = time.time()
        
        try:
            new_bar = {
                't': int(data['t']),
                'o': float(data['o']),
                'h': float(data['h']),
                'l': float(data['l']),
                'c': float(data['c']),
                'v': float(data['v']),
            }
        except (KeyError, ValueError, TypeError):
            self.stats['errors'] += 1
            return
        
        cur = self.current_bar[coin]
        
        # CLOSE DETECTION: if new bar's open_t differs from current_bar's open_t, the cur is now closed.
        if cur is not None and new_bar['t'] != cur['t']:
            # Cur is finalised
            closed_bar = cur
            self.last_close_t[coin] = closed_bar['t']
            self.stats['closes_processed'] += 1
            
            try:
                detector = self.detectors[coin]
                setup = detector.on_close(closed_bar)
                if setup:
                    self.stats['setups_fired'] += 1
                    self.log(
                        f"smc_native_feed: SETUP {coin} {setup['side']} "
                        f"ob_top={setup['ob_top']:.6f} sl={setup['sl_price']:.6f} "
                        f"tp2={setup['tp2']:.6f} rr={setup['rr_to_tp2']:.2f}"
                    )
                    try:
                        self.on_setup(setup)
                    except Exception as e:
                        log.exception(f"on_setup callback raised: {e}")
            except Exception as e:
                self.stats['errors'] += 1
                log.exception(f"detector.on_close raised for {coin}: {e}")
        
        # Always update current_bar to latest snapshot
        self.current_bar[coin] = new_bar
    
    def _on_error(self, ws, err):
        self.stats['errors'] += 1
        self.log(f"smc_native_feed: WS error: {err}")
    
    def _on_close(self, ws, code, msg):
        self.log(f"smc_native_feed: WS closed code={code} msg={msg}")
    
    def _run(self):
        backoff = 1
        while not self._stop:
            try:
                self._ws = websocket.WebSocketApp(
                    HL_WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                log.exception(f"WS run_forever raised: {e}")
            
            if self._stop:
                break
            
            self.stats['reconnects'] += 1
            self.log(f"smc_native_feed: reconnecting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
    
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True, name='smc_feed')
        self._thread.start()
        self.log(f"smc_native_feed: thread started")
    
    def stop(self):
        self._stop = True
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
    
    def status(self):
        with self._lock:
            return {
                'coins': len(self.coins),
                'msgs': self.stats['msgs'],
                'closes_processed': self.stats['closes_processed'],
                'setups_fired': self.stats['setups_fired'],
                'last_msg_age_sec': (time.time() - self.stats['last_msg_ts']) if self.stats['last_msg_ts'] else None,
                'reconnects': self.stats['reconnects'],
                'errors': self.stats['errors'],
                'detectors_states': {
                    c: d.state for c, d in self.detectors.items() if d.state != 'NONE'
                },
            }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    
    coins = ['JUP', 'JTO', 'SOL', 'INJ']
    feed = CandleFeed(coins, on_setup=lambda s: print(f"SETUP: {s['coin']}"), on_log=print)
    feed.start()
    
    print("Running 90s — wait for 15m boundary if you want to see a real close")
    for i in range(9):
        time.sleep(10)
        st = feed.status()
        print(f"t+{(i+1)*10}s msgs={st['msgs']} closes={st['closes_processed']} setups={st['setups_fired']}")
    
    feed.stop()
