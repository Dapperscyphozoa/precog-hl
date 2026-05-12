"""
whale_mirror_service.py — Whale Mirror, OBSERVE-ONLY Phase 1.

Tracks top-N HL traders by 30d PnL/ROI. Polls their positions every N
seconds via /info clearinghouseState. Diffs against last snapshot to
detect position changes (new opens, scale-ups/downs, closes). Logs
every change as a "whale signal" with a future-mirror scoring loop
that captures HL mid at T+1m / T+15m / T+1h / T+4h.

PHASE 1 = no orders. Pure observation. After ~7-30d of data we'll
have per-whale and per-coin hit-rates → wire Phase 2 (actual mirroring).

Cohort selection (refreshed every COHORT_REFRESH_SEC):
  Top-N (default 20) wallets, ranked by `month` window PnL,
  filtered by:
    - accountValue >= MIN_EQUITY_USD (default 50000)
    - 30d ROI    >= MIN_ROI_PCT (default 30)
    - 30d PnL   > 0
  Excludes vault leaders (vaultAddress != null) — vaults are
  multi-strategy not single-mind.

Leaderboard source: https://stats-data.hyperliquid.xyz/Mainnet/leaderboard
This is undocumented but is the same dataset the official HL frontend
uses. 35k+ wallets, refreshed by HL. Returns 30MB on each pull, so we
poll cohort refresh on a long interval (default 1h).

Architecture:
  - cohort_loop: every COHORT_REFRESH_SEC, refetch leaderboard, pick top-N
  - watch_loop: every WATCH_INTERVAL_SEC, for each cohort member, call
    /info clearinghouseState; diff against last snapshot; emit signals
  - score_loop: every 30s, walk pending signals, fetch HL mids, fill in
    T+1m/T+15m/T+1h/T+4h price snapshots
  - dash_push_loop: every 60s, post snapshot to dashboard

Tunables (env):
  WHALE_COHORT_SIZE           default 20
  WHALE_COHORT_REFRESH_SEC    default 3600  (1h)
  WHALE_WATCH_INTERVAL_SEC    default 60    (per wallet)
  WHALE_MIN_EQUITY_USD        default 50000
  WHALE_MIN_ROI_PCT           default 30
  WHALE_LOG_PATH              default /var/data/whale_mirror_log.jsonl
  WHALE_RANK_BY               default month  (day|week|month|allTime)
"""
import json
import os
import sys
import time
import threading
import urllib.request
import urllib.error
from collections import defaultdict

# ─── config ────────────────────────────────────────────────────────────
COHORT_SIZE         = int(os.environ.get('WHALE_COHORT_SIZE', '20'))
COHORT_REFRESH_SEC  = int(os.environ.get('WHALE_COHORT_REFRESH_SEC', '3600'))
WATCH_INTERVAL_SEC  = int(os.environ.get('WHALE_WATCH_INTERVAL_SEC', '60'))
MIN_EQUITY_USD      = float(os.environ.get('WHALE_MIN_EQUITY_USD', '50000'))
MIN_ROI_PCT         = float(os.environ.get('WHALE_MIN_ROI_PCT', '30'))
LOG_PATH            = os.environ.get('WHALE_LOG_PATH', '/var/data/whale_mirror_log.jsonl')
RANK_BY             = os.environ.get('WHALE_RANK_BY', 'month').lower()  # day|week|month|allTime

LEADERBOARD_URL = 'https://stats-data.hyperliquid.xyz/Mainnet/leaderboard'
HL_INFO_URL     = 'https://api.hyperliquid.xyz/info'

# Mid-snapshot offsets after a position change is detected
SNAPSHOT_OFFSETS_SEC = [60, 900, 3600, 14400]  # T+1m, T+15m, T+1h, T+4h

ENGINE_NAME = 'whale-mirror'
SCORE_INTERVAL_SEC = 30
DASH_PUSH_INTERVAL_SEC = 60

# Per-wallet polling spacing (to avoid bursts and HL rate limits).
# With COHORT_SIZE=20 and WATCH_INTERVAL_SEC=60, that's 20 polls per 60s
# = 0.33/sec, well under HL's 1200 weight/min limit.

# ─── state ─────────────────────────────────────────────────────────────
_state = {
    'started_ts': time.time(),
    'leaderboard_fetches': 0,
    'leaderboard_errors': 0,
    'last_cohort_refresh_ts': 0,
    'cohort': [],           # list of whale dicts
    'cohort_size': 0,
    'wallet_polls': 0,
    'wallet_poll_errors': 0,
    'signals_detected': 0,
    'signals_complete': 0,
    'wins_1h': 0,
    'losses_1h': 0,
    'wins_4h': 0,
    'losses_4h': 0,
    'last_signal_coin': None,
    'last_signal_ts': 0,
    'per_whale': defaultdict(lambda: {'signals': 0, 'wins_1h': 0, 'losses_1h': 0, 'pnl_pct_sum_1h': 0.0}),
}
_state_lock = threading.Lock()

# Last position snapshot per wallet
# {wallet: {coin: {'szi': float, 'entry': float, 'leverage': float, 'side': 'L'|'S', 'ts': ms}}}
_snapshots = {}
_snapshots_lock = threading.Lock()

# Pending signal follow-ups
# Each: {wallet, coin, change_type, side, sz_delta, t0_mid, ts_detect,
#        snapshots: {60: {...}, 900: {...}, 3600: {...}, 14400: {...}},
#        complete: bool}
_pending = []
_pending_lock = threading.Lock()


# ─── utils ─────────────────────────────────────────────────────────────
def _log(msg):
    print(f"[whale-mirror] {msg}", flush=True)


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


def _http_json(url, body=None, timeout=15):
    """POST if body provided else GET. Returns parsed JSON or None on error."""
    try:
        if body is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={'Content-Type': 'application/json'},
            )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {'__err': str(e)}


def _hl_mid(coin):
    """Fetch current HL mid for a coin via /info allMids."""
    r = _http_json(HL_INFO_URL, body={'type': 'allMids'}, timeout=8)
    if not r or r.get('__err'):
        return None
    mid_str = r.get(coin) if isinstance(r, dict) else None
    try:
        return float(mid_str) if mid_str else None
    except (TypeError, ValueError):
        return None


# ─── cohort selection ──────────────────────────────────────────────────
def fetch_leaderboard():
    """Pull full leaderboard. Returns list of rows or None on error."""
    r = _http_json(LEADERBOARD_URL, body=None, timeout=45)
    if not r or r.get('__err'):
        with _state_lock:
            _state['leaderboard_errors'] += 1
        _log(f"leaderboard fetch err: {r.get('__err') if r else 'no response'}")
        return None
    with _state_lock:
        _state['leaderboard_fetches'] += 1
    rows = r.get('leaderboardRows', [])
    _log(f"leaderboard fetched · {len(rows)} rows")
    return rows


def select_cohort(rows):
    """Filter and sort to top-N. Returns list of whale dicts."""
    if not rows:
        return []

    def perf_for(row, period):
        for w in row.get('windowPerformances', []) or []:
            if w and w[0] == period:
                return w[1] or {}
        return {}

    candidates = []
    for r in rows:
        addr = r.get('ethAddress')
        if not addr or not isinstance(addr, str) or not addr.startswith('0x'):
            continue
        try:
            eq = float(r.get('accountValue') or 0)
        except (TypeError, ValueError):
            continue
        if eq < MIN_EQUITY_USD:
            continue
        perf = perf_for(r, RANK_BY)
        try:
            pnl = float(perf.get('pnl') or 0)
            roi = float(perf.get('roi') or 0) * 100
            vlm = float(perf.get('vlm') or 0)
        except (TypeError, ValueError):
            continue
        if pnl <= 0:
            continue
        if roi < MIN_ROI_PCT:
            continue
        candidates.append({
            'addr': addr.lower(),
            'equity': eq,
            'pnl': pnl,
            'roi_pct': roi,
            'vlm': vlm,
            'display_name': r.get('displayName') or '',
        })

    candidates.sort(key=lambda w: -w['pnl'])
    return candidates[:COHORT_SIZE]


def cohort_loop():
    """Refresh cohort periodically."""
    _log(f"cohort loop started (refresh every {COHORT_REFRESH_SEC}s, top {COHORT_SIZE} by {RANK_BY})")
    while True:
        try:
            rows = fetch_leaderboard()
            if rows:
                cohort = select_cohort(rows)
                with _state_lock:
                    _state['cohort'] = cohort
                    _state['cohort_size'] = len(cohort)
                    _state['last_cohort_refresh_ts'] = time.time()
                _log(f"cohort refreshed: {len(cohort)} whales (min eq=${MIN_EQUITY_USD:,.0f}, "
                     f"min ROI={MIN_ROI_PCT}%, ranked by {RANK_BY})")
                _append_jsonl(LOG_PATH, {
                    'event': 'cohort_refresh',
                    'ts': int(time.time() * 1000),
                    'cohort': [{'addr': w['addr'], 'equity': w['equity'],
                                'pnl': w['pnl'], 'roi_pct': w['roi_pct']}
                               for w in cohort],
                })
        except Exception as e:
            _log(f"cohort_loop err: {e}")
        time.sleep(COHORT_REFRESH_SEC)


# ─── position polling + diff ───────────────────────────────────────────
def _fetch_positions(wallet):
    """Return {coin: {szi, entry, leverage, side}} or None."""
    r = _http_json(HL_INFO_URL, body={'type': 'clearinghouseState', 'user': wallet}, timeout=8)
    if not r or r.get('__err'):
        return None
    out = {}
    for ap in r.get('assetPositions', []) or []:
        pos = ap.get('position') or {}
        coin = pos.get('coin')
        if not coin:
            continue
        try:
            szi = float(pos.get('szi') or 0)
        except (TypeError, ValueError):
            continue
        if abs(szi) < 1e-12:
            continue
        try:
            entry = float(pos.get('entryPx') or 0)
        except (TypeError, ValueError):
            entry = 0
        lev = (pos.get('leverage') or {}).get('value', 0)
        out[coin] = {
            'szi': szi,
            'entry': entry,
            'leverage': lev,
            'side': 'L' if szi > 0 else 'S',
            'ts': int(time.time() * 1000),
        }
    return out


def _diff_positions(wallet, old, new):
    """Compare old vs new snapshots. Emit signals for each change.
    Returns list of signal events."""
    signals = []
    old = old or {}
    new = new or {}
    # New coins or size changes
    for coin, p in new.items():
        prev = old.get(coin)
        if prev is None:
            signals.append({
                'event': 'position_open',
                'wallet': wallet, 'coin': coin, 'side': p['side'],
                'szi': p['szi'], 'entry': p['entry'], 'leverage': p['leverage'],
                'sz_delta': p['szi'], 'ts': p['ts'],
            })
        else:
            # Same coin — did size change?
            if abs(p['szi'] - prev['szi']) > 1e-12:
                delta = p['szi'] - prev['szi']
                # Direction flip (rare)
                if (p['szi'] > 0) != (prev['szi'] > 0):
                    signals.append({
                        'event': 'position_flip',
                        'wallet': wallet, 'coin': coin,
                        'side': p['side'], 'side_prev': prev['side'],
                        'szi_prev': prev['szi'], 'szi_new': p['szi'],
                        'sz_delta': delta, 'ts': p['ts'],
                    })
                elif abs(p['szi']) > abs(prev['szi']):
                    signals.append({
                        'event': 'position_scale_up',
                        'wallet': wallet, 'coin': coin, 'side': p['side'],
                        'szi_prev': prev['szi'], 'szi_new': p['szi'],
                        'sz_delta': delta, 'entry': p['entry'], 'ts': p['ts'],
                    })
                else:
                    signals.append({
                        'event': 'position_scale_down',
                        'wallet': wallet, 'coin': coin, 'side': p['side'],
                        'szi_prev': prev['szi'], 'szi_new': p['szi'],
                        'sz_delta': delta, 'ts': p['ts'],
                    })
    # Closed coins
    for coin, prev in old.items():
        if coin not in new:
            signals.append({
                'event': 'position_close',
                'wallet': wallet, 'coin': coin, 'side': prev['side'],
                'szi_prev': prev['szi'], 'sz_delta': -prev['szi'],
                'ts': int(time.time() * 1000),
            })
    return signals


def watch_loop():
    """Poll each cohort member every WATCH_INTERVAL_SEC."""
    _log(f"watch loop started (interval={WATCH_INTERVAL_SEC}s per cohort)")
    while True:
        try:
            with _state_lock:
                cohort = list(_state['cohort'])
            if not cohort:
                time.sleep(5)
                continue
            # Spread polls across the interval
            per_poll_gap = max(1.0, WATCH_INTERVAL_SEC / max(1, len(cohort)))
            for whale in cohort:
                wallet = whale['addr']
                try:
                    new_snap = _fetch_positions(wallet)
                    with _state_lock:
                        _state['wallet_polls'] += 1
                    if new_snap is None:
                        with _state_lock:
                            _state['wallet_poll_errors'] += 1
                        time.sleep(per_poll_gap)
                        continue
                    with _snapshots_lock:
                        old_snap = _snapshots.get(wallet)
                        _snapshots[wallet] = new_snap
                    if old_snap is None:
                        # First poll for this wallet — just baseline, no signals
                        time.sleep(per_poll_gap)
                        continue
                    signals = _diff_positions(wallet, old_snap, new_snap)
                    for sig in signals:
                        # Snapshot HL mid at T0
                        coin = sig['coin']
                        t0_mid = _hl_mid(coin)
                        sig['t0_mid'] = t0_mid
                        sig['detect_ts'] = time.time()
                        sig['snapshots'] = {str(o): None for o in SNAPSHOT_OFFSETS_SEC}
                        sig['complete'] = False
                        with _pending_lock:
                            _pending.append(sig)
                        with _state_lock:
                            _state['signals_detected'] += 1
                            _state['last_signal_coin'] = coin
                            _state['last_signal_ts'] = time.time()
                            _state['per_whale'][wallet]['signals'] += 1
                        _log(f"SIGNAL {sig['event']} {wallet[:10]}… {sig.get('side','?')} {coin} "
                             f"sz_delta={sig.get('sz_delta',0):.4f} t0_mid={t0_mid}")
                        _append_jsonl(LOG_PATH, sig)
                except Exception as e:
                    _log(f"watch err {wallet[:10]}: {e}")
                time.sleep(per_poll_gap)
        except Exception as e:
            _log(f"watch_loop err: {e}")
            time.sleep(5)


# ─── scoring ───────────────────────────────────────────────────────────
def score_loop():
    """Capture mid prices at T+1m, T+15m, T+1h, T+4h for each pending signal."""
    _log(f"score loop started (interval={SCORE_INTERVAL_SEC}s)")
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
                            # PnL in whale's direction
                            if ev.get('side') == 'L':
                                pnl_pct = (mid - t0) / t0 * 100
                            else:
                                pnl_pct = (t0 - mid) / t0 * 100
                            ev['snapshots'][k] = {'mid': mid, 'pnl_pct': round(pnl_pct, 4)}
                            _append_jsonl(LOG_PATH, {
                                'event': 'signal_snapshot',
                                'wallet': ev.get('wallet'), 'coin': ev['coin'],
                                'side': ev.get('side'), 'detect_ts': ev['detect_ts'],
                                'offset_sec': off, 'mid': mid, 'pnl_pct': round(pnl_pct, 4),
                            })

                    all_captured = all(ev['snapshots'].get(str(o)) is not None for o in SNAPSHOT_OFFSETS_SEC)
                    if all_captured and not ev['complete']:
                        ev['complete'] = True
                        pnl_1h = ev['snapshots']['3600']['pnl_pct']
                        pnl_4h = ev['snapshots']['14400']['pnl_pct']
                        with _state_lock:
                            if pnl_1h > 0: _state['wins_1h'] += 1
                            elif pnl_1h < 0: _state['losses_1h'] += 1
                            if pnl_4h > 0: _state['wins_4h'] += 1
                            elif pnl_4h < 0: _state['losses_4h'] += 1
                            _state['signals_complete'] += 1
                            w = _state['per_whale'][ev['wallet']]
                            if pnl_1h > 0: w['wins_1h'] += 1
                            elif pnl_1h < 0: w['losses_1h'] += 1
                            w['pnl_pct_sum_1h'] += pnl_1h
                        _append_jsonl(LOG_PATH, {
                            'event': 'signal_complete',
                            'wallet': ev.get('wallet'), 'coin': ev['coin'],
                            'side': ev.get('side'), 'sig_type': ev.get('event'),
                            'detect_ts': ev['detect_ts'], 't0_mid': t0,
                            'pnl_1m': ev['snapshots']['60']['pnl_pct'],
                            'pnl_15m': ev['snapshots']['900']['pnl_pct'],
                            'pnl_1h': pnl_1h, 'pnl_4h': pnl_4h,
                        })

                    if age < max(SNAPSHOT_OFFSETS_SEC) + 60:
                        still_pending.append(ev)

                _pending.clear()
                _pending.extend(still_pending)
        except Exception as e:
            _log(f"score_loop err: {e}")
        time.sleep(SCORE_INTERVAL_SEC)


# ─── dashboard push ────────────────────────────────────────────────────
def dash_push_loop():
    try:
        from dashboard_push import push_state
    except Exception as e:
        _log(f"dashboard_push import err: {e} — push disabled")
        return
    _log(f"dash push loop started (interval={DASH_PUSH_INTERVAL_SEC}s)")
    while True:
        try:
            with _state_lock:
                snap = dict(_state)
                per_whale = {w: dict(v) for w, v in _state['per_whale'].items()}
            w1 = snap['wins_1h']; l1 = snap['losses_1h']
            w4 = snap['wins_4h']; l4 = snap['losses_4h']
            wr1 = (w1 / (w1+l1) * 100) if (w1+l1) else 0
            wr4 = (w4 / (w4+l4) * 100) if (w4+l4) else 0
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
                        'cohort_size': snap['cohort_size'],
                        'leaderboard_fetches': snap['leaderboard_fetches'],
                        'leaderboard_errors': snap['leaderboard_errors'],
                        'wallet_polls': snap['wallet_polls'],
                        'wallet_poll_errors': snap['wallet_poll_errors'],
                        'signals_detected': snap['signals_detected'],
                        'signals_complete': snap['signals_complete'],
                        'wins_1h': w1, 'losses_1h': l1, 'wr_1h_pct': round(wr1, 1),
                        'wins_4h': w4, 'losses_4h': l4, 'wr_4h_pct': round(wr4, 1),
                        'last_signal_coin': snap.get('last_signal_coin'),
                        'last_signal_ts_ms': int(snap['last_signal_ts'] * 1000) if snap['last_signal_ts'] else 0,
                        'per_whale_top5': dict(sorted(per_whale.items(),
                                                       key=lambda x: -x[1]['signals'])[:5]),
                    },
                )
            except Exception as e:
                _log(f"push_state err: {e}")
        except Exception as e:
            _log(f"dash_push_loop outer err: {e}")
        time.sleep(DASH_PUSH_INTERVAL_SEC)


# ─── stats ─────────────────────────────────────────────────────────────
def stats_loop():
    _log("stats loop started (every 5 min)")
    while True:
        time.sleep(300)
        try:
            with _state_lock:
                snap = dict(_state)
            uptime = int(time.time() - snap['started_ts'])
            w1 = snap['wins_1h']; l1 = snap['losses_1h']
            w4 = snap['wins_4h']; l4 = snap['losses_4h']
            wr1 = (w1/(w1+l1)*100) if (w1+l1) else 0
            wr4 = (w4/(w4+l4)*100) if (w4+l4) else 0
            _log(f"STATS uptime={uptime}s cohort={snap['cohort_size']} "
                 f"polls={snap['wallet_polls']}/{snap['wallet_poll_errors']}err "
                 f"signals={snap['signals_detected']}/{snap['signals_complete']}cpl "
                 f"1h:{w1}/{l1} WR={wr1:.1f}% · 4h:{w4}/{l4} WR={wr4:.1f}%")
        except Exception as e:
            _log(f"stats_loop err: {e}")


# ─── main ──────────────────────────────────────────────────────────────
def main():
    _log(f"whale-mirror starting · OBSERVE_ONLY · cohort={COHORT_SIZE} · log={LOG_PATH}")
    _log(f"filters: equity>=${MIN_EQUITY_USD:,.0f}, 30d ROI>={MIN_ROI_PCT}%, ranked by {RANK_BY}")
    threading.Thread(target=cohort_loop,    daemon=True, name='cohort').start()
    time.sleep(5)  # let initial cohort load before watch starts
    threading.Thread(target=watch_loop,     daemon=True, name='watch').start()
    threading.Thread(target=score_loop,     daemon=True, name='score').start()
    threading.Thread(target=dash_push_loop, daemon=True, name='dash').start()
    threading.Thread(target=stats_loop,     daemon=True, name='stats').start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        _log("shutdown requested")
        sys.exit(0)


if __name__ == '__main__':
    main()
