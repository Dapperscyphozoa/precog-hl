"""
funding_fade_service.py — Funding Spike Fade, OBSERVE-ONLY Phase 1.

When perp funding rate spikes hard, the cohort is too crowded. Funding
itself becomes exit pressure. Fade the consensus.

HL settles funding every 1h (not 8h). Per-hour funding rate maps to
annualized via ×24×365. Spike threshold defaults to 0.005%/hr (44%
annualized) with OI filter to skip ghost markets.

Mechanics:
  - Negative funding (longs pay shorts? no — HL convention: positive
    funding = longs pay, negative = shorts pay) →
    Convention check via HL docs: positive funding means longs pay
    shorts (longs are crowded), so positive spike → fade SHORT.
    Negative spike → shorts crowded → fade LONG.
  - Entry: log T0 mid.
  - Scoring at T+1h, T+4h, T+8h, T+24h: pnl_pct in fade direction.

Dedup: one signal per (coin, side) per 4h. After funding settles, a
coin can re-fire if extreme persists.

Phase 1 = no trades. Just detect + log + score. After ~7d we'll have
per-coin and per-magnitude hit rates → Phase 2 wires real entries.

Tunables (env):
  FUND_FADE_THRESH_PCT_HR     default 0.005   (per-hour funding %)
  FUND_FADE_MIN_OI_USD        default 100000
  FUND_FADE_POLL_SEC          default 60
  FUND_FADE_DEDUP_SEC         default 14400   (4h)
  FUND_FADE_LOG_PATH          default /var/data/funding_fade_log.jsonl
"""
import json
import os
import sys
import time
import threading
import urllib.request
from collections import defaultdict

# ─── config ────────────────────────────────────────────────────────────
THRESH_PCT_HR  = float(os.environ.get('FUND_FADE_THRESH_PCT_HR', '0.005')) / 100.0  # 0.005% → 0.00005
MIN_OI_USD     = float(os.environ.get('FUND_FADE_MIN_OI_USD', '100000'))
POLL_SEC       = int(os.environ.get('FUND_FADE_POLL_SEC', '60'))
DEDUP_SEC      = int(os.environ.get('FUND_FADE_DEDUP_SEC', '14400'))
LOG_PATH       = os.environ.get('FUND_FADE_LOG_PATH', '/var/data/funding_fade_log.jsonl')

HL_INFO_URL = 'https://api.hyperliquid.xyz/info'
ENGINE_NAME = 'funding-fade'

SNAPSHOT_OFFSETS_SEC = [3600, 14400, 28800, 86400]  # T+1h, T+4h, T+8h, T+24h
SCORE_INTERVAL_SEC = 60
DASH_PUSH_INTERVAL_SEC = 60

# ─── state ─────────────────────────────────────────────────────────────
_state = {
    'started_ts': time.time(),
    'poll_count': 0,
    'poll_errors': 0,
    'signals_detected': 0,
    'signals_complete': 0,
    'wins_4h': 0, 'losses_4h': 0,
    'wins_24h': 0, 'losses_24h': 0,
    'last_signal_coin': None,
    'last_signal_ts': 0,
    'per_coin': defaultdict(lambda: {'signals': 0, 'wins_4h': 0, 'losses_4h': 0, 'pnl_pct_sum_4h': 0.0}),
}
_state_lock = threading.Lock()

# Dedup: (coin, side) → last_signal_ts
_last_signal = {}

# Pending follow-ups
_pending = []
_pending_lock = threading.Lock()


def _log(msg):
    print(f"[funding-fade] {msg}", flush=True)


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


def _http_post(url, body, timeout=10):
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return None


def _hl_mid(coin):
    r = _http_post(HL_INFO_URL, {'type': 'allMids'}, timeout=8)
    if not r or not isinstance(r, dict):
        return None
    try:
        return float(r[coin]) if r.get(coin) else None
    except (TypeError, ValueError):
        return None


# ─── detector ──────────────────────────────────────────────────────────
def fetch_funding_table():
    """Return list of (coin, funding_per_hour, oi_usd, mark_px) tuples."""
    r = _http_post(HL_INFO_URL, {'type': 'metaAndAssetCtxs'}, timeout=12)
    if not r or not isinstance(r, list) or len(r) < 2:
        return []
    meta, ctxs = r[0], r[1]
    universe = meta.get('universe', [])
    out = []
    for i, c in enumerate(ctxs):
        if i >= len(universe):
            break
        coin = universe[i].get('name')
        if not coin or universe[i].get('isDelisted'):
            continue
        try:
            f = float(c.get('funding', 0))
            mark = float(c.get('markPx', 0))
            oi = float(c.get('openInterest', 0)) * mark
        except (TypeError, ValueError):
            continue
        out.append((coin, f, oi, mark))
    return out


def detect_loop():
    _log(f"detect loop · threshold={THRESH_PCT_HR*100:.4f}%/hr · min OI=${MIN_OI_USD:,.0f}")
    while True:
        try:
            funding = fetch_funding_table()
            with _state_lock:
                _state['poll_count'] += 1
            if not funding:
                with _state_lock:
                    _state['poll_errors'] += 1
                time.sleep(POLL_SEC)
                continue
            now = time.time()
            for coin, f, oi, mark in funding:
                if oi < MIN_OI_USD:
                    continue
                if abs(f) < THRESH_PCT_HR:
                    continue
                # HL convention: positive funding = longs pay → longs crowded → fade SHORT
                # Negative funding = shorts pay → shorts crowded → fade LONG
                fade_side = 'S' if f > 0 else 'L'
                key = (coin, fade_side)
                last_ts = _last_signal.get(key, 0)
                if now - last_ts < DEDUP_SEC:
                    continue
                _last_signal[key] = now

                t0_mid = _hl_mid(coin)
                if t0_mid is None or t0_mid <= 0:
                    _log(f"signal {coin} {fade_side} but no mid → skip")
                    continue

                sig = {
                    'event': 'funding_spike',
                    'coin': coin,
                    'side': fade_side,  # fade direction
                    'funding_per_hour': f,
                    'funding_annual_pct': f * 24 * 365 * 100,
                    'oi_usd': oi,
                    'mark_px': mark,
                    't0_mid': t0_mid,
                    'detect_ts': now,
                    'snapshots': {str(o): None for o in SNAPSHOT_OFFSETS_SEC},
                    'complete': False,
                }
                with _pending_lock:
                    _pending.append(sig)
                with _state_lock:
                    _state['signals_detected'] += 1
                    _state['last_signal_coin'] = coin
                    _state['last_signal_ts'] = now
                    _state['per_coin'][coin]['signals'] += 1
                _log(f"SIGNAL {coin} fade={fade_side} funding={f*100:+.4f}%/hr "
                     f"({f*24*365*100:+.0f}% annual) OI=${oi:,.0f} t0_mid={t0_mid}")
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
            with _pending_lock:
                still_pending = []
                for ev in _pending:
                    age = now - ev['detect_ts']
                    t0 = ev.get('t0_mid')
                    for off in SNAPSHOT_OFFSETS_SEC:
                        k = str(off)
                        if ev['snapshots'].get(k) is not None:
                            continue
                        if age >= off - SCORE_INTERVAL_SEC / 2:
                            mid = _hl_mid(ev['coin'])
                            if mid is None or not t0:
                                continue
                            if ev['side'] == 'L':
                                pnl_pct = (mid - t0) / t0 * 100
                            else:
                                pnl_pct = (t0 - mid) / t0 * 100
                            ev['snapshots'][k] = {'mid': mid, 'pnl_pct': round(pnl_pct, 4)}
                            _append_jsonl(LOG_PATH, {
                                'event': 'signal_snapshot',
                                'coin': ev['coin'], 'side': ev['side'],
                                'detect_ts': ev['detect_ts'], 'offset_sec': off,
                                'mid': mid, 'pnl_pct': round(pnl_pct, 4),
                            })
                    all_captured = all(ev['snapshots'].get(str(o)) is not None for o in SNAPSHOT_OFFSETS_SEC)
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
                            pc = _state['per_coin'][ev['coin']]
                            if pnl_4h > 0: pc['wins_4h'] += 1
                            elif pnl_4h < 0: pc['losses_4h'] += 1
                            pc['pnl_pct_sum_4h'] += pnl_4h
                        _append_jsonl(LOG_PATH, {
                            'event': 'signal_complete',
                            'coin': ev['coin'], 'side': ev['side'],
                            'funding_per_hour': ev['funding_per_hour'],
                            'detect_ts': ev['detect_ts'], 't0_mid': t0,
                            'pnl_1h': ev['snapshots']['3600']['pnl_pct'],
                            'pnl_4h': pnl_4h,
                            'pnl_8h': ev['snapshots']['28800']['pnl_pct'],
                            'pnl_24h': pnl_24h,
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
                per_coin = {c: dict(v) for c, v in _state['per_coin'].items()}
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
                        'thresh_pct_hr': THRESH_PCT_HR * 100,
                        'min_oi_usd': MIN_OI_USD,
                        'poll_count': snap['poll_count'],
                        'poll_errors': snap['poll_errors'],
                        'signals_detected': snap['signals_detected'],
                        'signals_complete': snap['signals_complete'],
                        'wins_4h': w4, 'losses_4h': l4, 'wr_4h_pct': round(wr4, 1),
                        'wins_24h': w24, 'losses_24h': l24, 'wr_24h_pct': round(wr24, 1),
                        'last_signal_coin': snap.get('last_signal_coin'),
                        'last_signal_ts_ms': int(snap['last_signal_ts'] * 1000) if snap['last_signal_ts'] else 0,
                        'per_coin_top5': dict(sorted(per_coin.items(),
                                                      key=lambda x: -x[1]['signals'])[:5]),
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
            _log(f"STATS uptime={uptime}s polls={snap['poll_count']}/{snap['poll_errors']}err "
                 f"signals={snap['signals_detected']}/{snap['signals_complete']}cpl "
                 f"4h:{w4}/{l4} WR={wr4:.1f}%")
        except Exception as e:
            _log(f"stats err: {e}")


def main():
    _log(f"funding-fade starting · OBSERVE_ONLY · log={LOG_PATH}")
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
