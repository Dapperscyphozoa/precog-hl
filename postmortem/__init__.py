"""PreCog Post-Mortem Tuning Engine

Per-component forensic analysis of every HL close. Agents diagnose which
signal components failed or succeeded, propose surgical parameter deltas,
apply them within hardcoded bounds. Signal engine reads params live at
the next signal tick.

Entry gate sits on top: before every trade fires, a Sonnet call reads
tuned params + active vetos + relevant KB entries and returns
ALLOW/SIZE_DOWN/BLOCK.

HL-ONLY. Does not touch MT4 paths.

Public API:
    run_postmortem_async(pos, coin, pnl_pct) -> None
        Fire-and-forget after HL close.

    get_param(coin, component, param_name, default) -> float
        Fast hot-path read for signal engines (30s cached).

    get_veto(coin, component) -> bool
        Hard-veto check.

    evaluate_entry(coin, side, signal_ctx) -> dict
        Pre-trade gate. Returns {decision, size_mult, reason, ...}.
        Never raises. Always returns a dict.
"""
from .runner import run_postmortem_async
from .params_api import get_param, get_veto, params_summary
from .entry_gate import evaluate_entry
from .db import init_db
from . import kb as kb_module

__all__ = [
    'run_postmortem_async',
    'get_param',
    'get_veto',
    'params_summary',
    'evaluate_entry',
    'init_db',
    'kb_module',
]
