"""
smc_execution.py — Order submission + lifecycle management for SMC v1.0.

Uses precog-hl/main's existing atomic layer:
  - atomic_entry.submit_atomic        → bracket placement
  - atomic_reconciler                  → SL/TP size correctness on partial fills
  - flight_guard.acquire(coin)         → BLOCKING write spacer (NOT a context mgr)
  - position_ledger.on_fill            → fill events (we hook via smc_fill_hook)

submit_smc_trade(payload, ctx)        — called by smc_engine after gates pass
on_smc_fill(fill_event)               — wired via smc_fill_hook (cloid-prefix dispatch)
on_smc_position_closed(coin, ...)     — wired via position_ledger close detection
expire_if_unfilled(trade_id)          — armed-order timeout
replace_sl(pos, new_sl_px)            — BE move
close_market(pos, reason)             — time-stop / manual close
"""
import os
import time
import threading
import logging

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid
from eth_account import Account

import atomic_entry
from atomic_entry import _build_cloid, _make_cloid_obj
import flight_guard
import smc_trade_log
from smc_config import SMC_CONFIG
from smc_state import state, persist as state_persist

log = logging.getLogger(__name__)

# ---------------- HL setup ----------------
HL_PRIVATE_KEY = os.environ.get('HL_PRIVATE_KEY', '')
HL_ADDRESS = os.environ.get('HL_ADDRESS', '')

_info = None
_exchange = None
_meta_cache = {'sz_decimals': {}, 'px_decimals': {}}


def _ensure_hl():
    global _info, _exchange
    if _info is None:
        _info = Info(constants.MAINNET_API_URL, skip_ws=True)
        meta = _info.meta()
        for u in meta.get('universe', []):
            name = u.get('name')
            if name:
                _meta_cache['sz_decimals'][name] = int(u.get('szDecimals', 4))
                _meta_cache['px_decimals'][name] = max(0, 6 - int(u.get('szDecimals', 4)))
    if _exchange is None and HL_PRIVATE_KEY:
        wallet = Account.from_key(HL_PRIVATE_KEY)
        _exchange = Exchange(wallet, constants.MAINNET_API_URL,
                             account_address=HL_ADDRESS or None)


def round_size(coin: str, sz: float) -> float:
    _ensure_hl()
    d = _meta_cache['sz_decimals'].get(coin, 4)
    return round(sz, d)


def round_price(coin: str, px: float) -> float:
    """Match precog-hl precog.round_price for small-tick safety."""
    _ensure_hl()
    d = _meta_cache['px_decimals'].get(coin, 4)
    # 5 sig figs first, then asset-specific decimals
    return round(float(f"{px:.5g}"), d)


# ---------------- Submit ----------------

def submit_smc_trade(payload: dict, ctx: dict):
    """Place atomic entry+SL+TP. Returns (response_body, http_status)."""
    _ensure_hl()
    if _exchange is None:
        return {'status': 'no_exchange', 'error': 'HL_PRIVATE_KEY not set'}, 500

    # Ensure ENTRY_TIF is set per spec (maker-only, no taker fallback)
    if SMC_CONFIG.get('order_type') == 'maker_only':
        os.environ.setdefault('ENTRY_TIF', 'Alo')

    coin = (payload.get('coin') or '').upper()
    notional = SMC_CONFIG['force_notional_usd']
    ob_top = float(payload['ob_top'])
    sl_px = float(payload['sl_price'])
    tp_px = float(payload['tp2'])

    raw_size = notional / ob_top
    size = round_size(coin, raw_size)

    # Reject below HL min notional
    if size * ob_top < 10:
        smc_trade_log.append({
            'event': 'REJECTED',
            'alert_id': payload.get('alert_id'),
            'coin': coin,
            'reason': 'below_min_notional',
            'notional_usd': size * ob_top,
        })
        return {'status': 'below_min_notional', 'notional': size * ob_top}, 200

    trade_id = f"smc-{payload['alert_id']}"
    submit_ms = int(time.time() * 1000)

    # flight_guard.acquire is a BLOCKING call (not context manager)
    flight_guard.acquire(coin)
    result = atomic_entry.submit_atomic(
        exchange=_exchange,
        coin=coin,
        is_buy=True,                # long-only
        size=size,
        mark_px=ob_top,
        sl_px=sl_px,
        tp_px=tp_px,
        trade_id=trade_id,
        slip_pct=0.0,
        log_fn=lambda m: log.info(f"atomic_entry: {m}"),
        price_rounder=round_price,
    )

    armed = {
        **payload,
        'coin': coin,
        'trade_id': trade_id,
        'size': size,
        'submit_ms': submit_ms,
        'submitted_at_ms': submit_ms,
        'expires_at_ms': submit_ms + SMC_CONFIG['limit_expiry_minutes'] * 60_000,
    }

    if not result.get('success'):
        smc_trade_log.append({
            'event': 'REJECTED',
            'trade_id': trade_id,
            'alert_id': payload.get('alert_id'),
            'coin': coin,
            'submit_ms': submit_ms,
            'hl_response_json': result.get('raw'),
            'error': result.get('reason'),
        })
        log.warning(f"REJECTED {coin}/{trade_id}: {result.get('reason')}")
        return {'status': 'submit_failed', 'reason': result.get('reason')}, 200

    # Captured oids from bulk_orders response
    armed['entry_oid'] = result.get('entry_oid')
    armed['sl_oid'] = result.get('sl_oid')
    armed['tp_oid'] = result.get('tp_oid')
    fill_px = result.get('fill_px')

    state.armed[trade_id] = armed
    try:
        state_persist()
    except Exception:
        pass

    smc_trade_log.log_armed(armed, result.get('raw'), submit_ms, ctx)

    # If IOC fill happened in-band, immediately promote to position
    if fill_px:
        on_smc_fill_synthetic(coin, trade_id, armed, fill_px, submit_ms)

    # Schedule expiry timeout
    threading.Timer(
        SMC_CONFIG['limit_expiry_minutes'] * 60,
        expire_if_unfilled,
        args=[trade_id]
    ).start()

    return {'status': 'armed', 'trade_id': trade_id, 'size': size}, 200


# ---------------- Lifecycle ----------------

def expire_if_unfilled(trade_id: str):
    """Cancel armed entry+SL+TP after limit_expiry_minutes if not filled."""
    armed = state.armed.get(trade_id)
    if not armed:
        return  # already filled or expired
    coin = armed.get('coin')
    log.info(f"expire_if_unfilled: cancelling {trade_id}")
    # Cancel by oid (match precog.py pattern) rather than cloid
    for oid_key in ('entry_oid', 'sl_oid', 'tp_oid'):
        oid = armed.get(oid_key)
        if not oid:
            continue
        try:
            flight_guard.acquire(coin)
            _exchange.cancel(coin, oid)
        except Exception as e:
            log.warning(f"cancel {trade_id} {oid_key}={oid} err: {e}")

    state.armed.pop(trade_id, None)
    try:
        state_persist()
    except Exception:
        pass
    smc_trade_log.append({**armed, 'event': 'EXPIRED', 'reason': 'limit_timeout'})


# ---------------- Fill dispatchers ----------------

def on_smc_fill_synthetic(coin: str, trade_id: str, armed: dict,
                          fill_px: float, fill_ms: int):
    """Called when atomic_entry returns an in-band IOC fill."""
    pos = _build_pos(armed, fill_px, armed['size'], fill_ms)
    state.positions[coin] = pos
    state.armed.pop(trade_id, None)
    try:
        state_persist()
    except Exception:
        pass
    smc_trade_log.log_filled(pos, fill_ms)


def on_smc_fill(fill: dict):
    """
    Called by smc_fill_hook when a SMC-prefixed cloid fills.
    fill: {coin, side, sz, px, ts_ms, oid, cloid}
    """
    cloid = fill.get('cloid')
    if not cloid:
        return
    cloid_str = str(cloid).lower()

    # Find armed by entry cloid suffix
    armed_match = None
    matched_tid = None
    for tid in list(state.armed.keys()):
        # Match by trade_id prefix in fill (cloid is opaque hash; rely on oid match)
        a = state.armed[tid]
        if (a.get('entry_oid') and fill.get('oid') == a['entry_oid']):
            armed_match = a
            matched_tid = tid
            break

    if not armed_match:
        # Could be SL/TP fill (close) — handled by close detection
        return

    coin = (armed_match.get('coin') or fill.get('coin') or '').upper()
    px = float(fill.get('px') or 0)
    size = float(fill.get('sz') or fill.get('size') or 0)
    ts_ms = int(fill.get('ts_ms') or time.time() * 1000)

    pos = _build_pos(armed_match, px, size, ts_ms)
    state.positions[coin] = pos
    state.armed.pop(matched_tid, None)
    try:
        state_persist()
    except Exception:
        pass
    smc_trade_log.log_filled(pos, ts_ms)


def _build_pos(armed: dict, fill_px: float, fill_size: float, fill_ms: int) -> dict:
    return {
        **armed,
        'state': 'OPEN',
        'fill_price': fill_px,
        'fill_size': fill_size,
        'fill_time_ms': fill_ms,
        'sl_orig': float(armed.get('sl_price', 0)),
        'sl_current': float(armed.get('sl_price', 0)),
        'best_r': 0.0,
        'worst_r': 0.0,
        'mfe_pct': 0.0,
        'mae_pct': 0.0,
        'be_done': False,
        'submit_ms': armed.get('submit_ms'),
        'intended_px': armed.get('ob_top'),
    }


def on_smc_position_closed(coin: str, exit_px: float, exit_ts_ms: int):
    """Called by position_ledger when an SMC position closes."""
    coin = coin.upper()
    pos = state.positions.get(coin)
    if not pos or not pos.get('trade_id', '').startswith('smc-'):
        return

    if pos.get('forced_close'):
        ev = 'CLOSED_MARKET'
        reason = pos.get('close_reason') or 'forced'
    elif pos.get('be_done') and exit_px <= pos.get('sl_current', 0):
        ev = 'CLOSED_BE'
        reason = 'be_buffer_hit'
    elif exit_px <= pos.get('sl_orig', 0):
        ev = 'CLOSED_SL'
        reason = 'sl_hit'
    else:
        ev = 'CLOSED_TP'
        reason = 'tp_hit'

    smc_trade_log.log_close(pos, ev, exit_px, exit_ts_ms,
                            fees_usd=0,
                            funding_paid_usd=0,
                            source='ws_userFill',
                            reason=reason)
    state.positions.pop(coin, None)
    try:
        state_persist()
    except Exception:
        pass


# ---------------- Position management ----------------

def replace_sl(pos: dict, new_sl_px: float):
    """BE move: cancel old SL, place new at new_sl_px (reduce-only trigger)."""
    _ensure_hl()
    if _exchange is None:
        return
    coin = (pos.get('coin') or '').upper()
    trade_id = pos['trade_id']
    new_sl_px = round_price(coin, new_sl_px)

    # Cancel old SL by oid (precog.py pattern)
    old_sl_oid = pos.get('sl_oid')
    if old_sl_oid:
        try:
            flight_guard.acquire(coin)
            _exchange.cancel(coin, old_sl_oid)
        except Exception as e:
            log.warning(f"replace_sl cancel old SL oid={old_sl_oid} failed for {trade_id}: {e}")

    # Place new SL trigger with cloid '_S2' (uses atomic_entry's own builder for compatibility)
    new_cloid_obj = _make_cloid_obj(trade_id, '_S2')
    try:
        flight_guard.acquire(coin)
        resp = _exchange.order(
            coin,
            False,                       # closing long → sell
            float(pos['fill_size']),
            float(new_sl_px),
            {'trigger': {'isMarket': True, 'triggerPx': float(new_sl_px), 'tpsl': 'sl'}},
            reduce_only=True,
            cloid=new_cloid_obj,
        )
        # Capture new sl_oid for future cancels
        try:
            new_oid = ((resp or {}).get('response', {}) or {}) \
                .get('data', {}).get('statuses', [{}])[0].get('resting', {}).get('oid')
            if new_oid:
                pos['sl_oid'] = new_oid
        except Exception:
            pass
    except Exception as e:
        log.exception(f"replace_sl place new SL failed for {trade_id}: {e}")


# ---------------- atomic_reconciler callbacks ----------------

def reconciler_cancel(coin: str, oid):
    _ensure_hl()
    if _exchange is None:
        return
    try:
        flight_guard.acquire(coin.upper())
        _exchange.cancel(coin.upper(), oid)
    except Exception as e:
        log.warning(f"reconciler_cancel {coin}/{oid} failed: {e}")


def reconciler_place_sl(coin: str, is_long: bool, entry: float, size: float):
    """Place a fresh reduce-only SL trigger. Reconciler calls this after a partial fill."""
    _ensure_hl()
    if _exchange is None:
        return None
    coin = coin.upper()
    pos = state.positions.get(coin) or {}
    sl_px = pos.get('sl_current') or pos.get('sl_orig') or pos.get('sl_price')
    if not sl_px:
        log.warning(f"reconciler_place_sl: no SL price known for {coin}")
        return None
    sl_px = round_price(coin, float(sl_px))
    trade_id = pos.get('trade_id') or f"smc-recov-{int(time.time()*1000)}"
    cloid_obj = _make_cloid_obj(trade_id, '_Sr')
    try:
        flight_guard.acquire(coin)
        return _exchange.order(
            coin, not is_long, float(size), float(sl_px),
            {'trigger': {'isMarket': True, 'triggerPx': float(sl_px), 'tpsl': 'sl'}},
            reduce_only=True, cloid=cloid_obj,
        )
    except Exception as e:
        log.exception(f"reconciler_place_sl {coin} failed: {e}")
        return None


def reconciler_place_tp(coin: str, is_long: bool, entry: float, size: float):
    """Place a fresh reduce-only TP trigger."""
    _ensure_hl()
    if _exchange is None:
        return None
    coin = coin.upper()
    pos = state.positions.get(coin) or {}
    tp_px = pos.get('tp2') or pos.get('tp1')
    if not tp_px:
        log.warning(f"reconciler_place_tp: no TP price known for {coin}")
        return None
    tp_px = round_price(coin, float(tp_px))
    trade_id = pos.get('trade_id') or f"smc-recov-{int(time.time()*1000)}"
    cloid_obj = _make_cloid_obj(trade_id, '_Tr')
    try:
        flight_guard.acquire(coin)
        return _exchange.order(
            coin, not is_long, float(size), float(tp_px),
            {'trigger': {'isMarket': True, 'triggerPx': float(tp_px), 'tpsl': 'tp'}},
            reduce_only=True, cloid=cloid_obj,
        )
    except Exception as e:
        log.exception(f"reconciler_place_tp {coin} failed: {e}")
        return None


def reconciler_emergency_close(coin: str, reason: str):
    """Hard-flatten coin when reconciler cannot resync SL/TP."""
    coin = coin.upper()
    pos = state.positions.get(coin)
    if pos:
        close_market(pos, reason=f'reconciler:{reason}')
    else:
        # No SMC position; still try to flatten anything on the coin
        _ensure_hl()
        try:
            flight_guard.acquire(coin)
            _exchange.market_close(coin)
        except Exception as e:
            log.exception(f"reconciler_emergency_close {coin} failed: {e}")


def close_market(pos: dict, reason: str = 'MANUAL'):
    """Force market close. Used by time-stop or manual."""
    _ensure_hl()
    if _exchange is None:
        return
    coin = (pos.get('coin') or '').upper()
    pos['forced_close'] = True
    pos['close_reason'] = reason
    try:
        flight_guard.acquire(coin)
        _exchange.market_close(coin)
    except Exception as e:
        log.exception(f"close_market failed for {coin}: {e}")
    smc_trade_log.append({
        **pos,
        'event': 'CLOSED_MARKET',
        'reason': reason,
        'exit_time_ms': int(time.time() * 1000),
    })
