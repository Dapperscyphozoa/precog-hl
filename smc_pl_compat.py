"""
smc_pl_compat.py — Position-ledger compatibility shim.

The existing precog-hl/main position_ledger exposes:
  on_webdata2, on_fill, on_order_update, mark_ws_connected, ws_is_fresh

It MAY expose: get_position(coin), get_all_positions(), get_mark_price, get_equity.
We don't know without inspecting. This shim provides those reads with fallbacks
so smc_engine / smc_monitors / smc_app don't crash if the names differ.

Read order:
  1. If position_ledger has the function name, call it.
  2. Else fall back to direct HL Info.all_mids() / clearinghouseState().
"""
import os
import logging
from functools import lru_cache

log = logging.getLogger(__name__)

_info = None


def _ensure_info():
    global _info
    if _info is None:
        try:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
            _info = Info(constants.MAINNET_API_URL, skip_ws=True)
        except Exception as e:
            log.warning(f"compat: failed to init Info: {e}")
    return _info


def get_mark_price(coin: str):
    """Returns latest mark price as float or None."""
    coin = coin.upper()
    try:
        import position_ledger
        if hasattr(position_ledger, 'get_mark_price'):
            v = position_ledger.get_mark_price(coin)
            if v is not None:
                return float(v)
        # Try reading from per-coin row
        if hasattr(position_ledger, 'get_position'):
            row = position_ledger.get_position(coin) or {}
            for k in ('mark_px', 'mark_price', 'mid_px', 'mid_price'):
                if row.get(k):
                    return float(row[k])
        if hasattr(position_ledger, '_state') or hasattr(position_ledger, '_rows'):
            rows = getattr(position_ledger, '_rows', None) or getattr(position_ledger, '_state', {})
            row = (rows.get(coin) if isinstance(rows, dict) else None) or {}
            for k in ('mark_px', 'mark_price', 'mid_px'):
                if isinstance(row, dict) and row.get(k):
                    return float(row[k])
    except Exception as e:
        log.warning(f"compat get_mark_price ledger lookup failed: {e}")

    # Fallback: HL all_mids
    info = _ensure_info()
    if info is None:
        return None
    try:
        mids = info.all_mids()
        v = mids.get(coin)
        return float(v) if v is not None else None
    except Exception as e:
        log.warning(f"compat get_mark_price all_mids failed: {e}")
        return None


def get_equity():
    """Returns account equity (float) or None."""
    try:
        import position_ledger
        if hasattr(position_ledger, 'get_equity'):
            v = position_ledger.get_equity()
            if v is not None:
                return float(v)
        if hasattr(position_ledger, 'get_account_summary'):
            s = position_ledger.get_account_summary() or {}
            v = s.get('equity') or s.get('account_value')
            if v is not None:
                return float(v)
    except Exception as e:
        log.warning(f"compat get_equity ledger lookup failed: {e}")

    # Fallback: HL clearinghouseState
    info = _ensure_info()
    if info is None:
        return None
    addr = os.environ.get('HL_ADDRESS', '')
    if not addr:
        return None
    try:
        cs = info.user_state(addr)
        ms = (cs or {}).get('marginSummary') or {}
        v = ms.get('accountValue')
        return float(v) if v is not None else None
    except Exception as e:
        log.warning(f"compat get_equity clearinghouseState failed: {e}")
        return None


def ws_is_fresh() -> bool:
    try:
        import position_ledger
        if hasattr(position_ledger, 'ws_is_fresh'):
            return bool(position_ledger.ws_is_fresh())
    except Exception:
        pass
    return False
