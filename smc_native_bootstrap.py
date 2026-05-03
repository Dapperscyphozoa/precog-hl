"""
smc_native_bootstrap.py — Backfill candle history into SMCDetectors at startup.

Calls HL info.candles_snapshot to fetch ~50 closed 15m bars per coin,
then feeds them through each detector via on_close so internal state
(pivots, OBs, FVGs) is warm before live WS takes over.

Note: HL info endpoint is REST. 217 coins × snapshot calls would burst
CloudFront. We throttle: 5 coins per second.
"""
import time
import logging
from hyperliquid.info import Info
from hyperliquid.utils import constants

log = logging.getLogger(__name__)


def candles_snapshot(info, coin, interval='15m', n_bars=50):
    """Fetch `n_bars` recent 15m candles for `coin`. Returns list of dicts."""
    end_ms = int(time.time() * 1000)
    interval_ms = {'1m': 60_000, '15m': 900_000, '1h': 3_600_000, '4h': 14_400_000}[interval]
    start_ms = end_ms - interval_ms * (n_bars + 5)
    
    try:
        raw = info.candles_snapshot(coin, interval, start_ms, end_ms)
    except Exception as e:
        log.warning(f"candles_snapshot {coin} failed: {e}")
        return []
    
    out = []
    for c in (raw or []):
        try:
            out.append({
                't': int(c['t']),
                'o': float(c['o']),
                'h': float(c['h']),
                'l': float(c['l']),
                'c': float(c['c']),
                'v': float(c['v']),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda x: x['t'])


def warmup_detectors(detectors, info=None, interval='15m', n_bars=50,
                     throttle_per_sec=5, on_log=None):
    """Backfill all detectors. detectors: {coin: SMCDetector}.
    Returns dict {coin: bars_loaded}.
    """
    log_fn = on_log or log.info
    if info is None:
        info = Info(constants.MAINNET_API_URL, skip_ws=True)
    
    interval_sec = 1.0 / throttle_per_sec
    out = {}
    
    for i, (coin, detector) in enumerate(detectors.items()):
        bars = candles_snapshot(info, coin, interval, n_bars)
        if bars:
            # Drop the most recent bar (likely in-progress, not closed)
            now_ms = int(time.time() * 1000)
            interval_ms = {'1m': 60_000, '15m': 900_000, '1h': 3_600_000}[interval]
            closed_bars = [b for b in bars if (b['t'] + interval_ms) < now_ms]
            for b in closed_bars:
                detector.on_close(b)
            out[coin] = len(closed_bars)
        else:
            out[coin] = 0
        
        if (i + 1) % 25 == 0:
            log_fn(f"warmup: {i+1}/{len(detectors)} coins backfilled")
        
        time.sleep(interval_sec)
    
    log_fn(f"warmup complete: {sum(out.values())} bars across {len(out)} coins")
    return out


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    from smc_native_engine import SMCDetector
    
    coins = ['JUP', 'JTO', 'SOL', 'INJ']
    detectors = {c: SMCDetector(c) for c in coins}
    result = warmup_detectors(detectors, on_log=print)
    print(f"Warmup result: {result}")
    
    # Inspect detector state
    for c, d in detectors.items():
        print(f"{c}: state={d.state} bars={d.bar_idx} pivots(h/l)={len(d.swing_highs)}/{len(d.swing_lows)} obs={len(d.obs)} fvgs={len(d.fvgs)}")
