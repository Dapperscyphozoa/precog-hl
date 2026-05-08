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

Caching: get_equity / get_mark_price are TTL-cached to prevent the 429 cascade
that otherwise emerges when /smc/status, dashboard pusher, and heartbeat all
hammer clearinghouseState every few seconds. WS is the truth source; REST is
just a fallback that should be quiet.
"""
import os
import time
import threading
import logging
from functools import lru_cache

log = logging.getLogger(__name__)

_info = None

# Caches: prevent REST fallback hammering CloudFront → 429 cascade.
_EQUITY_TTL_SEC = 60
_MARK_TTL_SEC = 10
_equity_cache = {'ts': 0.0, 'val': None}
_mark_cache = {}                 # coin -> {'ts': float, 'val': float|None}
_cache_lock = threading.Lock()


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
    """Returns latest mark price as float or None. Cached 10s per coin."""
    coin = coin.upper()
    now = time.time()
    with _cache_lock:
        c = _mark_cache.get(coin)
        if c and (now - c['ts']) < _MARK_TTL_SEC:
            return c['val']

    val = None
    try:
        import position_ledger
        if hasattr(position_ledger, 'get_mark_price'):
            v = position_ledger.get_mark_price(coin)
            if v is not None:
                val = float(v)
        if val is None and hasattr(position_ledger, 'get_position'):
            row = position_ledger.get_position(coin) or {}
            for k in ('mark_px', 'mark_price', 'mid_px', 'mid_price'):
                if row.get(k):
                    val = float(row[k]); break
        if val is None and (hasattr(position_ledger, '_state') or hasattr(position_ledger, '_rows')):
            rows = getattr(position_ledger, '_rows', None) or getattr(position_ledger, '_state', {})
            row = (rows.get(coin) if isinstance(rows, dict) else None) or {}
            for k in ('mark_px', 'mark_price', 'mid_px'):
                if isinstance(row, dict) and row.get(k):
                    val = float(row[k]); break
    except Exception as e:
        log.warning(f"compat get_mark_price ledger lookup failed: {e}")

    if val is None:
        info = _ensure_info()
        if info is not None:
            try:
                mids = info.all_mids()
                v = mids.get(coin)
                val = float(v) if v is not None else None
            except Exception as e:
                log.warning(f"compat get_mark_price all_mids failed: {e}")

    with _cache_lock:
        _mark_cache[coin] = {'ts': now, 'val': val}
    return val


def get_equity():
    """Returns account equity (float) or None. Cached 60s to stop 429 cascade."""
    now = time.time()
    with _cache_lock:
        if _equity_cache['val'] is not None and (now - _equity_cache['ts']) < _EQUITY_TTL_SEC:
            return _equity_cache['val']

    val = None
    try:
        import position_ledger
        if hasattr(position_ledger, 'get_equity'):
            v = position_ledger.get_equity()
            if v is not None:
                val = float(v)
        if val is None and hasattr(position_ledger, 'get_account_summary'):
            s = position_ledger.get_account_summary() or {}
            v = s.get('equity') or s.get('account_value')
            if v is not None:
                val = float(v)
    except Exception as e:
        log.warning(f"compat get_equity ledger lookup failed: {e}")

    if val is None:
        info = _ensure_info()
        addr = os.environ.get('HL_ADDRESS', '')
        if info is not None and addr:
            try:
                cs = info.user_state(addr)
                ms = (cs or {}).get('marginSummary') or {}
                v = ms.get('accountValue')
                val = float(v) if v is not None else None
            except Exception as e:
                # Stale-fallback: return last known on 429
                log.warning(f"compat get_equity clearinghouseState failed: {e}")
                with _cache_lock:
                    if _equity_cache['val'] is not None:
                        return _equity_cache['val']

    with _cache_lock:
        if val is not None:
            _equity_cache['ts'] = now
            _equity_cache['val'] = val
        elif _equity_cache['val'] is not None:
            # Suppress re-fetch storms even when None: hold for 30s
            _equity_cache['ts'] = now - (_EQUITY_TTL_SEC - 30)
    return val


def ws_is_fresh() -> bool:
    """WS health signal. position_ledger.ws_is_fresh() requires a webData2 msg
    within 30s, but HL only pushes webData2 on changes — a quiet account
    (no fills, no positions) goes silent and looks 'stale' even though the
    socket is healthy. We trust the connected flag instead, falling back to
    the strict check only if connected is unknown.
    """
    try:
        import position_ledger
        # Prefer the explicit connected flag when present
        if hasattr(position_ledger, '_LEDGER'):
            ledger = getattr(position_ledger, '_LEDGER', None)
            if ledger is not None and hasattr(ledger, '_ws_connected'):
                return bool(ledger._ws_connected)
        if hasattr(position_ledger, 'ws_is_fresh'):
            return bool(position_ledger.ws_is_fresh(max_age_sec=600))
    except Exception:
        pass
    return False

