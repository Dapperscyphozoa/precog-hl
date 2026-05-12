"""
stat_arb_service.py — Stat-arb pairs (ETH/BTC, SOL/BTC, SOL/ETH),
OBSERVE-ONLY Phase 1.

Tracks the price ratio of correlated coin pairs. Maintains a rolling
window of ratio values, computes z-score on every poll. When |z| >
Z_THRESH, emits a signal:
  z > +Z_THRESH  → ratio is too high  → expect mean revert DOWN
                   → short A, long B  → pair_side = SHORT_RATIO
  z < -Z_THRESH  → ratio is too low   → expect mean revert UP
                   → long A,  short B → pair_side = LONG_RATIO

Pair PnL = (ratio_t - ratio_0) / ratio_0 × side_sign, where
  side_sign = -1 if SHORT_RATIO else +1
i.e. profit when ratio moves toward the mean.

Scoring at T+1h / T+4h / T+24h.
Dedup: 1 signal per pair per 4h.

PHASE 1 = no trades. Just log + score. After ~7d we'll know whether
each pair actually mean-reverts and at what threshold.

Tunables (env):
  STATARB_PAIRS         "ETH:BTC,SOL:BTC,SOL:ETH"   pairs to track
  STATARB_Z_THRESH      2.0
  STATARB_WINDOW_SIZE   720    (12h at 1m sampling)
  STATARB_MIN_WINDOW    60     (need 1h of data before scoring)
  STATARB_POLL_SEC      60
  STATARB_DEDUP_SEC     14400  (4h)
  STATARB_LOG_PATH      /var/data/stat_arb_log.jsonl
"""
import json
import os
import sys
import time
import threading
import urllib.request
import math
from collections import defaultdict, deque

# ─── config ────────────────────────────────────────────────────────────
PAIRS_STR     = os.environ.get('STATARB_PAIRS', 'ETH:BTC,SOL:BTC,SOL:ETH')
PAIRS         = [tuple(p.split(':')) for p in PAIRS_STR.split(',') if ':' in p]
Z_THRESH      = float(os.environ.get('STATARB_Z_THRESH', '2.0'))
WINDOW_SIZE   = int(os.environ.get('STATARB_WINDOW_SIZE', '720'))
MIN_WINDOW    = int(os.environ.get('STATARB_MIN_WINDOW', '60'))
POLL_SEC      = int(os.environ.get('STATARB_POLL_SEC', '60'))
DEDUP_SEC     = int(os.environ.get('STATARB_DEDUP_SEC', '14400'))
LOG_PATH      = os.environ.get('STATARB_LOG_PATH', '/var/data/stat_arb_log.jsonl')

HL_INFO_URL = 'https://api.hyperliquid.xyz/info'
ENGINE_NAME = 'stat-arb'

SNAPSHOT_OFFSETS_SEC = [3600, 14400, 86400]  # T+1h, T+4h, T+24h
SCORE_INTERVAL_SEC = 60
DASH_PUSH_INTERVAL_SEC = 60

# ─── state ─────────────────────────────────────────────────────────────
_ratios = {pair: deque(maxlen=WINDOW_SIZE) for pair in PAIRS}
_state = {
    'started_ts': time.time(),
    'poll_count': 0,
    'poll_errors': 0,
    'signals_detected': 0,
    'signals_complete': 0,
    'wins_4h': 0, 'losses_4h': 0,
    'wins_24h': 0, 'losses_24h': 0,
    'last_signal_pair': None,
    'last_signal_ts': 0,
    'per_pair': defaultdict(lambda: {'samples': 0, 'signals': 0, 'wins_4h': 0,
                                       'losses_4h': 0, 'pnl_pct_sum_4h': 0.0,
                                       'last_z': None}),
}
_state_lock = threading.Lock()

# Dedup: pair_key → last_ts
_last_signal = {}

# Pending follow-ups
_pending = []
_pending_lock = threading.Lock()


def _log(msg):
    print(f"[stat-arb] {msg}", flush=True)


def _append_jsonl(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    try:
        with open(path, 'a') as f:
            f.write(json.dumps(obj) + '\n')
    except Exception as e:
        _log(f"jsonl write err: {e}")


def _http_post(url, body, timeout=8):
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _all_mids():
    r = _http_post(HL_INFO_URL, {'type': 'allMids'}, timeout=8)
    if not r or not isinstance(r, dict):
        return None
    return r


def _mean_stdev(values):
    n = len(values)
    if n < 2:
        return 0.0, 0.0
    m = sum(values) / n
    v = sum((x - m) ** 2 for x in values) / n
    return m, math.sqrt(v)


# ─── detector ──────────────────────────────────────────────────────────
def detect_loop():
    _log(f"detect loop · pairs={PAIRS} · z_thresh={Z_THRESH} · window={WINDOW_SIZE} (min {MIN_WINDOW})")
    while True:
        try:
            mids = _all_mids()
            with _state_lock:
                _state['poll_count'] += 1
            if not mids:
                with _state_lock:
                    _state['poll_errors'] += 1
                time.sleep(POLL_SEC)
                continue
            now = time.time()
            for a, b in PAIRS:
                try:
                    pa = float(mids.get(a, 0) or 0)
                    pb = float(mids.get(b, 0) or 0)
                except (TypeError, ValueError):
                    continue
                if pa <= 0 or pb <= 0:
                    continue
                ratio = pa / pb
                _ratios[(a, b)].append(ratio)
                pair_key = f'{a}/{b}'
                with _state_lock:
                    _state['per_pair'][pair_key]['samples'] += 1

                # Need enough history before evaluating
                hist = list(_ratios[(a, b)])
                if len(hist) < MIN_WINDOW:
                    continue
                mean, stdev = _mean_stdev(hist)
                if stdev <= 0:
                    continue
                z = (ratio - mean) / stdev
                with _state_lock:
                    _state['per_pair'][pair_key]['last_z'] = round(z, 3)

                if abs(z) < Z_THRESH:
                    continue
                pair_side = 'SHORT_RATIO' if z > 0 else 'LONG_RATIO'
                last_ts = _last_signal.get(pair_key, 0)
                if now - last_ts < DEDUP_SEC:
                    continue
                _last_signal[pair_key] = now

                sig = {
                    'event': 'stat_arb_signal',
                    'pair': pair_key,
                    'coin_a': a, 'coin_b': b,
                    'mid_a_t0': pa, 'mid_b_t0': pb,
                    'ratio_t0': ratio,
                    'mean': round(mean, 8),
                    'stdev': round(stdev, 8),
                    'z': round(z, 3),
                    'pair_side': pair_side,
                    'side_sign': -1 if pair_side == 'SHORT_RATIO' else 1,
                    'window_size': len(hist),
                    'detect_ts': now,
                    'snapshots': {str(o): None for o in SNAPSHOT_OFFSETS_SEC},
                    'complete': False,
                }
                with _pending_lock:
                    _pending.append(sig)
                with _state_lock:
                    _state['signals_detected'] += 1
                    _state['last_signal_pair'] = pair_key
                    _state['last_signal_ts'] = now
                    _state['per_pair'][pair_key]['signals'] += 1
                _log(f"SIGNAL {pair_key} {pair_side} z={z:+.2f} ratio={ratio:.6f} "
                     f"mean={mean:.6f} (mids a={pa} b={pb})")
                _append_jsonl(LOG_PATH, sig)
        except Exception as e:
            _log(f"detect_loop err: {e}")
        time.sleep(POLL_SEC)


# ─── scoring ───────────────────────────────────────────────────────────
def score_loop():
    _log(f"score loop · offsets={SNAPSHOT_OFFSETS_SEC}")
    while True:
        try:
            now = time.time()
            mids = None
            with _pending_lock:
                still_pending = []
                for ev in _pending:
                    age = now - ev['detect_ts']
                    needs_mid = any(ev['snapshots'].get(str(o)) is None
                                    and age >= o - SCORE_INTERVAL_SEC/2
                                    for o in SNAPSHOT_OFFSETS_SEC)
                    if needs_mid and mids is None:
                        mids = _all_mids()
                    for off in SNAPSHOT_OFFSETS_SEC:
                        k = str(off)
                        if ev['snapshots'].get(k) is not None:
                            continue
                        if age >= off - SCORE_INTERVAL_SEC / 2:
                            if not mids:
                                continue
                            a, b = ev['coin_a'], ev['coin_b']
                            try:
                                pa = float(mids.get(a, 0) or 0)
                                pb = float(mids.get(b, 0) or 0)
                            except (TypeError, ValueError):
                                continue
                            if pa <= 0 or pb <= 0:
                                continue
                            ratio_now = pa / pb
                            r0 = ev['ratio_t0']
                            pnl_pct = (ratio_now - r0) / r0 * 100 * ev['side_sign']
                            ev['snapshots'][k] = {
                                'mid_a': pa, 'mid_b': pb,
                                'ratio': ratio_now, 'pnl_pct': round(pnl_pct, 4),
                            }
                            _append_jsonl(LOG_PATH, {
                                'event': 'signal_snapshot',
                                'pair': ev['pair'], 'detect_ts': ev['detect_ts'],
                                'offset_sec': off, 'ratio': ratio_now,
                                'pnl_pct': round(pnl_pct, 4),
                            })

                    all_captured = all(ev['snapshots'].get(str(o)) is not None
                                        for o in SNAPSHOT_OFFSETS_SEC)
                    if all_captured and not ev['complete']:
                        ev['complete'] = True
                        pnl_4h = ev['snapshots']['14400']['pnl_pct']
                        pnl_24h = ev['snapshots']['86400']['pnl_pct']
                        with _state_lock:
                            if pnl_4h > 0: _state['wins_4h'] += 1
                            elif pnl_4h < 0: _state['losses_4h'] += 1
                            if pnl_24h > 0: _state['wins_24h'] += 1
                            elif pnl_24h < 0: _state['losses_24h'] += 1
                            _state['signals_complete'] += 1
                            pp = _state['per_pair'][ev['pair']]
                            if pnl_4h > 0: pp['wins_4h'] += 1
                            elif pnl_4h < 0: pp['losses_4h'] += 1
                            pp['pnl_pct_sum_4h'] += pnl_4h
                        _append_jsonl(LOG_PATH, {
                            'event': 'signal_complete',
                            'pair': ev['pair'], 'pair_side': ev['pair_side'],
                            'z': ev['z'], 'ratio_t0': ev['ratio_t0'],
                            'detect_ts': ev['detect_ts'],
                            'pnl_1h': ev['snapshots']['3600']['pnl_pct'],
                            'pnl_4h': pnl_4h, 'pnl_24h': pnl_24h,
                        })
                    if age < max(SNAPSHOT_OFFSETS_SEC) + 60:
                        still_pending.append(ev)
                _pending.clear()
                _pending.extend(still_pending)
        except Exception as e:
            _log(f"score_loop err: {e}")
        time.sleep(SCORE_INTERVAL_SEC)


# ─── dashboard ─────────────────────────────────────────────────────────
def dash_push_loop():
    try:
        from dashboard_push import push_state
    except Exception as e:
        _log(f"dashboard_push import err: {e}")
        return
    _log("dash push loop started")
    while True:
        try:
            with _state_lock:
                snap = dict(_state)
                per_pair = {p: dict(v) for p, v in _state['per_pair'].items()}
            w4 = snap['wins_4h']; l4 = snap['losses_4h']
            w24 = snap['wins_24h']; l24 = snap['losses_24h']
            wr4 = (w4/(w4+l4)*100) if (w4+l4) else 0
            wr24 = (w24/(w24+l24)*100) if (w24+l24) else 0
            try:
                push_state(
                    engine_name=ENGINE_NAME,
                    live=False,
                    sizing_mode='observe',
                    notional_usd=0,
                    max_concurrent=0,
                    positions_dict={},
                    history_list=[],
                    scan_count=snap['signals_detected'],
                    last_scan_ts=int(snap['last_signal_ts'] * 1000) if snap['last_signal_ts'] else 0,
                    extra_telemetry={
                        'mode': 'observe_only',
                        'pairs': [f'{a}/{b}' for a, b in PAIRS],
                        'z_thresh': Z_THRESH,
                        'window_size': WINDOW_SIZE,
                        'poll_count': snap['poll_count'],
                        'poll_errors': snap['poll_errors'],
                        'signals_detected': snap['signals_detected'],
                        'signals_complete': snap['signals_complete'],
                        'wins_4h': w4, 'losses_4h': l4, 'wr_4h_pct': round(wr4, 1),
                        'wins_24h': w24, 'losses_24h': l24, 'wr_24h_pct': round(wr24, 1),
                        'last_signal_pair': snap.get('last_signal_pair'),
                        'last_signal_ts_ms': int(snap['last_signal_ts'] * 1000) if snap['last_signal_ts'] else 0,
                        'per_pair': per_pair,
                    },
                )
            except Exception as e:
                _log(f"push_state err: {e}")
        except Exception as e:
            _log(f"dash outer err: {e}")
        time.sleep(DASH_PUSH_INTERVAL_SEC)


def stats_loop():
    while True:
        time.sleep(300)
        try:
            with _state_lock:
                snap = dict(_state)
            uptime = int(time.time() - snap['started_ts'])
            w4 = snap['wins_4h']; l4 = snap['losses_4h']
            wr4 = (w4/(w4+l4)*100) if (w4+l4) else 0
            zsamp = ', '.join(f"{p}:z={v['last_z']}" for p, v in snap['per_pair'].items() if v.get('last_z') is not None)
            _log(f"STATS uptime={uptime}s polls={snap['poll_count']}/{snap['poll_errors']}err "
                 f"signals={snap['signals_detected']}/{snap['signals_complete']}cpl "
                 f"4h:{w4}/{l4} WR={wr4:.1f}%  current_z=[{zsamp}]")
        except Exception as e:
            _log(f"stats err: {e}")


def main():
    _log(f"stat-arb starting · OBSERVE_ONLY · pairs={PAIRS} · z_thresh={Z_THRESH} · log={LOG_PATH}")
    if not PAIRS:
        _log("FATAL: no pairs configured")
        sys.exit(1)
    threading.Thread(target=detect_loop, daemon=True, name='detect').start()
    threading.Thread(target=score_loop, daemon=True, name='score').start()
    threading.Thread(target=dash_push_loop, daemon=True, name='dash').start()
    threading.Thread(target=stats_loop, daemon=True, name='stats').start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == '__main__':
    main()
