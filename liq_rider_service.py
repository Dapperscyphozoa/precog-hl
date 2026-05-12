"""
liq_rider_service.py — Liquidation Cascade Rider, OBSERVE-ONLY Phase 1.

Subscribes to Binance forceOrder feed via the existing liquidation_ws module,
detects cascade events on HL-listed coins, and logs every cascade as a
"would-have-fired" signal. No HL orders placed. No state mutations on the
trading wallet. Pure detection + measurement.

Phase 1 goal: prove the signal. For every detected cascade, snapshot:
  - coin, side (long_liq vs short_liq), cumulative USD, timestamp
  - HL mid price at detection (T0)
  - HL mid prices at T+30s, T+2min, T+5min, T+15min
  - "Fade win" = T+5min mid is in fade direction from T0 by >0.3%

After ~24-48h of cascades logged, we'll have a per-coin and per-side
hit-rate table. THEN we wire Phase 2 (actual trade execution).

Architecture:
  - Main thread: starts liquidation_ws, runs detector loop
  - Detector loop: every 5s scans all tracked coins for fresh cascades,
    deduplicates (one cascade per coin per 5min window), enqueues for
    follow-up
  - Follow-up loop: every 15s walks pending cascades, fetches HL mid,
    fills in T+30s/T+2m/T+5m/T+15m mid columns, marks complete when all
    timepoints captured
  - Dashboard push: every 60s posts engine_name='liq-rider' with
    cumulative cascade stats

Tunables (env):
  LIQ_RIDER_LIVE                   default 0 (observe only)
  LIQ_RIDER_LOG_PATH               default /var/data/liq_rider_log.jsonl
  LIQ_RIDER_DEDUP_WINDOW_SEC       default 300 (one cascade per coin per 5min)
  LIQ_CASCADE_USD_THRESHOLD        inherited from liquidation_ws (default 150k)

Phase 2 (future):
  - Set LIQ_RIDER_LIVE=1
  - On cascade detect: place IOC entry in fade direction
  - TP at +0.5% / SL at -0.4% (verified by Phase 1 data; placeholders for now)
  - Time stop at 5min hold
"""
import json
import os
import sys
import time
import threading
import urllib.request
from collections import defaultdict

import liquidation_ws


# ─── config ────────────────────────────────────────────────────────────
LIVE = os.environ.get('LIQ_RIDER_LIVE', '0') == '1'
LOG_PATH = os.environ.get('LIQ_RIDER_LOG_PATH', '/var/data/liq_rider_log.jsonl')
DEDUP_WINDOW_SEC = int(os.environ.get('LIQ_RIDER_DEDUP_WINDOW_SEC', '300'))
DASH_URL = os.environ.get('DASH_URL', '').rstrip('/')
DASH_PUSH_SECRET = os.environ.get('DASH_PUSH_SECRET', '')
ENGINE_NAME = 'liq-rider'
HL_INFO_URL = 'https://api.hyperliquid.xyz/info'

DETECT_INTERVAL_SEC = 5
FOLLOWUP_INTERVAL_SEC = 15
DASH_PUSH_INTERVAL_SEC = 60

# Mid-price snapshot offsets after a cascade fires
SNAPSHOT_OFFSETS_SEC = [30, 120, 300, 900]  # T+30s, T+2m, T+5m, T+15m

FADE_WIN_THRESHOLD_PCT = 0.3  # 0.3% in fade direction at T+5min = "win"


# ─── state ─────────────────────────────────────────────────────────────
_state = {
    'started_ts': time.time(),
    'cascades_detected': 0,
    'cascades_pending_followup': 0,
    'cascades_complete': 0,
    'wins_5min': 0,
    'losses_5min': 0,
    'last_cascade_coin': None,
    'last_cascade_ts': 0,
    'per_coin': defaultdict(lambda: {'detected': 0, 'wins_5min': 0, 'losses_5min': 0, 'pnl_pct_sum': 0.0}),
}
_state_lock = threading.Lock()

# Dedup: last cascade ts per (coin, side)
_last_dedup_ts = {}  # (coin, side) -> ts

# Pending follow-ups
# Each: {coin, side, fade_direction, cascade_ts, total_usd, t0_mid,
#        snapshots: {30: {ts, mid, pnl_pct, ...}, 120: {...}, 300: {...}, 900: {...}},
#        complete: bool}
_pending = []
_pending_lock = threading.Lock()


def _log(msg):
    print(f"[liq-rider] {msg}", flush=True)


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


def _hl_mid(coin):
    """Fetch current HL mid for a coin via /info allMids. Returns float or None."""
    try:
        body = json.dumps({'type': 'allMids'}).encode()
        req = urllib.request.Request(HL_INFO_URL, data=body,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read())
        mid_str = data.get(coin)
        return float(mid_str) if mid_str else None
    except Exception as e:
        _log(f"allMids err for {coin}: {e}")
        return None


# ─── detector ──────────────────────────────────────────────────────────
def detect_loop():
    """Every DETECT_INTERVAL_SEC, scan all tracked coins for fresh cascades."""
    _log(f"detector loop started (interval={DETECT_INTERVAL_SEC}s)")
    while True:
        try:
            # liquidation_ws._LIQS keys = all coins seen so far
            try:
                coins = list(liquidation_ws._LIQS.keys())
            except Exception:
                coins = []

            for coin in coins:
                casc = liquidation_ws.get_cascade(coin, max_age_sec=30)
                if not casc:
                    continue
                key = (coin, casc['side'])
                now = time.time()
                last = _last_dedup_ts.get(key, 0)
                if now - last < DEDUP_WINDOW_SEC:
                    continue
                _last_dedup_ts[key] = now

                # New cascade — snapshot T0 mid + register for follow-up
                t0_mid = _hl_mid(coin)
                if not t0_mid:
                    _log(f"cascade {coin} {casc['side']} ${casc['total_usd']:.0f} but no HL mid → skip")
                    continue

                cascade_event = {
                    'event': 'cascade_detected',
                    'coin': coin,
                    'side': casc['side'],
                    'fade_direction': casc['fade_direction'],
                    'total_usd': casc['total_usd'],
                    'cascade_ts': casc['ts'],
                    'detect_ts': now,
                    't0_mid': t0_mid,
                    'snapshots': {str(off): None for off in SNAPSHOT_OFFSETS_SEC},
                    'complete': False,
                }
                with _pending_lock:
                    _pending.append(cascade_event)
                with _state_lock:
                    _state['cascades_detected'] += 1
                    _state['cascades_pending_followup'] = len(_pending)
                    _state['last_cascade_coin'] = coin
                    _state['last_cascade_ts'] = now
                    _state['per_coin'][coin]['detected'] += 1

                _log(f"CASCADE {coin} {casc['side']} ${casc['total_usd']:.0f} fade={casc['fade_direction']} t0_mid={t0_mid}")
                _append_jsonl(LOG_PATH, cascade_event)
        except Exception as e:
            _log(f"detect_loop err: {e}")
        time.sleep(DETECT_INTERVAL_SEC)


# ─── follow-up ─────────────────────────────────────────────────────────
def followup_loop():
    """Every FOLLOWUP_INTERVAL_SEC, walk pending cascades and capture mid
    snapshots at the configured offsets."""
    _log(f"followup loop started (interval={FOLLOWUP_INTERVAL_SEC}s)")
    while True:
        try:
            now = time.time()
            with _pending_lock:
                still_pending = []
                for ev in _pending:
                    age = now - ev['detect_ts']
                    captured_any = False
                    for off in SNAPSHOT_OFFSETS_SEC:
                        k = str(off)
                        if ev['snapshots'][k] is not None:
                            continue
                        # Capture once age >= offset (with small grace)
                        if age >= off - FOLLOWUP_INTERVAL_SEC / 2:
                            mid = _hl_mid(ev['coin'])
                            if mid is None:
                                continue
                            # PnL in fade direction
                            t0 = ev['t0_mid']
                            if ev['fade_direction'] == 'BUY':
                                # We faded by buying. Win if mid > t0.
                                pnl_pct = (mid - t0) / t0 * 100
                            else:
                                pnl_pct = (t0 - mid) / t0 * 100
                            ev['snapshots'][k] = {
                                'ts': now,
                                'mid': mid,
                                'pnl_pct': round(pnl_pct, 4),
                            }
                            captured_any = True
                            _log(f"  followup {ev['coin']} T+{off}s mid={mid} pnl={pnl_pct:+.3f}%")
                            _append_jsonl(LOG_PATH, {
                                'event': 'cascade_snapshot',
                                'coin': ev['coin'],
                                'side': ev['side'],
                                'cascade_ts': ev['cascade_ts'],
                                'offset_sec': off,
                                'mid': mid,
                                'pnl_pct': round(pnl_pct, 4),
                            })

                    # Complete?
                    all_captured = all(ev['snapshots'][str(o)] is not None for o in SNAPSHOT_OFFSETS_SEC)
                    if all_captured and not ev['complete']:
                        ev['complete'] = True
                        # Score using T+5min (300s)
                        s5 = ev['snapshots']['300']
                        if s5:
                            pnl5 = s5['pnl_pct']
                            with _state_lock:
                                if pnl5 >= FADE_WIN_THRESHOLD_PCT:
                                    _state['wins_5min'] += 1
                                    _state['per_coin'][ev['coin']]['wins_5min'] += 1
                                else:
                                    _state['losses_5min'] += 1
                                    _state['per_coin'][ev['coin']]['losses_5min'] += 1
                                _state['per_coin'][ev['coin']]['pnl_pct_sum'] += pnl5
                                _state['cascades_complete'] += 1
                            _append_jsonl(LOG_PATH, {
                                'event': 'cascade_complete',
                                'coin': ev['coin'],
                                'side': ev['side'],
                                'fade_direction': ev['fade_direction'],
                                'total_usd': ev['total_usd'],
                                'cascade_ts': ev['cascade_ts'],
                                't0_mid': ev['t0_mid'],
                                'pnl_5min_pct': pnl5,
                                'win_5min': pnl5 >= FADE_WIN_THRESHOLD_PCT,
                                'snapshots': ev['snapshots'],
                            })
                            _log(f"COMPLETE {ev['coin']} {ev['side']} T+5min={pnl5:+.3f}% "
                                 f"{'WIN' if pnl5 >= FADE_WIN_THRESHOLD_PCT else 'loss'}")

                    # Keep pending unless cascade is older than max offset + grace
                    if age < max(SNAPSHOT_OFFSETS_SEC) + 60:
                        still_pending.append(ev)
                    elif not ev['complete']:
                        # Aged out without capturing all snapshots — log partial
                        _append_jsonl(LOG_PATH, {
                            'event': 'cascade_partial',
                            'coin': ev['coin'],
                            'side': ev['side'],
                            'cascade_ts': ev['cascade_ts'],
                            'snapshots': ev['snapshots'],
                        })

                _pending.clear()
                _pending.extend(still_pending)
                with _state_lock:
                    _state['cascades_pending_followup'] = len(_pending)
        except Exception as e:
            _log(f"followup_loop err: {e}")
        time.sleep(FOLLOWUP_INTERVAL_SEC)


# ─── dashboard push ────────────────────────────────────────────────────
def dash_push_loop():
    """Post engine state to the dashboard every DASH_PUSH_INTERVAL_SEC.
    Reuses the canonical dashboard_push.push_state() so we hit the
    correct endpoint and auth header."""
    try:
        from dashboard_push import push_state
    except Exception as e:
        _log(f"dashboard_push import err: {e} — push disabled")
        return
    _log(f"dash push loop started → {DASH_URL or '(no DASH_URL)'} (interval={DASH_PUSH_INTERVAL_SEC}s)")
    while True:
        try:
            with _state_lock:
                snap = dict(_state)
                per_coin = {c: dict(v) for c, v in _state['per_coin'].items()}
            wins = snap['wins_5min']
            losses = snap['losses_5min']
            total = wins + losses
            wr = (wins / total * 100) if total else 0.0
            # Build a synthetic "history" entry per completed cascade so the
            # dashboard's history_12h column shows recent activity. For Phase 1
            # we don't have real positions, just observation outcomes.
            try:
                push_state(
                    engine_name=ENGINE_NAME,
                    live=LIVE,
                    sizing_mode='observe',
                    notional_usd=0,
                    max_concurrent=0,
                    positions_dict={},
                    history_list=[],  # no real fills in observe mode
                    scan_count=snap['cascades_detected'],
                    last_scan_ts=int(snap['last_cascade_ts'] * 1000) if snap['last_cascade_ts'] else 0,
                    extra_telemetry={
                        'mode': 'observe_only' if not LIVE else 'live',
                        'cascades_detected': snap['cascades_detected'],
                        'cascades_complete': snap['cascades_complete'],
                        'cascades_pending': snap['cascades_pending_followup'],
                        'wins_5min': wins,
                        'losses_5min': losses,
                        'wr_5min_pct': round(wr, 1),
                        'last_cascade_coin': snap.get('last_cascade_coin'),
                        'last_cascade_ts_ms': int(snap['last_cascade_ts'] * 1000) if snap['last_cascade_ts'] else 0,
                        'per_coin': per_coin,
                    },
                )
            except Exception as e:
                _log(f"push_state err: {e}")
        except Exception as e:
            _log(f"dash_push_loop outer err: {e}")
        time.sleep(DASH_PUSH_INTERVAL_SEC)


# ─── stats log ─────────────────────────────────────────────────────────
def stats_loop():
    """Every 5 min, log a stats summary to stdout."""
    _log("stats loop started (every 5 min)")
    while True:
        time.sleep(300)
        try:
            with _state_lock:
                snap = dict(_state)
            wins = snap['wins_5min']
            losses = snap['losses_5min']
            total = wins + losses
            wr = (wins / total * 100) if total else 0.0
            ws_status = liquidation_ws.status()
            _log(f"STATS uptime={int(time.time() - snap['started_ts'])}s "
                 f"detected={snap['cascades_detected']} complete={snap['cascades_complete']} "
                 f"pending={snap['cascades_pending_followup']} "
                 f"wins/losses={wins}/{losses} WR={wr:.1f}% "
                 f"ws_tracked={ws_status['tracked_coins']} ws_total={ws_status['total_liqs_cached']}")
        except Exception as e:
            _log(f"stats_loop err: {e}")


# ─── main ──────────────────────────────────────────────────────────────
def main():
    _log(f"liq-rider starting · LIVE={LIVE} · log={LOG_PATH}")
    if LIVE:
        _log("WARNING: LIVE=1 but Phase 1 doesn't actually trade. Set LIQ_RIDER_LIVE_TRADE=1 in Phase 2.")

    # 1. Start liquidation_ws (the WS subscriber)
    try:
        liquidation_ws.start()
    except Exception as e:
        _log(f"liquidation_ws.start err: {e}")
        sys.exit(1)
    _log("liquidation_ws started")
    time.sleep(3)  # let WS connect

    # 2. Start daemon loops
    threading.Thread(target=detect_loop, daemon=True, name='detect').start()
    threading.Thread(target=followup_loop, daemon=True, name='followup').start()
    threading.Thread(target=dash_push_loop, daemon=True, name='dash_push').start()
    threading.Thread(target=stats_loop, daemon=True, name='stats').start()

    # 3. Idle main thread (keeps process alive)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        _log("shutdown requested")
        sys.exit(0)


if __name__ == '__main__':
    main()
