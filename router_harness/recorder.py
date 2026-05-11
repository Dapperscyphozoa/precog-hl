"""Append-only JSONL recorder for wall snapshots + signal attempts.

Mounted at /var/data/router_harness/ (Render disk persistence).
Files rotate daily by date suffix. Both streams thread-safe via per-file lock.

Usage from precog.py:
    from router_harness.recorder import record_wall_snapshot, record_signal_attempt

This is the data infrastructure for the Day-1 council mandate.
"""
import json
import os
import threading
import time
from typing import Optional


_BASE = os.environ.get('ROUTER_HARNESS_DIR', '/var/data/router_harness')
_WALL_LOCK = threading.Lock()
_SIG_LOCK = threading.Lock()
os.makedirs(_BASE, exist_ok=True)


def _wall_file_today() -> str:
    return os.path.join(_BASE, f'walls_{time.strftime("%Y%m%d")}.jsonl')


def _sig_file_today() -> str:
    return os.path.join(_BASE, f'signals_{time.strftime("%Y%m%d")}.jsonl')


def record_wall_snapshot(coin: str, mid: float, walls: list) -> None:
    """Persist one wall snapshot. Walls is list of dicts with side/price/usd/distance_pct/etc."""
    rec = {
        'ts': time.time(),
        'coin': coin,
        'mid': mid,
        'walls': walls,
    }
    line = json.dumps(rec, separators=(',', ':')) + '\n'
    with _WALL_LOCK:
        try:
            with open(_wall_file_today(), 'a') as f:
                f.write(line)
        except Exception as e:
            print(f'[recorder] wall write err: {e}', flush=True)


def record_signal_attempt(coin: str, side: str, engine: str, entry_px: float,
                          sl_px: Optional[float] = None, tp_px: Optional[float] = None,
                          intended_size_usd: Optional[float] = None,
                          blocked_by: Optional[str] = None,
                          block_reason: Optional[str] = None) -> None:
    """Persist one signal attempt. Captures EVERY signal the engine layer proposes,
    including those blocked by guards. The router replay needs these unfiltered."""
    rec = {
        'ts': time.time(),
        'coin': coin,
        'side': side,
        'engine': engine,
        'entry_px': entry_px,
        'sl_px': sl_px,
        'tp_px': tp_px,
        'intended_size_usd': intended_size_usd,
        'blocked_by': blocked_by,
        'block_reason': block_reason,
    }
    line = json.dumps(rec, separators=(',', ':')) + '\n'
    with _SIG_LOCK:
        try:
            with open(_sig_file_today(), 'a') as f:
                f.write(line)
        except Exception as e:
            print(f'[recorder] sig write err: {e}', flush=True)


def stats() -> dict:
    """Quick stats for /health introspection."""
    try:
        wall_files = sorted([f for f in os.listdir(_BASE) if f.startswith('walls_')])
        sig_files = sorted([f for f in os.listdir(_BASE) if f.startswith('signals_')])
        wall_lines = sum(sum(1 for _ in open(os.path.join(_BASE, f))) for f in wall_files) if wall_files else 0
        sig_lines = sum(sum(1 for _ in open(os.path.join(_BASE, f))) for f in sig_files) if sig_files else 0
        return {
            'base_dir': _BASE,
            'wall_files': len(wall_files),
            'wall_lines': wall_lines,
            'sig_files': len(sig_files),
            'sig_lines': sig_lines,
        }
    except Exception as e:
        return {'err': str(e)}
