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

# Trade ledger + edge gate (eager imports — modules don't depend on precog)
try:
    import trade_ledger as _ledger
    _LEDGER_OK = True
except Exception as _e:
    _ledger = None
    _LEDGER_OK = False
try:
    import gates as _gates
    _GATES_OK = True
except Exception:
    _gates = None
    _GATES_OK = False

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
MAX_SIGNAL_AGE_S = int(os.environ.get('CONFLUENCE_MAX_SIGNAL_AGE_S', '86400'))
# 2026-04-26: was hardcoded 15min, then 60min — both empirically inadequate.
# `latest_signal_ts` is the bar START of the most recent qualifying bar in
# the 24h CONF_WINDOW. EMA crosses with all 6 filters passing are rare, so
# the most recent qualifier is typically 1-12h old. With caps below 60min
# we got: 15min→32 stale 0 fires, 60min→29 stale 0 fires (regime shifted
# but same blocker pattern).
#
# Default 86400s = 24h, matches CONF_WINDOW_S — effectively defers to the
# engine's own window as the upper bound. The IOC slippage buffer (0.08%
# in _size_and_fire) is the natural price-staleness filter: if the market
# drifted past entry, the order won't fill. Bad fills aren't possible.
# Maximum signal flow with bounded execution risk.
#
# Tighten via CONFLUENCE_MAX_SIGNAL_AGE_S env if no-fill IOCs spike API
# usage. e.g. =14400 (4h), =3600 (60min, prior default).

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
    # 2026-04-26: per-gate reject counters — empirical "why 0 fires" diagnosis.
    # Cumulative since boot. `rejects_last_scan` resets each scan so we can
    # see the current scan's gate distribution in isolation.
    'rejects': {
        'killed': 0, 'cooldown': 0, 'in_position': 0, 'no_bars': 0,
        'no_signal': 0, 'stale': 0, 'dedupe': 0, 'entry_gate_v3': 0,
        'cap_queued': 0,
    },
    'rejects_last_scan': {
        'killed': 0, 'cooldown': 0, 'in_position': 0, 'no_bars': 0,
        'no_signal': 0, 'stale': 0, 'dedupe': 0, 'entry_gate_v3': 0,
        'cap_queued': 0,
    },
    'last_scan_at': 0,
    'last_scan_signals': 0,        # signals_yielded contributed by THIS scan
    'last_scan_fires': 0,
    # ─── TELEMETRY 2026-04-25: per-trade close log ───
    # Ring buffer of last N closed trades. Used for downstream eval:
    # which scores actually work, which coins are dead weight, exit-reason mix.
    'closed_trades': [],         # list of dicts (capped at 500), newest last
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
    """Use OKX as candle source (drop-in for HL info.candles_snapshot).
    Returns list of {t, o, h, l, c, v} with ONLY fully-closed bars aligned
    to 00/15/30/45.  Returns None on failure or if no new closed bar since
    last process.

    2026-04-25: migrated from HL info.candles_snapshot to okx_fetch.fetch_klines.
    HL was rate-limiting confluence scans on 60+ coins. OKX is unmetered for
    public candles. Same return shape, drop-in compatible.

    OKX caps at 300 bars per call. eval_coin needs 100+ — 300 is sufficient
    coverage (75h of 15m ≈ 3 days, ample for 24h CONF_WINDOW + HTF context).
    """
    try:
        import okx_fetch
        # OKX cap = 300; we still request up to n_bars but truncate
        bars_to_request = min(int(n_bars), 300)
        raw = okx_fetch.fetch_klines(coin, '15m', bars_to_request)
        if not raw:
            return None

        BAR_S = 15 * 60
        now_s = int(time.time())
        latest_closed_start = (now_s // BAR_S - 1) * BAR_S

        bars = []
        for b in raw:
            t = int(b['t'])
            t_s = t // 1000 if t > 10**12 else t
            if t_s % BAR_S != 0:
                continue
            if t_s > latest_closed_start:
                continue  # still-open bar — drop
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

    2026-04-25: respects FORCE_NOTIONAL_USD env override (debug mode).
    When set, all System B trades use fixed notional regardless of risk math.
    """
    sl_pct = signal['sl_pct']
    risk_usd = equity * RISK_PCT
    notional_usd = risk_usd / sl_pct
    # ─── DEBUG MODE: force fixed notional if env set ───
    try:
        _force = float(os.environ.get('FORCE_NOTIONAL_USD', '11'))
        if _force > 0:
            notional_usd = _force
    except Exception:
        pass
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

    # Pre-bind a trade identity so the ENTRY ledger row uses the same id
    # the close path will reference. Generated even if order fails (we
    # discard it then) — this avoids a race where the order fills before
    # the ledger row exists.
    _trade_id = (_ledger.new_trade_id()
                 if (_LEDGER_OK and _ledger) else None)

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
        # Record ENTRY in the unified ledger so /trades/recent and
        # analyze_trades see confluence trades alongside legacy precog ones.
        # tp_pct/sl_pct are known at signal time (no ENTRY_UPDATE needed).
        if _trade_id and _LEDGER_OK and _ledger and actual_px:
            try:
                _edge = (_gates.compute_expected_edge(signal['tp_pct'], signal['sl_pct'])
                         if _GATES_OK else None)
                _engine_tag = 'CONFLUENCE_' + '+'.join(signal.get('systems') or ['?'])
                _ledger.append_entry(
                    coin=coin, side=signal['side'], entry_price=actual_px,
                    engine=_engine_tag, source='confluence_signal',
                    sl_pct=signal['sl_pct'], tp_pct=signal['tp_pct'],
                    expected_edge_at_entry=_edge,
                    trade_id=_trade_id,
                )
                if isinstance(r, dict):
                    r['trade_id'] = _trade_id
            except Exception as _le:
                _log(f"[ledger] confluence append_entry err {coin}: {_le}")
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
    # ─── ALIGNMENT 2026-04-25 STEP 2: hard check at entry ───────
    # Even if a stray signal slips through earlier filters, refuse to
    # register if coin is not in current System A whitelist.
    try:
        import percoin_configs as _pc
        _wl = set(list(_pc.PURE_14.keys()) + list(_pc.NINETY_99.keys()) +
                  list(_pc.EIGHTY_89.keys()) + list(_pc.SEVENTY_79.keys()))
        if coin not in _wl:
            _log(f"REJECT_ENTRY {coin}: not in current ELITE whitelist — alignment guard")
            return
    except Exception as e:
        _log(f"alignment guard err {coin}: {e} — failing OPEN to allow entry")
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
            # Unified ledger trade_id stamped by _size_and_fire — used
            # by _close_position to write the matching CLOSE row.
            'trade_id': fill_result.get('trade_id') if isinstance(fill_result, dict) else None,
            # ─── STEP 3: tag every trade with source universe ───
            'universe': 'ELITE_71',
            'universe_aligned_ts': 1777076400,  # 2026-04-25 alignment anchor
            # ─── LIFECYCLE STATE MACHINE ───
            'state': 'OPEN',                 # OPEN → IN_PROFIT → LOCKED → CLOSED → TIMEOUT
            'max_favourable_px': signal['entry'],  # tracks high-water for trailing logic
            'sl_at_be': False,               # set True when BE shift fires
        }
        _state['total_fires'] += 1
        _state['last_fire_ts'][coin] = int(time.time())
    _save_state()

def _monitor_exits():
    """Walk open positions. LIFECYCLE ENGINE — every position must exit cleanly.
    
    Exit rules (in order of evaluation):
      1. SL hit                  → close('sl')
      2. TP hit                  → close('tp')
      3. Profit lock raw≥1.5%    → close('tp_lock')
      4. BE shift  raw≥0.8%      → SL → entry (sl_pct = 0)
      5. No-progress age≥2h, |raw|<0.3% → close('no_progress')
      6. Hard timeout age≥6h     → close('timeout')
    
    All raw_move = (mark - entry) / entry × direction. Tier-agnostic.
    Replaces old passive 72h max_hold model.
    """
    HARD_TIMEOUT_S = 6 * 3600       # 6h non-negotiable
    NO_PROGRESS_AGE_S = 2 * 3600    # 2h then check progress
    NO_PROGRESS_THRESHOLD = 0.003   # |raw| < 0.3% = stuck
    PROFIT_LOCK_PCT = 0.015          # raw ≥ 1.5% → close
    PROFIT_LOCK_BE_PCT = 0.008       # raw ≥ 0.8% → move SL to entry

    now = int(time.time())
    with _state_lock:
        to_check = list(_state['open_positions'].items())
    
    # Single shared mid-price fetch (rate-limit safety)
    try:
        mids = _precog.info.all_mids() if hasattr(_precog.info, 'all_mids') else {}
    except Exception as e:
        _log(f"mids fetch err: {e}")
        return
    
    for coin, pos in to_check:
        try:
            age = now - pos['ts']
            entry = pos['entry']
            is_buy = pos['side'] == 'BUY'
            px = float(mids.get(coin, 0))
            if not px:
                # No price — only age-based exits possible
                if age >= HARD_TIMEOUT_S:
                    _log(f"{coin} TIMEOUT {age/3600:.1f}h (no_price) — flat")
                    _close_position(coin, 'timeout')
                continue
            raw_move = ((px - entry) / entry) if is_buy else ((entry - px) / entry)

            # 1+2: traditional SL/TP
            if raw_move <= -pos['sl_pct']:
                _log(f"{coin} SL hit pnl={raw_move*100:.2f}% age={age/60:.0f}m — flat")
                _close_position(coin, 'sl', raw_move)
                continue
            if raw_move >= pos['tp_pct']:
                _log(f"{coin} TP hit pnl={raw_move*100:.2f}% age={age/60:.0f}m — flat")
                _close_position(coin, 'tp', raw_move)
                continue

            # 3: profit lock (close at +1.5% raw)
            if raw_move >= PROFIT_LOCK_PCT:
                _log(f"{coin} PROFIT_LOCK pnl={raw_move*100:.2f}% age={age/60:.0f}m — flat")
                _close_position(coin, 'tp_lock', raw_move)
                continue

            # 4: BE shift (move SL to entry once raw ≥ 0.8%)
            if raw_move >= PROFIT_LOCK_BE_PCT and not pos.get('sl_at_be'):
                with _state_lock:
                    if coin in _state['open_positions']:
                        _state['open_positions'][coin]['sl_at_be'] = True
                        _state['open_positions'][coin]['sl_pct'] = 0.0
                _log(f"{coin} BE_SHIFT raw={raw_move*100:.2f}% — SL moved to entry")
                # Don't continue — let next tick evaluate against new SL=0

            # 5: no-progress kill (age ≥ 2h, |raw| < 0.3%)
            if age >= NO_PROGRESS_AGE_S and abs(raw_move) < NO_PROGRESS_THRESHOLD:
                _log(f"{coin} NO_PROGRESS {age/3600:.1f}h raw={raw_move*100:+.2f}% — flat")
                _close_position(coin, 'no_progress', raw_move)
                continue

            # 6: hard timeout (age ≥ 6h)
            if age >= HARD_TIMEOUT_S:
                _log(f"{coin} TIMEOUT {age/3600:.1f}h raw={raw_move*100:+.2f}% — flat")
                _close_position(coin, 'timeout', raw_move)
                continue

        except Exception as e:
            _log(f"{coin} lifecycle err: {e}")


def _close_position(coin, reason, pnl=None):
    with _state_lock:
        pos = _state['open_positions'].pop(coin, None)
    if not pos:
        return
    _exit_px_for_ledger = None
    if not DRY_RUN:
        try:
            # precog has a close helper? Fall back to reduce-only market
            if hasattr(_precog, 'close_position'):
                _precog.close_position(coin, reason)
            else:
                mids = _precog.info.all_mids()
                px = float(mids.get(coin, 0))
                _exit_px_for_ledger = px or None
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

    # Unified ledger CLOSE — pairs with the ENTRY written by _size_and_fire.
    # confluence's `pnl` arg is a signed fraction (e.g. +0.0032 = +0.32%);
    # passed through unchanged to match the existing /trades/recent shape.
    _tid = pos.get('trade_id')
    if _tid and _LEDGER_OK and _ledger and not DRY_RUN:
        try:
            _ledger.append_close(
                trade_id=_tid,
                exit_price=_exit_px_for_ledger,
                pnl=(pnl if pnl is not None else 0.0),
                close_reason=reason,
                source='confluence_close',
            )
        except Exception as _le:
            _log(f"[ledger] confluence append_close err {coin}: {_le}")
    if pnl is not None:
        with _state_lock:
            if pnl > 0: _state['wins'] += 1
            else: _state['losses'] += 1
            _state['total_pnl_pct'] += pnl * 100
    elif reason == 'timeout':
        with _state_lock:
            _state['timeouts'] += 1

    # ─── TELEMETRY 2026-04-25: per-trade close log ───
    # Schema (per spec): coin, side, confluence_score (n_sys), exit_reason,
    # duration_min, pnl_pct, plus universe tag for audit and entry/exit prices.
    try:
        now = int(time.time())
        duration_s = now - pos.get('ts', now)
        trade_record = {
            'coin': coin,
            'side': pos.get('side'),
            'confluence_score': pos.get('n_sys'),       # 2 or 3 systems agreed
            'systems': pos.get('systems', []),
            'entry': pos.get('entry'),
            'exit_reason': reason,
            'duration_min': round(duration_s / 60.0, 1),
            'pnl_pct': round((pnl * 100) if pnl is not None else 0.0, 3),
            'opened_ts': pos.get('ts'),
            'closed_ts': now,
            'universe': pos.get('universe', 'UNKNOWN'),
            'sl_at_be': pos.get('sl_at_be', False),
        }
        with _state_lock:
            _state['closed_trades'].append(trade_record)
            # Cap at 500 — drop oldest
            if len(_state['closed_trades']) > 500:
                _state['closed_trades'] = _state['closed_trades'][-500:]
    except Exception as e:
        _log(f"{coin} telemetry record err: {e}")

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

    # ─── ALIGNMENT 2026-04-25: bind to System A universe ───────────
    # Was: 99 hardcoded OOS-validated coins from earlier sweep.
    # Now: imported live from percoin_configs — same source of truth as System A.
    # Updates to whitelist auto-propagate. No drift, no silent mismatches.
    try:
        import percoin_configs as _pc
        CONFLUENCE_UNIVERSE = sorted(set(
            list(_pc.PURE_14.keys()) +
            list(_pc.NINETY_99.keys()) +
            list(_pc.EIGHTY_89.keys()) +
            list(_pc.SEVENTY_79.keys())
        ))
    except Exception as e:
        _log(f"universe import err, falling back to empty: {e}")
        CONFLUENCE_UNIVERSE = []
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

    # Reset per-scan reject counters; cumulative ones in _state['rejects'] keep going.
    _scan_rejects = {k: 0 for k in _state['rejects_last_scan']}
    _scan_signals = 0

    with _state_lock:
        killed = set(_state['killed_coins'].keys())
        fired_events = dict(_state['fired_events'])

    # Purge old dedupe entries
    for k, fire_ts in list(fired_events.items()):
        if now_ts - fire_ts > DEDUPE_WINDOW_S:
            with _state_lock:
                _state['fired_events'].pop(k, None)

    def _bump(reason):
        _scan_rejects[reason] += 1
        with _state_lock:
            _state['rejects'][reason] = _state['rejects'].get(reason, 0) + 1

    for coin in coins:
        try:
            # Fix 4/5 kill filter
            if coin in killed:
                _bump('killed'); continue
            # Cooldown
            if not _ce.should_enter(coin, _state['last_fire_ts'], now_ts):
                _bump('cooldown'); continue
            # Already in position?
            if _in_position(coin):
                _bump('in_position'); continue
            # Fetch + evaluate (Fix 1: only fully-closed bars)
            bars = _fetch_15m_bars(coin, 800)
            if not bars:
                _bump('no_bars'); continue
            sig = _ce.eval_coin(coin, bars, now_ts=now_ts)
            # Mark bar as processed even if no signal
            with _state_lock:
                _state['last_bar_ts'][coin] = bars[-1]['t']
            if not sig:
                _bump('no_signal'); continue
            _scan_signals += 1

            # ─── Fix B: entry drift control ───
            latest_sig_ts = sig.get('latest_signal_ts') or sig.get('ts')
            sig_age = now_ts - latest_sig_ts
            if sig_age > MAX_SIGNAL_AGE_S:
                _log(f"{coin} {sig['side']} stale ({sig_age}s > {MAX_SIGNAL_AGE_S}s) — skip")
                _bump('stale'); continue

            # ─── Fix 2: confluence event dedupe ───
            # Use first signal ts across the agreeing systems as the event anchor
            first_ts = sig.get('latest_signal_ts') or sig.get('ts')
            # Bucket to confluence window so minor drift doesn't break dedupe
            ts_bucket = (first_ts // DEDUPE_WINDOW_S) * DEDUPE_WINDOW_S
            evt_key = f"{coin}|{sig['side']}|{ts_bucket}"
            if evt_key in fired_events:
                _bump('dedupe'); continue  # already fired this event

            # Entry gate
            ok, why = _entry_gate_ok(coin, sig['side'])
            if not ok:
                _log(f"{coin} {sig['side']} n={sig['n_sys']} — gated by {why}")
                _bump('entry_gate_v3'); continue

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
                _bump('cap_queued')
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

    # Snapshot per-scan counters into state so /confluence can show
    # the most recent scan's gate distribution in isolation.
    with _state_lock:
        _state['rejects_last_scan'] = _scan_rejects
        _state['last_scan_at'] = now_ts
        _state['last_scan_signals'] = _scan_signals
        _state['last_scan_fires'] = fires_this_scan

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


def _lifecycle_loop():
    """Lifecycle ticker — runs every 30s independently of signal scan.
    Enforces timeouts, profit lock, BE shift, no-progress kill, SL/TP.
    Decouples exit cadence from signal cadence (was 5min, way too slow)."""
    LIFECYCLE_INTERVAL_S = 30
    _log(f"lifecycle ticker started: interval={LIFECYCLE_INTERVAL_S}s")
    while ENABLED:
        try:
            with _state_lock:
                n_open = len(_state['open_positions'])
            if n_open > 0:
                _monitor_exits()
        except Exception as e:
            _log(f"lifecycle loop err: {e}")
            traceback.print_exc()
        time.sleep(LIFECYCLE_INTERVAL_S)


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
    # Separate lifecycle ticker — runs every 30s
    t2 = threading.Thread(target=_lifecycle_loop, name='confluence-lifecycle', daemon=True)
    t2.start()
    _log("thread launched (signal scan + lifecycle ticker)")
    return t

def status():
    """Expose state for /health or /confluence endpoint."""
    with _state_lock:
        out = dict(_state)
    # Surface per-filter rejection counters from the engine — surgical
    # diagnosis for "why 0 fires"
    try:
        import confluence_engine as ce
        if hasattr(ce, 'status'):
            out['engine_stats'] = ce.status()
    except Exception as e:
        out['engine_stats_err'] = f"{type(e).__name__}: {e}"
    return out


def reset(preserve_history=False):
    """Reset System B state. Used after universe alignment to clear positions
    that were opened under the old (drift-prone) universe.
    
    If preserve_history=True, keeps total_fires/wins/losses/pnl as 'pre_alignment'
    snapshot fields (for audit), zeros active counters.
    """
    with _state_lock:
        if preserve_history:
            _state['pre_alignment_snapshot'] = {
                'total_fires': _state['total_fires'],
                'wins': _state['wins'],
                'losses': _state['losses'],
                'timeouts': _state['timeouts'],
                'total_pnl_pct': _state['total_pnl_pct'],
                'snapshot_ts': int(time.time()),
                'snapshot_reason': 'universe_alignment_2026_04_25',
            }
        # Clear active state
        _state['open_positions'] = {}
        _state['total_fires'] = 0
        _state['wins'] = 0
        _state['losses'] = 0
        _state['timeouts'] = 0
        _state['total_pnl_pct'] = 0.0
        _state['fired_events'] = {}
        _state['pending_queue'] = []
        _state['last_fire_ts'] = {}
        # last_bar_ts intentionally preserved — avoids re-firing same bars
    _save_state()
    _log("STATE RESET — universe alignment complete, fresh sample begins now")
    return True
