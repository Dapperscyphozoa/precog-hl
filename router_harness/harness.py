"""Orchestrator. Loads recorded signal + wall streams; for each signal at time T,
finds wall context at T (nearest snapshot ≤T); applies router fn; scores outcome
via HL candles; aggregates.

Usage:
    from router_harness.harness import run_backtest
    from router_harness.router import route
    summary = run_backtest(
        signals_jsonl='/var/data/router_harness/signals_20260511.jsonl',
        walls_jsonl='/var/data/router_harness/walls_20260511.jsonl',
        router_fn=route,
        router_params={'block_proximity_pct': 0.003, ...},
    )

Three router variants to compare:
    1. status_quo:   symmetric block at 0.5% (current btc_macro behavior)
    2. router_v1:    full council recipe (side+approach+persistence+size)
    3. allow_all:    no guard at all (baseline for what we're protecting against)

Output: per-variant aggregate stats — n_block, n_allow, n_modify,
  cumulative_pnl_120m, avg_pnl, win_rate, sharpe-ish.
"""
import json
import time
from collections import defaultdict
from typing import Callable, Optional


def _load_jsonl(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
    return out


def _find_wall_context_at(walls_by_coin, coin, ts):
    """Walls for this coin at time ts (nearest snapshot ≤ ts within 60s)."""
    snaps = walls_by_coin.get(coin, [])
    candidate = None
    for s in snaps:
        if s['ts'] > ts: break
        if ts - s['ts'] <= 60:
            candidate = s
    return candidate


def run_backtest(signals_jsonl, walls_jsonl, router_fn, router_params=None, score_fn=None):
    """Returns: {n_signals, n_allow, n_block, n_modify, sum_pnl_120m, win_rate, ...}."""
    from .scorer import score_signal, attribute_decision
    score_fn = score_fn or score_signal
    router_params = router_params or {}

    signals = _load_jsonl(signals_jsonl)
    walls   = _load_jsonl(walls_jsonl)
    # Index walls: coin -> sorted list of snapshots
    walls_by_coin = defaultdict(list)
    for w in walls:
        walls_by_coin[w['coin']].append(w)
    for c in walls_by_coin: walls_by_coin[c].sort(key=lambda x: x['ts'])

    agg = defaultdict(float)
    actions = defaultdict(int)
    pnls = defaultdict(list)
    per_signal = []

    for sig in signals:
        if sig.get('side') not in ('BUY', 'SELL'): continue
        wall_ctx = _find_wall_context_at(walls_by_coin, sig['coin'], sig['ts'])
        walls_at_t = []
        if wall_ctx:
            # Enrich walls with persistence_sec if available
            for w in wall_ctx.get('walls', []):
                w2 = dict(w)
                if 'persistence_sec' not in w2 and 'persistence_windows' in w2:
                    w2['persistence_sec'] = w2['persistence_windows'] * 30
                walls_at_t.append(w2)
        decision = router_fn(
            coin=sig['coin'], side=sig['side'], entry_px=sig['entry_px'],
            walls=walls_at_t, recent_px_trajectory=None,
            **router_params
        )
        score = score_fn(sig['coin'], sig['side'], sig['entry_px'], sig['ts'])
        if 'err' in score:
            continue
        attrib = attribute_decision(score, decision)
        actions[decision['action']] += 1
        for h in (30, 60, 120):
            pnls[h].append(attrib.get(f'pnl_pct_{h}m', 0.0))
        per_signal.append({'sig': sig, 'decision': decision, 'attrib': attrib})

    def stats(arr):
        if not arr: return {'n':0}
        wins = sum(1 for x in arr if x > 0)
        losses = sum(1 for x in arr if x < 0)
        return {
            'n': len(arr),
            'sum_pct': round(sum(arr), 4),
            'mean_pct': round(sum(arr)/len(arr), 4),
            'win_rate_pct': round(100*wins/len(arr), 1),
            'n_wins': wins,
            'n_losses': losses,
        }
    return {
        'router_params': router_params,
        'n_signals_scored': len(per_signal),
        'actions': dict(actions),
        'horizons': {f'{h}m': stats(pnls[h]) for h in (30, 60, 120)},
        'per_signal_sample': per_signal[:5],
    }


def compare_variants(signals_jsonl, walls_jsonl):
    """Run 3 canonical variants side-by-side."""
    from .router import route
    def status_quo_fn(**kwargs):
        # Mimic current btc_macro: symmetric block if any wall within 0.5%
        walls = kwargs.get('walls', [])
        entry = kwargs.get('entry_px', 0)
        if entry == 0: return {'action':'ALLOW','reason':'no_entry','size_mult':1.0,
                                'suggested_sl_px':None,'suggested_tp_px':None}
        for w in walls:
            if abs(w.get('price', 0) - entry) / entry <= 0.005:
                return {'action':'BLOCK','reason':'symmetric_block_0.5pct','size_mult':0,
                        'suggested_sl_px':None,'suggested_tp_px':None}
        return {'action':'ALLOW','reason':'no_walls_within_0.5pct','size_mult':1.0,
                'suggested_sl_px':None,'suggested_tp_px':None}
    def allow_all_fn(**kwargs):
        return {'action':'ALLOW','reason':'no_guard','size_mult':1.0,
                'suggested_sl_px':None,'suggested_tp_px':None}
    return {
        'status_quo':       run_backtest(signals_jsonl, walls_jsonl, status_quo_fn),
        'router_v1':        run_backtest(signals_jsonl, walls_jsonl, route),
        'allow_all':        run_backtest(signals_jsonl, walls_jsonl, allow_all_fn),
    }
