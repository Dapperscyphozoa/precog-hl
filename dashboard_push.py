"""
dashboard_push.py — drop-in helper to push engine state to the dashboard.

Usage in any engine service:

    from dashboard_push import push_state

    # at the end of save_state(state):
    push_state(
        engine_name      = 'lsr',                 # one of: multi-gate, smc-v1, smc-v2, smc-loose, lsr
        live             = LIVE_TRADING,
        sizing_mode      = SIZING_MODE,
        notional_usd     = FIXED_NOTIONAL_USD,
        max_concurrent   = MAX_CONCURRENT,
        positions_dict   = state['positions'],    # the 'positions' field
        history_list     = state.get('history', []),
        scan_count       = state.get('scan_count', 0),
        last_scan_ts     = state.get('last_scan_ts', 0),
    )

Notes:
  - Network failures are SWALLOWED — never raise back to the engine.
  - Push runs in a daemon thread so the engine save_state() returns immediately.
  - 12h-window stats are derived from the history list provided.
"""
import os, json, time, threading, traceback, urllib.request, urllib.error

DASH_URL    = os.environ.get('DASH_URL', '').rstrip('/')
DASH_SECRET = os.environ.get('DASH_PUSH_SECRET', 'change-me')
PUSH_TIMEOUT_SEC = 3
WINDOW_MS   = 12 * 3600 * 1000


def _compute_stats_12h(history_list):
    if not history_list:
        return {'wins': 0, 'losses': 0, 'breakevens': 0, 'pnl_total': 0.0,
                'avg_win': 0.0, 'avg_loss': 0.0, 'wr': None, 'rr_blended': None}
    cutoff = int(time.time()*1000) - WINDOW_MS
    recent = []
    for h in history_list:
        # tolerate multiple key conventions used across engines
        ts = h.get('close_t') or h.get('exit_t') or h.get('closed_t') or h.get('ts') or 0
        if ts and ts >= cutoff:
            recent.append(h)
    if not recent:
        return {'wins': 0, 'losses': 0, 'breakevens': 0, 'pnl_total': 0.0,
                'avg_win': 0.0, 'avg_loss': 0.0, 'wr': None, 'rr_blended': None}
    wins=[]; losses=[]; bes=[]; pnl_total=0.0
    for h in recent:
        # PNL fallback chain: explicit fields first, then derive from price+size
        pnl = float(h.get('realized_pnl') or h.get('pnl_usd') or h.get('pnl') or 0)
        if pnl == 0:
            entry    = float(h.get('entry') or h.get('entry_px') or 0)
            close_px = float(h.get('close_px') or h.get('exit_px') or h.get('exit') or 0)
            sz       = float(h.get('sz_total') or h.get('sz') or h.get('size') or 0)
            is_long  = h.get('is_long')
            if is_long is None:
                side = (h.get('side') or '').upper()
                is_long = side == 'LONG'
            if entry > 0 and close_px > 0 and sz > 0:
                sign = 1 if is_long else -1
                pnl = round(sign * (close_px - entry) * sz, 4)

        # OUTCOME fallback chain: explicit field, then close_reason, then sign of pnl
        outcome = (h.get('outcome') or '').upper()
        if not outcome:
            cr = (h.get('close_reason') or '').lower()
            if cr.startswith('tp2'):                               outcome = 'TP2'
            elif cr == 'tp1':                                       outcome = 'TP1'
            elif cr == 'be_stop':                                   outcome = 'BE'
            elif cr == 'sl':                                        outcome = 'SL'
            elif any(x in cr for x in ('time','pending','zombie')): outcome = 'TIMEOUT'
            elif cr:                                                outcome = cr.upper()

        pnl_total += pnl
        # Classification rule (no breakeven category):
        #   - TP1/TP2 hits = WIN
        #   - BE stop = WIN (TP1 already paid 50%, runner exited at entry — net positive)
        #   - SL = LOSS
        #   - TIMEOUT / PHANTOM_LIVE / pnl≈0 = NOT A TRADE (entry never filled or reconciled away)
        #     These are excluded from W/L counts entirely.
        if outcome.startswith('TP') or outcome == 'BE':
            wins.append(pnl)
        elif outcome.startswith('SL'):
            losses.append(pnl)
        elif outcome in ('TIMEOUT', 'PHANTOM_LIVE'):
            pass  # not a real trade — exclude from counts
        elif pnl > 0.001:
            wins.append(pnl)
        elif pnl < -0.001:
            losses.append(pnl)
        # else: pnl≈0 with no clear outcome label → excluded
    wr = (len(wins) / max(1, len(wins)+len(losses))) * 100 if (wins or losses) else None
    avg_win = (sum(wins)/len(wins)) if wins else 0.0
    avg_loss = (sum(losses)/len(losses)) if losses else 0.0
    rr_blended = (avg_win / abs(avg_loss)) if (avg_win > 0 and avg_loss < 0) else None
    return {'wins': len(wins), 'losses': len(losses), 'breakevens': 0,
            'pnl_total': round(pnl_total, 4),
            'avg_win': round(avg_win, 4), 'avg_loss': round(avg_loss, 4),
            'wr': round(wr, 2) if wr is not None else None,
            'rr_blended': round(rr_blended, 3) if rr_blended is not None else None}


def _serialize_open(positions_dict):
    """Compact summary of each open position. Includes cloids for fill attribution."""
    out = []
    if not positions_dict: return out
    for coin, p in positions_dict.items():
        if not isinstance(p, dict): continue
        try:
            entry = p.get('entry') or p.get('entry_px') or 0
            sl    = p.get('sl')    or p.get('sl_px')    or 0
            tp1   = p.get('tp1')   or p.get('tp1_px')   or 0
            tp2   = p.get('tp2')   or p.get('tp2_px')   or 0
            sz    = p.get('size')  or p.get('sz')       or p.get('sz_total') or 0
            is_long = p.get('is_long')
            if is_long is None:
                is_long = (sl < entry) if (sl and entry) else None
            out.append({
                'coin':     coin,
                'side':     'LONG' if is_long else ('SHORT' if is_long is False else '?'),
                'entry':    entry,
                'sl':       sl,
                'tp1':      tp1,
                'tp2':      tp2,
                'size':     sz,
                'opened_t': p.get('fired_t') or p.get('opened_t') or 0,
                'unreal_pnl': float(p.get('unrealized_pnl') or 0),
                'cloids': {
                    'entry': p.get('cloid_entry'),
                    'sl':    p.get('cloid_sl'),
                    'tp1':   p.get('cloid_tp1'),
                    'tp2':   p.get('cloid_tp2'),
                    'close': p.get('cloid_close'),
                },
            })
        except Exception:
            continue
    return out


def _serialize_history_recent(history_list, limit=30):
    """Most recent closed trades within 12h."""
    if not history_list: return []
    cutoff = int(time.time()*1000) - WINDOW_MS
    out = []
    for h in history_list:
        ts = h.get('close_t') or h.get('exit_t') or h.get('closed_t') or h.get('ts') or 0
        if not ts or ts < cutoff: continue

        entry    = h.get('entry') or h.get('entry_px') or 0
        close_px = h.get('exit_px') or h.get('close_px') or 0
        sz       = float(h.get('sz_total') or h.get('sz') or h.get('size') or 0)
        is_long  = h.get('is_long')
        if is_long is None:
            side_str = (h.get('side') or '').upper()
            is_long = side_str == 'LONG' if side_str else None

        # PNL fallback: explicit field, else compute from price+size
        pnl = float(h.get('realized_pnl') or h.get('pnl_usd') or h.get('pnl') or 0)
        if pnl == 0 and entry and close_px and sz and is_long is not None:
            sign = 1 if is_long else -1
            pnl = round(sign * (float(close_px) - float(entry)) * sz, 4)

        # OUTCOME fallback: explicit, then close_reason, then sign of pnl
        outcome = h.get('outcome')
        if not outcome:
            cr = (h.get('close_reason') or '').lower()
            if cr.startswith('tp2'):                               outcome = 'TP2'
            elif cr == 'tp1':                                       outcome = 'TP1'
            elif cr == 'be_stop':                                   outcome = 'BE'
            elif cr == 'sl':                                        outcome = 'SL'
            elif any(x in cr for x in ('time','pending','zombie')): outcome = 'TIMEOUT'
            elif cr:                                                outcome = cr.upper()
            elif pnl > 0.001:                                       outcome = 'WIN'
            elif pnl < -0.001:                                      outcome = 'LOSS'
            else:                                                   outcome = 'BE'

        out.append({
            'coin':    h.get('coin'),
            'side':    'LONG' if is_long else 'SHORT',
            'entry':   entry,
            'exit':    close_px,
            'pnl':     pnl,
            'outcome': outcome,
            'close_t': ts,
            'cloids': {
                'entry': h.get('cloid_entry'),
                'sl':    h.get('cloid_sl'),
                'tp1':   h.get('cloid_tp1'),
                'tp2':   h.get('cloid_tp2'),
                'close': h.get('cloid_close'),
            },
        })
    out.sort(key=lambda x: -(x['close_t'] or 0))
    return out[:limit]


def _do_push(payload):
    if not DASH_URL:
        return
    try:
        body = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            DASH_URL + '/push',
            data=body,
            method='POST',
            headers={'Content-Type': 'application/json',
                     'X-Push-Secret': DASH_SECRET,
                     'User-Agent': 'engine-push/1'})
        with urllib.request.urlopen(req, timeout=PUSH_TIMEOUT_SEC) as r:
            r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, Exception):
        # Never raise back to the caller — dashboard MUST NOT affect trading.
        pass


def push_state(engine_name, live, sizing_mode, notional_usd, max_concurrent,
               positions_dict, history_list, scan_count=0, last_scan_ts=0,
               extra_telemetry=None):
    """Build the snapshot payload and POST it in a daemon thread (non-blocking).

    Idempotent and crash-safe — wraps everything in try/except and never raises.
    """
    try:
        payload = {
            'engine':         engine_name,
            'live':           bool(live),
            'sizing_mode':    sizing_mode,
            'notional_usd':   notional_usd,
            'max_concurrent': max_concurrent,
            'open_positions': _serialize_open(positions_dict),
            'history_12h':    _serialize_history_recent(history_list, limit=30),
            'stats_12h':      _compute_stats_12h(history_list),
            'telemetry': {
                'fires_total':   len(history_list) if history_list else 0,
                'filled':        len(history_list) if history_list else 0,
                'no_fill':       0,
                'errors':        0,
                'scan_count':    scan_count,
                'last_scan_ts':  last_scan_ts,
                **((extra_telemetry or {})),
            },
        }
        threading.Thread(target=_do_push, args=(payload,), daemon=True).start()
    except Exception:
        # No-op on any internal serialization error
        pass


def start_heartbeat(engine_name, state_getter, config_getter, interval_sec=60,
                    log_fn=None):
    """Start a daemon thread that pushes a fresh state snapshot every N seconds.

    Use this in worker engines whose save_state() fires only on scan cycles
    (which can take 10-20 min on slow scans), so the dashboard 5-min staleness
    threshold isn't tripped.

    Args:
        engine_name:    'multi-gate' / 'smc-v1' / 'smc-v2' / 'smc-loose' / 'lsr'
        state_getter:   callable() -> the engine's state dict (with 'positions' and 'history')
        config_getter:  callable() -> dict with keys: live, sizing_mode, notional_usd, max_concurrent
        interval_sec:   how often to push (default 60s)
        log_fn:         optional callable(msg) for log output
    """
    def _heartbeat_loop():
        if log_fn: log_fn(f'[dashboard heartbeat] started (interval={interval_sec}s)')
        while True:
            try:
                state = state_getter() or {}
                cfg = config_getter() or {}
                # ── Filter pending/done positions out of heartbeat push ──
                # save_state has its own filter, but heartbeat is a SEPARATE
                # push path (60s cadence) that gets the raw state.positions.
                # Without filtering here, phantoms (phase=pending_fill or
                # corrupt phase=live without actual fill) leak through.
                all_pos = state.get('positions', {}) or {}
                filtered_pos = {}
                for coin, p in all_pos.items():
                    if not isinstance(p, dict):
                        continue
                    phase = p.get('phase')
                    # No phase field → assume legacy engine, include it
                    if phase is None:
                        filtered_pos[coin] = p
                        continue
                    if phase not in ('live', 'tp1_filled'):
                        continue
                    # Even if phase says live, require entry actually filled
                    cum = float(p.get('entry_filled_sz', 0) or 0)
                    tot = float(p.get('sz_total', 0) or 0)
                    if tot > 0 and cum < tot * 0.95:
                        continue
                    filtered_pos[coin] = p
                push_state(
                    engine_name=engine_name,
                    live=cfg.get('live', False),
                    sizing_mode=cfg.get('sizing_mode'),
                    notional_usd=cfg.get('notional_usd'),
                    max_concurrent=cfg.get('max_concurrent'),
                    positions_dict=filtered_pos,
                    history_list=state.get('history', []),
                    scan_count=state.get('scan_count', 0),
                    last_scan_ts=state.get('last_scan_ts', 0),
                )
            except Exception as e:
                if log_fn: log_fn(f'[dashboard heartbeat] err: {e}')
            time.sleep(interval_sec)
    t = threading.Thread(target=_heartbeat_loop, daemon=True, name=f'dash_hb_{engine_name}')
    t.start()
    return t
