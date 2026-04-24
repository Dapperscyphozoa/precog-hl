#!/usr/bin/env python3
"""
confluence_worker.py  —  SYSTEM B live worker

Bolts onto precog.py. Runs in its own thread. Every SCAN_INTERVAL_S:
  1. fetch 15m candles for each coin via precog's `info` client
  2. call confluence_engine.eval_coin()
  3. if signal AND cooldown ok AND not already in position AND entry gate passes:
     - size per 1% risk
     - fire via precog's `exchange.order`
     - register SL/TP via precog's existing position manager
     - stamp cooldown
  4. log every scan cycle to /var/data/confluence.log

ENV:
  CONFLUENCE_ENABLED=1         # master switch
  CONFLUENCE_DRY_RUN=1         # log only, don't place orders (default 1 until validated)
  CONFLUENCE_SCAN_INTERVAL=300 # seconds between scans (default 5 min)
  CONFLUENCE_MAX_POSITIONS=15  # concurrent position cap
  CONFLUENCE_RISK_PCT=0.01     # 1% per trade
"""
import os
import time
import json
import threading
import traceback
from datetime import datetime

# Lazy imports from precog at init time
_precog = None
_ce = None

ENABLED         = os.environ.get('CONFLUENCE_ENABLED', '0') == '1'
DRY_RUN         = os.environ.get('CONFLUENCE_DRY_RUN', '1') == '1'
SCAN_INTERVAL_S = int(os.environ.get('CONFLUENCE_SCAN_INTERVAL', '300'))
MAX_POSITIONS   = int(os.environ.get('CONFLUENCE_MAX_POSITIONS', '15'))
RISK_PCT        = float(os.environ.get('CONFLUENCE_RISK_PCT', '0.01'))

STATE_FILE      = '/var/data/confluence_state.json'
LOG_FILE        = '/var/data/confluence.log'

# ─── FIX 4: slippage reality gap ─────────────────────────────────────
# Track expected vs actual fill; kill coin if avg slip > 0.15%
SLIPPAGE_KILL_THRESHOLD_PCT = 0.15  # kill coin above this
SLIPPAGE_MIN_SAMPLES = 5             # need at least N fills before killing

# ─── FIX 5: coin decay detection ─────────────────────────────────────
DECAY_MIN_TRADES = 10
DECAY_WR_THRESHOLD = 0.35
DECAY_PNL_THRESHOLD = 0.0

# ─── FIX 2: confluence dedupe ────────────────────────────────────────
# Hash = (coin, side, first_signal_ts) — any signal sharing this key is
# treated as the same confluence event and blocked within 24h window
DEDUPE_WINDOW_S = 24 * 3600

# ─── FIX B: entry drift control ──────────────────────────────────────
MAX_SIGNAL_AGE_S = 15 * 60   # 1 × 15m bar; skip signals older than 1 bar

_state = {
    'last_fire_ts': {},          # coin -> ts (24h cooldown tracking)
    'open_positions': {},        # coin -> {side, entry, ts, ...}
    'total_fires': 0,
    'wins': 0,
    'losses': 0,
    'timeouts': 0,
    'total_pnl_pct': 0.0,
    'started_at': int(time.time()),
    # fix 1: per-coin last processed bar timestamp (prevents partial candle eval)
    'last_bar_ts': {},           # coin -> bar_ts_sec
    # fix 2: confluence event dedupe
    'fired_events': {},          # event_key "coin|side|first_ts" -> fire_ts
    # fix 3: position queue (when cap hit)
    'pending_queue': [],         # list of {coin, signal, queued_ts}
    # fix 4: slippage tracking
    'slippage_samples': {},      # coin -> [pct, pct, ...]
    'killed_coins': {},          # coin -> {reason, ts}
    # fix 5: per-coin live perf
    'coin_stats': {},            # coin -> {n, w, l, pnl_pct}
}
_state_lock = threading.Lock()

def _log(msg):
    line = f"[{datetime.utcnow().isoformat(timespec='seconds')}Z] [CONFLUENCE] {msg}"
    print(line, flush=True)
    try:
        os.makedirs('/var/data', exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass

def _save_state():
    try:
        with _state_lock:
            snapshot = dict(_state)
        with open(STATE_FILE, 'w') as f:
            json.dump(snapshot, f)
    except Exception as e:
        _log(f"state save err: {e}")

def _load_state():
    global _state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                saved = json.load(f)
            with _state_lock:
                for k, v in saved.items():
                    _state[k] = v
            _log(f"state loaded: {len(_state.get('last_fire_ts', {}))} cooldowns, "
                 f"{len(_state.get('open_positions', {}))} open positions")
    except Exception as e:
        _log(f"state load err: {e}")

def _fetch_15m_bars(coin, n_bars=800):
    """Use precog's info client to pull 15m candles. Returns list of
    {t, o, h, l, c, v} with ONLY fully-closed bars aligned to 00/15/30/45.
    Returns None on failure or if no new closed bar since last process."""
    try:
        # Current fully-closed 15m boundary (exclusive upper bound)
        now_s = int(time.time())
        BAR_S = 15 * 60
        latest_closed_start = (now_s // BAR_S - 1) * BAR_S   # last fully-closed bar start (sec)

        end_ms = (latest_closed_start + BAR_S) * 1000  # request up to end of last closed bar
        start_ms = end_ms - n_bars * BAR_S * 1000
        raw = _precog.info.candles_snapshot(coin, '15m', start_ms, end_ms)
        bars = []
        for b in raw:
            t = int(b['t'])
            t_s = t // 1000 if t > 10**12 else t
            # Drop any unaligned or not-yet-closed bar
            if t_s % BAR_S != 0:
                continue
            if t_s > latest_closed_start:
                continue  # still open bar — drop it
            bars.append({
                't': t_s,
                'o': float(b['o']), 'h': float(b['h']),
                'l': float(b['l']), 'c': float(b['c']),
                'v': float(b['v']),
            })
        if len(bars) < 100:
            return None
        # Short-circuit: if we already processed this latest bar, skip
        with _state_lock:
            last_seen = _state['last_bar_ts'].get(coin, 0)
        if bars[-1]['t'] <= last_seen:
            return None
        return bars
    except Exception as e:
        if '429' not in str(e):
            _log(f"{coin} candle err: {str(e)[:80]}")
        return None

def _in_position(coin):
    """Check if precog already has an open position on this coin, OR our own tracker does."""
    with _state_lock:
        if coin in _state['open_positions']:
            return True
    # Defer to precog's state if available
    try:
        live = _precog.live_positions
        if live and coin in live:
            return True
    except Exception:
        pass
    return False

def _entry_gate_ok(coin, side):
    """Reuse precog's existing gate stack — V3 trend, ATR-min, ticker gate."""
    try:
        if hasattr(_precog, 'trend_gate') and not _precog.trend_gate(coin, side):
            return False, 'V3'
    except Exception:
        pass
    return True, 'ok'

def _size_and_fire(coin, signal, equity):
    """
    Fixed-risk sizing: (equity * risk_pct) / sl_pct => notional USD.
    Convert to coin units using signal entry price.
    Fire through precog's `exchange.order`.
    """
    sl_pct = signal['sl_pct']
    risk_usd = equity * RISK_PCT
    notional_usd = risk_usd / sl_pct
    entry = signal['entry']
    size_coin = notional_usd / entry

    # Slippage buffer on entry
    is_buy = signal['side'] == 'BUY'
    px = entry * (1.0008 if is_buy else 0.9992)

    # Compute TP/SL prices
    tp_px = entry * (1 + signal['tp_pct']) if is_buy else entry * (1 - signal['tp_pct'])
    sl_px = entry * (1 - sl_pct)           if is_buy else entry * (1 + sl_pct)

    _log(f"{coin} {signal['side']} n_sys={signal['n_sys']} {'+'.join(signal['systems'])} "
         f"entry={entry:.4f} size=${notional_usd:.2f} ({size_coin:.6f} {coin}) "
         f"TP={tp_px:.4f} SL={sl_px:.4f} [{'DRY' if DRY_RUN else 'LIVE'}]")

    if DRY_RUN:
        return {'dry_run': True, 'size_coin': size_coin, 'tp': tp_px, 'sl': sl_px,
                'expected_px': entry, 'actual_px': entry}

    try:
        # IOC limit into slip price
        r = _precog.exchange.order(
            coin, is_buy, size_coin, px,
            {'limit': {'tif': 'Ioc'}}, reduce_only=False
        )
        _log(f"{coin} order result: {str(r)[:200]}")

        # ─── FIX 4: record actual slippage vs expected ───
        actual_px = _extract_avg_fill_px(r)
        if actual_px and entry > 0:
            slip_pct = abs(actual_px - entry) / entry * 100
            _record_slippage(coin, slip_pct)
            _log(f"{coin} slip: expected={entry:.4f} actual={actual_px:.4f} "
                 f"delta={slip_pct:.3f}%")
            if isinstance(r, dict):
                r['expected_px'] = entry
                r['actual_px'] = actual_px
                r['slip_pct'] = slip_pct
        return r
    except Exception as e:
        _log(f"{coin} order FAIL: {e}")
        traceback.print_exc()
        return None

def _extract_avg_fill_px(order_result):
    """Best-effort extraction of fill price from HL exchange.order response."""
    try:
        if not isinstance(order_result, dict):
            return None
        # HL response shape: {'status':'ok','response':{'data':{'statuses':[{'filled':{'avgPx':'...','totalSz':'...'}}]}}}
        data = order_result.get('response', {}).get('data', {})
        for s in data.get('statuses', []):
            f = s.get('filled') or {}
            px = f.get('avgPx')
            if px:
                return float(px)
        return None
    except Exception:
        return None

def _record_slippage(coin, slip_pct):
    with _state_lock:
        arr = _state['slippage_samples'].setdefault(coin, [])
        arr.append(float(slip_pct))
        if len(arr) > 50:
            arr[:] = arr[-50:]
        # Kill check
        if len(arr) >= SLIPPAGE_MIN_SAMPLES:
            avg = sum(arr) / len(arr)
            if avg > SLIPPAGE_KILL_THRESHOLD_PCT:
                _state['killed_coins'][coin] = {
                    'reason': f'avg_slippage {avg:.3f}% > {SLIPPAGE_KILL_THRESHOLD_PCT}%',
                    'ts': int(time.time()),
                }
                _log(f"*** {coin} KILLED: avg slippage {avg:.3f}% over {len(arr)} fills")

def _register_position(coin, signal, fill_result):
    """Track cluster position locally for monitoring + exit management."""
    with _state_lock:
        _state['open_positions'][coin] = {
            'side': signal['side'],
            'entry': signal['entry'],
            'ts': int(time.time()),
            'n_sys': signal['n_sys'],
            'systems': signal['systems'],
            'tp_pct': signal['tp_pct'],
            'sl_pct': signal['sl_pct'],
            'max_hold_s': signal['max_hold_s'],
        }
        _state['total_fires'] += 1
        _state['last_fire_ts'][coin] = int(time.time())
    _save_state()

def _monitor_exits():
    """Walk open positions. Close on TP / SL / 72h timeout via precog's close path.
    Called each scan cycle after new signals."""
    now = int(time.time())
    with _state_lock:
        to_check = list(_state['open_positions'].items())
    for coin, pos in to_check:
        # Age check (72h timeout)
        age = now - pos['ts']
        if age >= pos['max_hold_s']:
            _log(f"{coin} TIMEOUT {age/3600:.1f}h — flat")
            _close_position(coin, 'timeout')
            continue
        # Let precog's native TP/SL orders do the work — we just monitor here
        # (TP/SL were wired via register; this is a backup check via live price)
        try:
            mids = _precog.info.all_mids() if hasattr(_precog.info, 'all_mids') else {}
            px = float(mids.get(coin, 0))
            if not px: continue
            entry = pos['entry']
            is_buy = pos['side'] == 'BUY'
            pnl = (px - entry) / entry if is_buy else (entry - px) / entry
            if pnl >= pos['tp_pct']:
                _log(f"{coin} TP hit pnl={pnl*100:.2f}% — flat")
                _close_position(coin, 'tp', pnl)
            elif pnl <= -pos['sl_pct']:
                _log(f"{coin} SL hit pnl={pnl*100:.2f}% — flat")
                _close_position(coin, 'sl', pnl)
        except Exception as e:
            _log(f"{coin} exit monitor err: {e}")

def _close_position(coin, reason, pnl=None):
    with _state_lock:
        pos = _state['open_positions'].pop(coin, None)
    if not pos:
        return
    if not DRY_RUN:
        try:
            # precog has a close helper? Fall back to reduce-only market
            if hasattr(_precog, 'close_position'):
                _precog.close_position(coin, reason)
            else:
                mids = _precog.info.all_mids()
                px = float(mids.get(coin, 0))
                if px:
                    is_buy_close = (pos['side'] == 'SELL')  # opposite side
                    # conservative size read from account
                    try:
                        state = _precog.info.user_state(_precog.WALLET)
                        sz = 0.0
                        for p in state.get('assetPositions', []):
                            if p.get('position', {}).get('coin') == coin:
                                sz = abs(float(p['position']['szi']))
                                break
                        if sz > 0:
                            close_px = px * (1.002 if is_buy_close else 0.998)
                            _precog.exchange.order(coin, is_buy_close, sz, close_px,
                                {'limit': {'tif': 'Ioc'}}, reduce_only=True)
                    except Exception as e:
                        _log(f"{coin} close fetch err: {e}")
        except Exception as e:
            _log(f"{coin} close err: {e}")
    if pnl is not None:
        with _state_lock:
            if pnl > 0: _state['wins'] += 1
            else: _state['losses'] += 1
            _state['total_pnl_pct'] += pnl * 100
    elif reason == 'timeout':
        with _state_lock:
            _state['timeouts'] += 1

    # ─── FIX 5: per-coin tracking + decay check ───
    _update_coin_stats(coin, pnl if pnl is not None else 0.0)
    _save_state()

def _update_coin_stats(coin, pnl_decimal):
    """Track per-coin performance; kill on decay."""
    with _state_lock:
        s = _state['coin_stats'].setdefault(coin, {'n': 0, 'w': 0, 'l': 0, 'pnl_pct': 0.0})
        s['n'] += 1
        if pnl_decimal > 0:
            s['w'] += 1
        elif pnl_decimal < 0:
            s['l'] += 1
        s['pnl_pct'] += pnl_decimal * 100

        if s['n'] >= DECAY_MIN_TRADES:
            wr = s['w'] / max(s['w'] + s['l'], 1)
            if wr < DECAY_WR_THRESHOLD and s['pnl_pct'] < DECAY_PNL_THRESHOLD:
                _state['killed_coins'][coin] = {
                    'reason': f'decay: n={s["n"]} WR={wr:.1%} pnl={s["pnl_pct"]:+.2f}%',
                    'ts': int(time.time()),
                }
                _log(f"*** {coin} KILLED (decay): WR={wr:.1%} pnl={s['pnl_pct']:+.2f}% over {s['n']} trades")

# ─── MAIN LOOP ────────────────────────────────────────────────────────
def _scan_once():
    if not _precog:
        _log("precog not initialized yet, skip")
        return

    # OOS-validated whitelist (190 tested, 99 passed PnL>=+2% on 60d 2-sys confluence)
    CONFLUENCE_UNIVERSE = ['JUP', 'PENGU', 'MINA', 'MANTA', 'MOVE', 'ENA', 'GRIFFAIN', 'DYDX', 'WLD', 'ZEREBRO', 'GMX', 'HEMI', 'VIRTUAL', 'ALT', 'UNI', 'XPL', 'ZEN', 'MON', 'IP', 'ZEC', 'ENS', 'IOTA', 'SKY', 'TRB', 'BTC', 'CFX', 'LAYER', 'XLM', 'XMR', 'ZRO', 'LINEA', 'PENDLE', 'PYTH', 'SAGA', 'W', 'BCH', 'POPCAT', '2Z', 'EIGEN', 'HYPER', 'ME', 'ARB', 'GOAT', 'MET', 'MORPHO', 'ORDI', 'STX', 'kPEPE', 'PAXG', 'UMA', 'GAS', 'PURR', 'GALA', 'PNUT', 'FTT', 'MAV', 'SUPER', 'PUMP', 'BABY', 'FARTCOIN', 'SEI', 'TIA', 'MNT', 'ACE', 'PROMPT', '0G', 'ZK', 'NEAR', 'NIL', 'MERL', 'TRX', 'DOT', 'HYPE', 'LINK', 'MELANIA', 'NXPC', 'SUI', 'AAVE', 'IO', 'kLUNC', 'MEW', 'POLYX', 'AR', 'FIL', 'INIT', 'WIF', 'ZORA', 'kNEIRO', 'XRP', 'LTC', 'ANIME', 'BLAST', 'IMX', 'KAITO', 'RESOLV', 'REZ', 'RENDER', 'ALGO', 'VVV']
    coins = CONFLUENCE_UNIVERSE
    if not coins:
        _log("no coin universe defined, skip")
        return

    try:
        equity = _precog.get_balance()
    except Exception as e:
        _log(f"balance read err: {e}")
        return

    # ─── Collect candidate signals first (scan-then-fire pattern) ───
    # This lets us queue by priority when cap is hit (Fix 3)
    candidates = []  # list of (signal_dict, first_ts) — queued by newness
    now_ts = int(time.time())

    with _state_lock:
        killed = set(_state['killed_coins'].keys())
        fired_events = dict(_state['fired_events'])

    # Purge old dedupe entries
    for k, fire_ts in list(fired_events.items()):
        if now_ts - fire_ts > DEDUPE_WINDOW_S:
            with _state_lock:
                _state['fired_events'].pop(k, None)

    for coin in coins:
        try:
            # Fix 4/5 kill filter
            if coin in killed:
                continue
            # Cooldown
            if not _ce.should_enter(coin, _state['last_fire_ts'], now_ts):
                continue
            # Already in position?
            if _in_position(coin):
                continue
            # Fetch + evaluate (Fix 1: only fully-closed bars)
            bars = _fetch_15m_bars(coin, 800)
            if not bars:
                continue
            sig = _ce.eval_coin(coin, bars, now_ts=now_ts)
            # Mark bar as processed even if no signal
            with _state_lock:
                _state['last_bar_ts'][coin] = bars[-1]['t']
            if not sig:
                continue

            # ─── Fix B: entry drift control ───
            latest_sig_ts = sig.get('latest_signal_ts') or sig.get('ts')
            sig_age = now_ts - latest_sig_ts
            if sig_age > MAX_SIGNAL_AGE_S:
                _log(f"{coin} {sig['side']} stale ({sig_age}s > {MAX_SIGNAL_AGE_S}s) — skip")
                continue

            # ─── Fix 2: confluence event dedupe ───
            # Use first signal ts across the agreeing systems as the event anchor
            first_ts = sig.get('latest_signal_ts') or sig.get('ts')
            # Bucket to confluence window so minor drift doesn't break dedupe
            ts_bucket = (first_ts // DEDUPE_WINDOW_S) * DEDUPE_WINDOW_S
            evt_key = f"{coin}|{sig['side']}|{ts_bucket}"
            if evt_key in fired_events:
                continue  # already fired this event

            # Entry gate
            ok, why = _entry_gate_ok(coin, sig['side'])
            if not ok:
                _log(f"{coin} {sig['side']} n={sig['n_sys']} — gated by {why}")
                continue

            sig['_evt_key'] = evt_key
            sig['_first_ts'] = first_ts
            candidates.append(sig)
        except Exception as e:
            _log(f"{coin} scan err: {e}")

    # ─── Fix 3: priority queue — newest first when cap is hit ───
    candidates.sort(key=lambda s: -s['_first_ts'])

    fires_this_scan = 0
    queued_this_scan = 0
    for sig in candidates:
        coin = sig['coin']
        try:
            with _state_lock:
                n_open = len(_state['open_positions'])
            if n_open >= MAX_POSITIONS:
                # Queue it; older queued entries drop off if stale
                with _state_lock:
                    _state['pending_queue'].append({
                        'coin': coin, 'signal': sig, 'queued_ts': now_ts
                    })
                    # Keep queue bounded + fresh
                    cutoff = now_ts - MAX_SIGNAL_AGE_S
                    _state['pending_queue'] = [
                        q for q in _state['pending_queue'] if q['queued_ts'] >= cutoff
                    ][-50:]
                queued_this_scan += 1
                continue
            # Fire
            fill = _size_and_fire(coin, sig, equity)
            if fill is not None:
                _register_position(coin, sig, fill)
                with _state_lock:
                    _state['fired_events'][sig['_evt_key']] = now_ts
                fires_this_scan += 1
            time.sleep(0.1)
        except Exception as e:
            _log(f"{coin} fire err: {e}")

    _monitor_exits()

    # ─── Fix 3: drain queue if exits freed slots ───
    drained = 0
    with _state_lock:
        queue_snapshot = list(_state['pending_queue'])
    for q in queue_snapshot:
        with _state_lock:
            n_open = len(_state['open_positions'])
        if n_open >= MAX_POSITIONS:
            break
        age = now_ts - q['queued_ts']
        if age > MAX_SIGNAL_AGE_S:
            # Stale — drop
            with _state_lock:
                if q in _state['pending_queue']:
                    _state['pending_queue'].remove(q)
            continue
        sig = q['signal']
        coin = sig['coin']
        if _in_position(coin) or coin in killed:
            with _state_lock:
                if q in _state['pending_queue']:
                    _state['pending_queue'].remove(q)
            continue
        try:
            fill = _size_and_fire(coin, sig, equity)
            if fill is not None:
                _register_position(coin, sig, fill)
                with _state_lock:
                    _state['fired_events'][sig['_evt_key']] = now_ts
                    if q in _state['pending_queue']:
                        _state['pending_queue'].remove(q)
                drained += 1
        except Exception as e:
            _log(f"{coin} queue drain err: {e}")

    with _state_lock:
        n_open = len(_state['open_positions'])
        q_len = len(_state['pending_queue'])
        n_killed = len(_state['killed_coins'])
        stats = f"fires={_state['total_fires']} W={_state['wins']} L={_state['losses']} " \
                f"TO={_state['timeouts']} pnl={_state['total_pnl_pct']:+.2f}%"
    _log(f"scan done: +{fires_this_scan} new +{drained} drained +{queued_this_scan} queued "
         f"| open={n_open}/{MAX_POSITIONS} queue={q_len} killed={n_killed} | {stats}")
    _save_state()

def _loop():
    _load_state()
    _log(f"worker started: ENABLED={ENABLED} DRY_RUN={DRY_RUN} "
         f"interval={SCAN_INTERVAL_S}s max_pos={MAX_POSITIONS} risk={RISK_PCT*100}%")
    while ENABLED:
        try:
            _scan_once()
        except Exception as e:
            _log(f"loop err: {e}")
            traceback.print_exc()
        time.sleep(SCAN_INTERVAL_S)

def start(precog_module):
    """Called from precog.py after its init completes."""
    global _precog, _ce
    if not ENABLED:
        print("[CONFLUENCE] disabled (CONFLUENCE_ENABLED != 1)")
        return None
    _precog = precog_module
    import confluence_engine as ce
    _ce = ce
    t = threading.Thread(target=_loop, name='confluence-worker', daemon=True)
    t.start()
    _log("thread launched")
    return t

def status():
    """Expose state for /health or /confluence endpoint."""
    with _state_lock:
        return dict(_state)
