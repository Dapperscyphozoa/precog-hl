"""PreCog Post-Mortem Tuning Engine

Per-component forensic analysis of every HL close. Agents diagnose which
signal components failed or succeeded, propose surgical parameter deltas,
apply them within hardcoded bounds. Signal engine reads params live at
the next signal tick.

HL-ONLY. Does not touch MT4 paths.

Public API:
    run_postmortem_async(pos, coin, pnl_pct) -> None
        Fire-and-forget. Spawns daemon thread, returns immediately.

    get_param(coin, component, param_name, default) -> float
        Read a tuned param with fallback. Signal engines call this.

    get_veto(coin, component) -> bool
        Hard-veto check. True means signal engine should reject.
"""
from .runner import run_postmortem_async
from .params_api import get_param, get_veto, params_summary
from .db import init_db

__all__ = [
    'run_postmortem_async',
    'get_param',
    'get_veto',
    'params_summary',
    'init_db',
]
