"""Post-mortem runner.

Entry point from precog.py:
    run_postmortem_async(pos, coin, pnl_pct)  → fire-and-forget, daemon thread

Inside the thread:
    1. Build trade dict from `pos` and close metadata
    2. Build per-component context snapshots from live modules
       (bybit_ws, cvd_ws, orderbook_ws, regime_detector, etc.)
    3. Fan out all agents in parallel (ThreadPoolExecutor)
    4. Collect findings
    5. Synthesize with head agent (Sonnet)
    6. Tune per synthesizer decisions within bounds
    7. Write audit trail
"""
import os
import time
import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db, agents, tuner

# Max concurrent agents. Anthropic handles the concurrency fine; this is
# about not starving the main trading loop of CPU.
MAX_WORKERS = int(os.environ.get('POSTMORTEM_MAX_WORKERS', '6'))
ENABLED = os.environ.get('POSTMORTEM_ENABLED', '1') == '1'


def _safe_log(msg):
    try:
        print(f'[postmortem] {msg}', flush=True)
    except Exception:
        pass


def _build_trade(pos, coin, pnl_pct):
    now = time.time()
    entry_ts = (pos.get('entry_ts') or pos.get('open_ts') or pos.get('opened_at')
                or pos.get('ts') or now)
    pnl = float(pnl_pct)
    # pnl_pct is ALREADY in percent units (e.g. -0.158 means -0.158%, not -15.8%).
    # LLMs reading raw JSON often misinterpret small decimals as fractional returns
    # and multiply by 100. Add an unambiguous human-readable field.
    pnl_display = f'{pnl:+.3f}%'  # e.g. "-0.158%", "+0.410%"
    return {
        'coin': coin,
        'side': pos.get('side', '?'),
        'engine': pos.get('engine', 'UNKNOWN'),
        'entry_px': pos.get('entry') or pos.get('entryPx'),
        'pnl_pct': pnl,
        'pnl_display': pnl_display,  # unambiguous "+X.XXX%" format
        'pnl_note': 'pnl_pct is already in percent; -0.158 means -0.158%, not -15.8%',
        'is_win': pnl > 0,
        'entry_ts': entry_ts,
        'exit_ts': now,
        'duration_s': now - entry_ts if entry_ts else None,
        'exit_reason': pos.get('exit_reason', 'unknown'),
        'sl_pct': pos.get('sl_pct'),
        'tp_pct': pos.get('tp_pct'),
        'conf': pos.get('conf'),
        'size': pos.get('size') or pos.get('sz'),
        'leverage': pos.get('leverage') or pos.get('lev'),
        'utc_h': pos.get('utc_h'),
        'hwm': pos.get('hwm'),
        'tp_locked': pos.get('tp_locked'),
    }


def _build_context(coin, trade):
    """Snapshot every component's state at/around entry.

    This function is defensive: it reads from live modules if available,
    returns empty dicts if a module is missing. It never raises.
    """
    ctx = {
        'coin': coin,
        'side': trade['side'],
        'entry_px': trade['entry_px'],
        'entry_ts': trade['entry_ts'],
    }

    # Candles from bybit_ws if cached
    try:
        import bybit_ws
        if hasattr(bybit_ws, 'get_candles'):
            ctx['candles_5m'] = bybit_ws.get_candles(coin, '5m', 60) or []
    except Exception: pass

    # CVD state
    try:
        import cvd_ws
        if hasattr(cvd_ws, 'get_cvd'):
            ctx['cvd'] = cvd_ws.get_cvd(coin)
    except Exception: pass

    # Orderbook snapshot
    try:
        import orderbook_ws
        if hasattr(orderbook_ws, 'get_snapshot'):
            ctx['orderbook'] = orderbook_ws.get_snapshot(coin)
    except Exception: pass

    # Liquidations
    try:
        import liquidation_ws
        if hasattr(liquidation_ws, 'get_recent'):
            ctx['liquidations'] = liquidation_ws.get_recent(coin)
    except Exception: pass

    # OI
    try:
        import oi_tracker
        if hasattr(oi_tracker, 'get_oi'):
            ctx['oi'] = oi_tracker.get_oi(coin)
    except Exception: pass

    # Funding
    try:
        import funding_filter
        if hasattr(funding_filter, 'get_funding'):
            ctx['funding'] = funding_filter.get_funding(coin)
    except Exception: pass

    # Regime
    try:
        import regime_detector
        if hasattr(regime_detector, 'get_regime'):
            ctx['regime'] = regime_detector.get_regime(coin)
    except Exception: pass

    # Walls
    try:
        import wall_confluence
        if hasattr(wall_confluence, 'get_walls'):
            ctx['walls'] = wall_confluence.get_walls(coin)
    except Exception: pass

    # Whale flow
    try:
        import whale_filter
        if hasattr(whale_filter, 'get_whales'):
            ctx['whales'] = whale_filter.get_whales(coin)
    except Exception: pass

    # Per-coin OOS config (reveals which filters were active)
    try:
        import percoin_configs
        if hasattr(percoin_configs, 'get_config'):
            ctx['percoin_config'] = percoin_configs.get_config(coin)
    except Exception: pass

    # Market context: news, macro, calendar (for context-aware forensics)
    try:
        from . import context as _ctx
        mkt = _ctx.for_coin(coin)
        ctx['news_for_coin'] = mkt.get('news_for_coin', [])[:4]  # compact
        ctx['macro'] = mkt.get('macro', {})
        ctx['calendar_2h'] = mkt.get('calendar_2h', [])
    except Exception: pass

    return ctx


def _run_one_agent(name, fn, trade, context):
    try:
        result = fn(trade, context) or {}
        result['agent'] = name
        return result
    except Exception as e:
        return {
            'agent': name,
            'verdict': 'irrelevant',
            'confidence': 0.0,
            'reasoning': f'runtime error: {type(e).__name__}: {str(e)[:200]}',
            'proposed_delta': [],
            'proposed_veto': None,
        }


def _run_sync(pos, coin, pnl_pct):
    """Synchronous post-mortem. Called inside the daemon thread."""
    if not ENABLED:
        return

    try:
        db.init_db()
    except Exception as e:
        _safe_log(f'db init failed: {e}')
        return

    trade = _build_trade(pos, coin, pnl_pct)
    pos_copy = {k: v for k, v in pos.items() if not callable(v)}

    log_id = None
    try:
        log_id = db.create_log_entry(
            coin=coin,
            side=trade['side'],
            engine=trade['engine'],
            pnl_pct=trade['pnl_pct'],
            entry_px=trade['entry_px'],
            exit_reason=trade['exit_reason'],
            duration_s=trade['duration_s'],
            pos_dict=pos_copy,
        )
    except Exception as e:
        _safe_log(f'log entry creation failed: {e}')
        return

    # Check API key before spending time on context build
    if not os.environ.get('ANTHROPIC_API_KEY'):
        db.update_log_entry(log_id, agents_run=0, deltas_applied=0, status='skipped_no_api_key')
        _safe_log(f'log_id={log_id} skipped: ANTHROPIC_API_KEY not set')
        return

    try:
        context = _build_context(coin, trade)
    except Exception as e:
        _safe_log(f'context build failed: {e}')
        context = {'coin': coin, 'side': trade['side'], 'error': str(e)}

    # Fan out agents
    findings = []
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_run_one_agent, name, fn, trade, context): name
                       for name, fn in agents.AGENTS.items()}
            for fut in as_completed(futures, timeout=180):
                try:
                    findings.append(fut.result(timeout=60))
                except Exception as e:
                    findings.append({
                        'agent': futures[fut],
                        'verdict': 'irrelevant',
                        'confidence': 0.0,
                        'reasoning': f'future error: {e}',
                        'proposed_delta': [],
                        'proposed_veto': None,
                    })
    except Exception as e:
        _safe_log(f'agent fan-out failed: {e}')

    # Filter out irrelevants before sending to synthesizer to reduce noise
    relevant = [f for f in findings if f.get('verdict') != 'irrelevant']

    synthesis = {'root_cause': '', 'decisions': [], 'new_vetos': []}
    if relevant:
        try:
            synthesis = agents.synthesize(trade, relevant)
        except Exception as e:
            _safe_log(f'synthesis failed: {e}')

    deltas_applied = 0
    try:
        deltas_applied = tuner.apply_decisions(coin, trade, findings, synthesis, log_id)
    except Exception as e:
        _safe_log(f'tuner failed: {e}')

    db.update_log_entry(
        log_id,
        agents_run=len(findings),
        deltas_applied=deltas_applied,
        status=f'complete:{synthesis.get("root_cause","")[:500]}',
        synthesis_json=json.dumps(synthesis, default=str),
    )
    _safe_log(f'log_id={log_id} coin={coin} pnl={pnl_pct:.2f}% agents={len(findings)} '
              f'relevant={len(relevant)} deltas={deltas_applied}')


def run_postmortem_async(pos, coin, pnl_pct):
    """Public entry. Fire-and-forget. Safe to call from HL close path only.
    Do NOT call from MT4 close path — this module is HL-scoped."""
    if not ENABLED:
        return
    try:
        # Shallow copy so caller can keep mutating `pos` without affecting us
        pos_snapshot = dict(pos) if isinstance(pos, dict) else {}
        t = threading.Thread(
            target=_run_sync,
            args=(pos_snapshot, coin, pnl_pct),
            name=f'postmortem-{coin}-{int(time.time())}',
            daemon=True,
        )
        t.start()
    except Exception as e:
        _safe_log(f'thread spawn failed: {e}')
