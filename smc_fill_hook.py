"""
smc_fill_hook.py — Wraps position_ledger.on_fill to dispatch SMC fills.

Strategy: monkey-patch position_ledger.on_fill at boot. The wrapper:
  1. Calls the original (so the live state machine still works untouched)
  2. If the cloid prefix or trade_id pattern matches SMC, calls
     smc_execution.on_smc_fill with the fill event

Cloid prefix discrimination: SMC cloids derive from trade_id "smc-{alert_id}",
so the cloid string starts with the same hash, but cleaner is to match by
oid against state.armed[*].entry_oid set during submit.

This module exposes install() — call once on app boot.
"""
import logging
import threading
import position_ledger

log = logging.getLogger(__name__)
_installed = False
_lock = threading.Lock()


def install():
    """Wrap position_ledger.on_fill. Idempotent."""
    global _installed
    with _lock:
        if _installed:
            return
        original_on_fill = position_ledger.on_fill

        def wrapped_on_fill(*args, **kwargs):
            # 1. Always run the live system's handler first
            try:
                result = original_on_fill(*args, **kwargs)
            except Exception as e:
                log.exception(f"position_ledger.on_fill (original) raised: {e}")
                result = None

            # 2. Then dispatch to SMC if applicable
            try:
                fill_event = _normalize_fill_args(args, kwargs)
                from smc_execution import on_smc_fill
                on_smc_fill(fill_event)
            except Exception as e:
                log.exception(f"smc on_fill dispatch raised: {e}")

            return result

        position_ledger.on_fill = wrapped_on_fill
        _installed = True
        log.info("smc_fill_hook installed (position_ledger.on_fill wrapped)")


def _normalize_fill_args(args, kwargs) -> dict:
    """Coerce position_ledger.on_fill positional+kwarg signature to dict."""
    # Real signature: on_fill(coin, side, sz, px, ts_ms, oid=, cloid=)
    out = {}
    keys = ['coin', 'side', 'sz', 'px', 'ts_ms']
    for i, val in enumerate(args[:5]):
        out[keys[i]] = val
    out.update(kwargs)
    out['size'] = out.get('sz')
    return out
