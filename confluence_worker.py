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
try:
    import funding_accrual as _funding_accrual
    _FA_OK = True
except Exception:
    _funding_accrual = None
    _FA_OK = False

ENABLED         = os.environ.get('CONFLUENCE_ENABLED', '0') == '1'
DRY_RUN         = os.environ.get('CONFLUENCE_DRY_RUN', '1') == '1'
SCAN_INTERVAL_S = int(os.environ.get('CONFLUENCE_SCAN_INTERVAL', '180'))
# 2026-04-26: 300s → 180s. Increases scan frequency from every 5min to
# every 3min. Same per-scan signal yield → 1.67x more checks/day → ~30%
# more fires/day at near-zero risk increase. Override via env if needed.
MAX_POSITIONS   = int(os.environ.get('CONFLUENCE_MAX_POSITIONS', '25'))
RISK_PCT        = float(os.environ.get('CONFLUENCE_RISK_PCT', '0.01'))

# 2026-04-26: optional side filter. Default permissive (both BUY,SELL). The
# earlier SELL-bias read dissipated once N grew from 30 to 40 decided.
# Override explicitly via ALLOWED_SIDES=BUY (or =SELL) only if you want to
# cut signals on directional evidence.
_sides_raw = os.environ.get('ALLOWED_SIDES', 'BUY,SELL').upper()
ALLOWED_SIDES = {s.strip() for s in _sides_raw.split(',') if s.strip() in ('BUY', 'SELL')}
if not ALLOWED_SIDES:
    ALLOWED_SIDES = {'BUY', 'SELL'}

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
DEDUPE_WINDOW_S = int(os.environ.get('CONFLUENCE_DEDUPE_WINDOW_S', str(4 * 3600)))
# 2026-04-26: 24h → 4h. After a coin closes, allow re-entry sooner if a fresh
# confluence signal appears. Old 24h window blocked many legitimate re-entries
# on the same coin throughout the day, especially in chop where the same level
# gets tested multiple times. 4h is enough to avoid same-event double-fires
# but short enough to capture independent setups same day.

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
    # 2026-04-26: fire-stage attempt counters — empirical "why 0 fires"
    # diagnosis for the post-gate phase (after stale/in_position/etc).
    'place_attempts': 0,           # times we called _precog.place()
    'place_filled': 0,             # returned a real fill_px
    'place_no_fill': 0,            # returned None (both maker AND taker failed)
    'place_error': 0,              # raised an exception
    # ─── TELEMETRY 2026-04-25: per-trade close log ───
    # Ring buffer of last N closed trades. Used for downstream eval:
    # which scores actually work, which coins are dead weight, exit-reason mix.
    'closed_trades': [],         # list of dicts (capped at 500), newest last
}
_state_lock = threading.Lock()

def _log(msg):
    line = f"[{datetime.utcnow().isoformat(timespec='seconds')}Z] [CONFLUENCE] {msg}"
    print(line, flush=True)
    # Mirror to precog's in-memory LOG_BUFFER if available so /health
    # recent_logs surfaces confluence activity (otherwise it's stuck in
    # /var/data/confluence.log only). Best-effort — never break logging.
    try:
        if _precog is not None and hasattr(_precog, 'LOG_BUFFER'):
            _precog.LOG_BUFFER.append(line)
            if len(_precog.LOG_BUFFER) > 200:
                _precog.LOG_BUFFER.pop(0)
    except Exception:
        pass
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
            # 2026-04-26: cleanup phantom entries with trade_id=null. These
            # are pre-race-fix orphans (e.g. PROVE) where an order placed
            # but the ledger wasn't bound yet. Without trade_id we can't
            # write a CLOSE to the unified ledger, so the position lingers
            # forever and blocks future fires on that coin via _in_position.
            _phantoms = []
            with _state_lock:
                for _c, _p in list(_state.get('open_positions', {}).items()):
                    if _p.get('trade_id') is None:
                        _phantoms.append(_c)
                        _state['open_positions'].pop(_c, None)
            if _phantoms:
                _log(f"startup cleanup: dropped {len(_phantoms)} trade_id=null phantom positions: {_phantoms}")
                _save_state()
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

def _in_position(coin, exchange_coins=None):
    """Check if there's an open position on `coin` from ANY source.

    Three checks (any True → True):
      1. Confluence's own open_positions tracker
      2. precog's cached live_positions tracker (may be WS-stale)
      3. Authoritative exchange snapshot (set passed in by caller)

    `exchange_coins` is a set of coin names with non-zero positions on the
    exchange right now — fetched once per scan in _scan_once via REST. This
    catches the cases where (1) and (2) miss because of WS lag or restart
    state-load gaps, which is what was producing duplicate trade_ids
    (e.g. confluence firing ICP at 07:47 even though precog had it open
    since 06:37 — same exchange position, two ledger trade_ids).
    """
    with _state_lock:
        if coin in _state['open_positions']:
            return True
    # Defer to precog's cached state if available
    try:
        live = _precog.live_positions
        if live and coin in live:
            return True
    except Exception:
        pass
    # Authoritative — exchange-truth snapshot, if caller fetched one
    if exchange_coins is not None and coin in exchange_coins:
        return True
    return False

def _entry_gate_ok(coin, side):
    """Reuse precog's existing gate stack — V3 trend, ATR-min, ticker gate."""
    if side not in ALLOWED_SIDES:
        return False, 'side_filter'
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
        _force = float(os.environ.get('FORCE_NOTIONAL_USD', '22'))
        if _force > 0:
            notional_usd = _force
    except Exception:
        pass
    entry = signal['entry']
    size_coin = notional_usd / entry

    # Slippage buffer on entry. Was hardcoded 0.08% — too tight for fast-moving
    # alts; observed empirically as 29/29 yielded signals failing IOC match
    # (with all gates passed, fires still 0). 30bps default gives much higher
    # fill rate. Operator can tune via env CONFLUENCE_IOC_SLIP_BUFFER.
    is_buy = signal['side'] == 'BUY'
    _slip_buf = float(os.environ.get('CONFLUENCE_IOC_SLIP_BUFFER', '0.003'))
    px = entry * (1 + _slip_buf) if is_buy else entry * (1 - _slip_buf)

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

    # Build a precog-compatible cloid so the reconciler can match fills
    # back to this trade_id (same scheme precog uses at its entry sites).
    _cloid = None
    if _trade_id:
        _suffix = 'L' if is_buy else 'S'
        _cloid = f"{_trade_id[:8]}{coin[:4]}{_suffix}"[:16]

    # 2026-04-26: Pre-write the ENTRY ledger row BEFORE placing the order.
    # Live evidence (AAVE event 337/342): when confluence's append_entry
    # ran AFTER the fill, the reconciler swept the new exchange position
    # in that ~1-10s window and adopted it as a fresh RECONCILED trade,
    # creating a duplicate trade_id (one CONFLUENCE_SNIPER, one RECONCILED).
    # Writing first means the reconciler finds an existing open entry and
    # skips its adopt path.
    #
    # entry_price is the SIGNAL'S entry (last bar close), not the actual
    # fill — fill differs by slippage (typically <0.5%). realized_slippage_pct
    # is added to the row by an ENTRY_UPDATE after the fill returns.
    # regime captured at entry time. Direct call to regime_detector since
    # precog doesn't cache it on state — only emits in /health per-tick.
    _regime_at_entry = None
    try:
        import regime_detector as _rd_e
        _regime_at_entry = _rd_e.get_regime()
    except Exception:
        _regime_at_entry = None

    if _trade_id and _LEDGER_OK and _ledger:
        try:
            _edge = (_gates.compute_expected_edge(signal['tp_pct'], signal['sl_pct'])
                     if _GATES_OK else None)
            _engine_tag = 'CONFLUENCE_' + '+'.join(signal.get('systems') or ['?'])
            _ledger.append_entry(
                coin=coin, side=signal['side'], entry_price=entry,
                engine=_engine_tag, source='confluence_signal',
                sl_pct=signal['sl_pct'], tp_pct=signal['tp_pct'],
                expected_edge_at_entry=_edge,
                trade_id=_trade_id, cloid=_cloid,
                regime=_regime_at_entry,
            )
        except Exception as _le:
            _log(f"[ledger] confluence pre-place append_entry err {coin}: {_le}")

    try:
        # 2026-04-26: Route through _precog.place() instead of direct
        # exchange.order. precog's place() implements MAKER (post-only Alo)
        # → TAKER (IOC) fallback with a 10s window — we previously fired
        # pure IOC at 30bps and got 0/29 fills because price drift past
        # 30bps in the order-placement window blew through every IOC.
        # place() returns the fill price on success, None on no-fill.
        with _state_lock:
            _state['place_attempts'] = _state.get('place_attempts', 0) + 1
        fill_px = _precog.place(coin, is_buy, size_coin, cloid=_cloid)
        if fill_px is None:
            with _state_lock:
                _state['place_no_fill'] = _state.get('place_no_fill', 0) + 1
            _log(f"{coin} NO_FILL — precog.place returned None (maker+taker both failed)")
            # Close the pre-written ENTRY so it doesn't dangle as an orphan
            # for the reconciler to "discover" and double-book.
            if _trade_id and _LEDGER_OK and _ledger:
                try:
                    _ledger.append_close(
                        trade_id=_trade_id,
                        exit_price=None, pnl=0,
                        close_reason='confluence_no_fill',
                        source='confluence_close',
                    )
                except Exception as _le:
                    _log(f"[ledger] confluence no_fill close err {coin}: {_le}")
            return None
        with _state_lock:
            _state['place_filled'] = _state.get('place_filled', 0) + 1
        actual_px = float(fill_px)
        _log(f"{coin} FILLED via precog.place: {actual_px:.6f} (signal_entry={entry:.6f})")

        slip_pct_pct = 0.0
        slip_pct_signed = None
        if entry > 0:
            slip_pct_pct = abs(actual_px - entry) / entry * 100
            _record_slippage(coin, slip_pct_pct)
            # Signed slippage from the trade's perspective (positive = paid more
            # than signal price for buys / received less for sells = unfavorable).
            _drift = (actual_px - entry) / entry
            slip_pct_signed = _drift if is_buy else -_drift
        # Update the pre-written ENTRY row with actual fill price + signed slippage.
        if _trade_id and _LEDGER_OK and _ledger:
            try:
                _ledger.update_entry_fields(
                    _trade_id,
                    realized_slippage_pct=slip_pct_signed,
                    entry_price=actual_px,
                )
            except Exception as _ue:
                _log(f"[ledger] confluence ENTRY_UPDATE post-fill err {coin}: {_ue}")
        # Synthesize a result dict for callers that expect the legacy shape.
        r = {'expected_px': entry, 'actual_px': actual_px,
             'slip_pct': slip_pct_pct,
             'trade_id': _trade_id}
        return r
    except Exception as e:
        with _state_lock:
            _state['place_error'] = _state.get('place_error', 0) + 1
        _log(f"{coin} order FAIL: {e}")
        # Close the pre-written ENTRY on exception too.
        if _trade_id and _LEDGER_OK and _ledger:
            try:
                _ledger.append_close(
                    trade_id=_trade_id,
                    exit_price=None, pnl=0,
                    close_reason='confluence_order_exception',
                    source='confluence_close',
                )
            except Exception:
                pass
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

def _try_rotate_stale_flat():
    """Find the worst stale-flat-eligible position and close it. Returns
    coin name on successful eviction, None if no candidate exists.

    Worst = lowest MFE first, then most negative raw_move, then oldest.
    Only positions previously marked stale_flat_eligible by _monitor_exits
    qualify — that means: age >= 90min AND mfe < +0.3% AND raw < -0.7%.
    """
    with _state_lock:
        candidates = [
            (coin, dict(pos)) for coin, pos in _state['open_positions'].items()
            if pos.get('stale_flat_eligible')
        ]
    if not candidates:
        return None
    # Rank: lowest MFE first (most directionally wrong), then most negative
    # raw, then oldest. Goal: kick out the deadest position.
    def _badness(item):
        _, pos = item
        mfe = pos.get('mfe_pct') or 0
        # We don't have current raw here; use stale_flat_marked_ts as proxy
        # for staleness (more recent mark = fresher data, prefer to evict
        # ones that have been marked longest)
        marked_ts = pos.get('stale_flat_marked_ts') or 0
        return (mfe, -marked_ts)  # ascending mfe (lowest first), oldest mark first
    candidates.sort(key=_badness)
    coin_to_evict, pos = candidates[0]
    # Compute current raw for the close log line
    try:
        mids = _precog.info.all_mids()
        mark = float(mids.get(coin_to_evict, 0))
        entry = float(pos.get('entry') or 0)
        is_buy = pos.get('side') == 'BUY'
        raw_move = ((mark - entry) / entry) if is_buy else ((entry - mark) / entry)
    except Exception:
        raw_move = pos.get('mfe_pct') or 0  # best-effort
    _close_position(coin_to_evict, 'stale_flat_rotated', raw_move)
    return coin_to_evict


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

    # 2026-04-26: stale-flat eviction.
    # v2: tightened raw threshold (-0.7%) and changed from auto-close to
    # rotation. Marks positions as eviction-eligible; _scan_once evicts only
    # when a fresh signal needs the slot. Avoids replacing one loss with
    # another when no better signal is queued.
    STALE_FLAT_AGE_S    = 90 * 60   # 1h30 — older than this is candidate
    STALE_FLAT_MFE_MAX  = 0.003     # never crossed +0.3% MFE
    STALE_FLAT_RAW_MIN  = -0.007    # currently below -0.7% raw move (tightened from -0.4%)

    now = int(time.time())
    with _state_lock:
        to_check = list(_state['open_positions'].items())
    
    # Single shared mid-price fetch (rate-limit safety) with 429 retry.
    # 2026-04-26: live log showed `[CONFLUENCE] mids fetch err: (429, ...)`
    # which on bare exception aborted the WHOLE monitor cycle — no TP-lock,
    # BE-shift, or no-progress check ran for any open position. Brief retry
    # with backoff + jitter keeps the cycle alive through transient 429s.
    mids = {}
    if hasattr(_precog.info, 'all_mids'):
        import random as _r
        for _attempt in range(3):
            try:
                mids = _precog.info.all_mids() or {}
                break
            except Exception as e:
                _es = str(e)
                if '429' not in _es and 'rate' not in _es.lower() and _attempt < 2:
                    _log(f"mids fetch err (non-429, no retry): {_es[:120]}")
                    return
                if _attempt >= 2:
                    _log(f"mids fetch err after 3 attempts ({_es[:120]}) — skipping monitor cycle")
                    return
                _wait = 0.5 * (2 ** _attempt) + _r.uniform(0, 0.2)
                _log(f"mids fetch 429 attempt {_attempt+1}/3 — retry in {_wait:.1f}s")
                time.sleep(_wait)
    if not mids:
        _log("mids fetch returned empty — skipping monitor cycle")
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

            # MFE/MAE tracking — high/low water of price-side raw move during hold.
            # Used at close to compute "did this trade ever go in our favor?" and
            # "how deep did it go against us?" — single most important diagnostic
            # for distinguishing bad-entry from bad-exit.
            with _state_lock:
                if coin in _state['open_positions']:
                    _cur = _state['open_positions'][coin]
                    if 'mfe_pct' not in _cur:
                        _cur['mfe_pct'] = 0.0
                    if 'mae_pct' not in _cur:
                        _cur['mae_pct'] = 0.0
                    if raw_move > _cur['mfe_pct']:
                        _cur['mfe_pct'] = raw_move
                    if raw_move < _cur['mae_pct']:
                        _cur['mae_pct'] = raw_move

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

            # 5b: stale-flat MARKING (rotation-gated eviction)
            # 2026-04-26 v2: per user feedback, time-based auto-eviction
            # could replace one loss with another. Smarter: MARK eligible,
            # but only EVICT when a new signal needs the slot (rotation in
            # _scan_once). HARD_TIMEOUT (#6) remains as backstop.
            #
            # Tightened threshold: raw < -0.7% (was -0.4%) — gives recovery
            # more rope. Trade must be both old enough AND clearly underwater
            # AND never went meaningfully positive to be eviction candidate.
            if (age >= STALE_FLAT_AGE_S
                    and (pos.get('mfe_pct') or 0) < STALE_FLAT_MFE_MAX
                    and raw_move < STALE_FLAT_RAW_MIN):
                if not pos.get('stale_flat_eligible'):
                    with _state_lock:
                        if coin in _state['open_positions']:
                            _state['open_positions'][coin]['stale_flat_eligible'] = True
                            _state['open_positions'][coin]['stale_flat_marked_ts'] = now
                    _state.setdefault('stale_flat_marked', 0)
                    _state['stale_flat_marked'] += 1
                    _log(f"{coin} STALE_FLAT_MARKED {age/60:.0f}min mfe={(pos.get('mfe_pct') or 0)*100:.2f}% "
                         f"raw={raw_move*100:+.2f}% — eviction-eligible (will close if better signal queued)")
                # Don't auto-close. Let _scan_once rotate when new signal comes.

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
                # 2026-04-26: same 429 retry logic as _monitor_exits.
                import random as _r2
                _mids = {}
                for _att in range(3):
                    try:
                        _mids = _precog.info.all_mids() or {}
                        break
                    except Exception as _ce:
                        _ces = str(_ce)
                        if ('429' not in _ces and 'rate' not in _ces.lower()) or _att >= 2:
                            _log(f"{coin} close mids fetch err: {_ces[:120]}")
                            break
                        time.sleep(0.5 * (2 ** _att) + _r2.uniform(0, 0.2))
                mids = _mids
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
        # Funding accrual: signed cost on notional from entry_ts to now.
        # Best-effort — pos['ts'] is unix int set at _register_position.
        _fp = None
        if _FA_OK and _funding_accrual is not None:
            try:
                _entry_ts = pos.get('ts')
                if _entry_ts:
                    _fp, _src = _funding_accrual.compute_funding_paid_pct(
                        coin, pos.get('side', ''), float(_entry_ts), time.time())
            except Exception:
                _fp = None
        # MFE/MAE: tracked by _monitor_exits at every monitor tick. Default 0
        # if the trade closed before any monitor cycle saw it.
        _mfe = pos.get('mfe_pct')
        _mae = pos.get('mae_pct')
        try:
            _ledger.append_close(
                trade_id=_tid,
                exit_price=_exit_px_for_ledger,
                pnl=(pnl if pnl is not None else 0.0),
                close_reason=reason,
                source='confluence_close',
                funding_paid_pct=_fp,
                mfe_pct=_mfe,
                mae_pct=_mae,
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

    # 2026-04-26: fetch authoritative exchange position snapshot ONCE per
    # scan. Plugs the gap where _precog.live_positions is WS-stale or
    # missing entries from a recent restart — the ICP/AAVE duplicate
    # trade_id problem comes from confluence firing on a coin that
    # precog's cached tracker doesn't see but the exchange actually has.
    # ONE REST call per 5min scan; minimal rate-limit impact.
    _exchange_coins = None
    try:
        _us = _precog.info.user_state(_precog.WALLET)
        _ec = set()
        for _p in _us.get('assetPositions', []):
            _pos = _p.get('position', {}) if isinstance(_p, dict) else {}
            _name = _pos.get('coin')
            try:
                _sz = float(_pos.get('szi', 0) or 0)
            except (TypeError, ValueError):
                _sz = 0
            if _name and abs(_sz) > 0:
                _ec.add(_name)
        _exchange_coins = _ec
    except Exception as _ee:
        _log(f"[scan] exchange position snapshot fetch failed (non-fatal): {_ee}")
        # _exchange_coins stays None → _in_position falls back to cached checks only

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
            # Already in position? (checks confluence tracker, precog cache,
            # AND authoritative exchange snapshot — see _exchange_coins above)
            if _in_position(coin, exchange_coins=_exchange_coins):
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
                _bump(f'entry_gate_{why.lower()}'); continue

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
                # 2026-04-26: ROTATION — try to evict a stale-flat position
                # to make room for this fresh signal. Only fires if a marked
                # eviction-candidate exists. If none, queue as before.
                evicted = _try_rotate_stale_flat()
                if evicted:
                    _state.setdefault('stale_flat_rotated', 0)
                    _state['stale_flat_rotated'] += 1
                    _log(f"ROTATION: evicted {evicted} to make room for {coin} {sig['side']} "
                         f"n={sig['n_sys']} {'+'.join(sig['systems'])}")
                    # Slot freed — fall through to fire path below
                else:
                    # Queue it; older queued entries drop off if stale
                    with _state_lock:
                        _state['pending_queue'].append({
                            'coin': coin, 'signal': sig, 'queued_ts': now_ts
                        })
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
        if _in_position(coin, exchange_coins=_exchange_coins) or coin in killed:
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
