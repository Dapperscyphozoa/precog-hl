"""
atomic_entry.py — Atomic entry+SL+TP via HL bulk_orders.

Ends the "fill before SL exists" race condition. Three orders (entry,
SL trigger, TP trigger) submit in ONE API call. Either all three accept
or none — there's no window where the entry can fill before the brackets
are placed.

HL bulk_orders contract:
  exchange.bulk_orders([order1, order2, order3])
  Each order is a dict per HL Python SDK:
    {
      "coin":    "BTC",
      "is_buy":  True/False,
      "sz":      float,
      "limit_px": float,
      "order_type": {...},     # see below
      "reduce_only": bool,
      "cloid":   Cloid_obj or None,
    }

Order types we use:
  ENTRY (limit IOC):
    {"limit": {"tif": "Ioc"}}
  ENTRY (limit ALO maker):
    {"limit": {"tif": "Alo"}}
  SL trigger market:
    {"trigger": {"isMarket": True, "triggerPx": <px>, "tpsl": "sl"}}
  TP trigger market:
    {"trigger": {"isMarket": True, "triggerPx": <px>, "tpsl": "tp"}}

Trigger orders with tpsl="sl"/"tp" + reduce_only=True are HL's native
"position-attached" stops. They activate when a position exists and the
trigger price is breached. If the entry doesn't fill, the resting triggers
sit harmlessly with reduce_only=True (cannot open a position).

Failure modes & handling:
  - Whole bulk fails (e.g. tpsl px crossed at submission, leverage cap):
      → no orders rest. Ledger stays EMPTY.
  - Entry rejected, triggers accepted:
      → orphan triggers. We cancel them in the cleanup path.
  - Entry IOC fills partial, triggers full size:
      → triggers cover up to their full sz; if position is smaller than
        trigger sz, that's fine — reduce_only caps at actual position.

Cloid scheme:
  cloid_base = trade_id from signal layer (e.g. "BTC_BUY_1714058400")
  Entry cloid:  base + "_E"
  SL cloid:     base + "_S"
  TP cloid:     base + "_T"
  These map back to ledger rows on userFills events.

Slippage policy:
  ENTRY:  IOC limit at slippage % (configurable, default 0.5%).
          Aggressive enough to fill in normal liquidity; if it doesn't,
          the bulk effectively no-ops (ledger stays clean, retry next signal).
  SL:     market trigger at sl_px. Slips through book on activation.
  TP:     market trigger at tp_px. Same.
"""

import logging
import os
import time

import position_ledger

log = logging.getLogger("atomic_entry")

DEFAULT_SLIP = 0.005  # 0.5% IOC entry slippage cap


def _build_cloid(base, suffix):
    """Produce a 16-byte hex cloid string suitable for HL's Cloid.from_str.
    Caller wraps with hyperliquid.utils.types.Cloid."""
    raw = (base + suffix).encode().hex()[:32].ljust(32, '0')
    return '0x' + raw


def _make_cloid_obj(base, suffix):
    """Build a hyperliquid Cloid object. Returns None if SDK not available."""
    try:
        from hyperliquid.utils.types import Cloid
        return Cloid.from_str(_build_cloid(base, suffix))
    except Exception:
        return None


def submit_atomic(exchange, coin, is_buy, size, mark_px, sl_px, tp_px,
                  trade_id, slip_pct=DEFAULT_SLIP, log_fn=None,
                  price_rounder=None):
    """Submit entry+SL+TP atomically via bulk_orders.

    price_rounder: optional callable (coin, px) -> px. Used to enforce HL
    tick-size compliance on `entry_limit_px` (computed internally as
    mark_px*(1±slip_pct)). Without it, multiplications like 0.003654*1.005
    produce floats with too many sig figs (0.0036772949999999997) and HL
    rejects with `float_to_wire causes rounding`. Passing precog.round_price
    fixes the entire small-tick coin universe (LINEA, kFLOKI, etc.).
    Defaults to identity for backward compatibility.

    Returns dict:
      {
        'success':     bool,
        'reason':      str,                  # error code if not success
        'fill_px':     float or None,        # if entry filled in IOC
        'entry_oid':   int or None,          # resting oid (None if filled instantly)
        'sl_oid':      int or None,
        'tp_oid':      int or None,
        'raw':         <bulk_orders response>,
      }

    On success, the position_ledger row for `coin` is initialized to
    PENDING_ENTRY (or LIVE if fill returned in the response).
    """
    out = {
        'success': False, 'reason': None, 'fill_px': None,
        'entry_oid': None, 'sl_oid': None, 'tp_oid': None, 'raw': None,
    }
    log_ = log_fn or log.info

    # ─── Sanity gates ─────────────────────────────────────────────────
    if size <= 0:
        out['reason'] = 'size_zero'; return out
    if mark_px <= 0 or sl_px <= 0 or tp_px <= 0:
        out['reason'] = 'invalid_px'; return out
    if is_buy and not (sl_px < mark_px < tp_px):
        out['reason'] = 'long_bracket_invalid'
        log_(f"atomic_entry {coin} LONG bracket invalid: sl={sl_px} mark={mark_px} tp={tp_px}")
        return out
    if (not is_buy) and not (tp_px < mark_px < sl_px):
        out['reason'] = 'short_bracket_invalid'
        log_(f"atomic_entry {coin} SHORT bracket invalid: tp={tp_px} mark={mark_px} sl={sl_px}")
        return out

    # ─── Build orders ─────────────────────────────────────────────────
    # Entry IOC — slipped 0.5% in direction of trade for fill confidence
    entry_limit_px = mark_px * (1 + slip_pct) if is_buy else mark_px * (1 - slip_pct)
    if price_rounder is not None:
        try:
            entry_limit_px = float(price_rounder(coin, entry_limit_px))
        except Exception as _re:
            log_(f"atomic_entry {coin} price_rounder failed (non-fatal): {_re}")

    cloid_entry = _make_cloid_obj(trade_id, '_E')
    cloid_sl    = _make_cloid_obj(trade_id, '_S')
    cloid_tp    = _make_cloid_obj(trade_id, '_T')

    orders = [
        # 0: entry
        {
            "coin": coin,
            "is_buy": bool(is_buy),
            "sz": float(size),
            "limit_px": float(entry_limit_px),
            "order_type": {"limit": {"tif": "Ioc"}},
            "reduce_only": False,
            "cloid": cloid_entry,
        },
        # 1: SL trigger market (opposite side, reduce_only)
        {
            "coin": coin,
            "is_buy": (not bool(is_buy)),
            "sz": float(size),
            "limit_px": float(sl_px),  # required field; market trigger ignores
            "order_type": {
                "trigger": {
                    "isMarket": True,
                    "triggerPx": float(sl_px),
                    "tpsl": "sl",
                }
            },
            "reduce_only": True,
            "cloid": cloid_sl,
        },
        # 2: TP trigger (opposite side, reduce_only)
        # 2026-04-28: TP maker mode (env TP_MAKER_MODE, default 1) — limit
        # order at tp_px, rests on book as maker. Saves ~0.045% taker fee
        # + earns rebate per TP fill. Set TP_MAKER_MODE=0 to revert.
        # SL stays as market trigger (execution certainty preserved).
        {
            "coin": coin,
            "is_buy": (not bool(is_buy)),
            "sz": float(size),
            "limit_px": float(tp_px),
            "order_type": {
                "trigger": {
                    "isMarket": (os.environ.get('TP_MAKER_MODE', '1') != '1'),
                    "triggerPx": float(tp_px),
                    "tpsl": "tp",
                }
            },
            "reduce_only": True,
            "cloid": cloid_tp,
        },
    ]

    # ─── Submit ───────────────────────────────────────────────────────
    try:
        resp = exchange.bulk_orders(orders)
        out['raw'] = resp
    except Exception as e:
        out['reason'] = f"bulk_exception:{type(e).__name__}:{str(e)[:120]}"
        log_(f"atomic_entry {coin} bulk_orders raised: {e}")
        return out

    # ─── Parse response ───────────────────────────────────────────────
    # HL response shape:
    #   {"status":"ok","response":{"type":"order","data":{"statuses":[
    #       {"resting":{"oid":...}} or {"filled":{"totalSz":...,"avgPx":...,"oid":...}}
    #       or {"error":"..."},
    #       ... 3 entries
    #   ]}}}
    if not isinstance(resp, dict) or resp.get('status') != 'ok':
        out['reason'] = f"bulk_not_ok:{str(resp)[:200]}"
        log_(f"atomic_entry {coin} bulk_orders response not ok: {str(resp)[:200]}")
        return out
    statuses = ((resp.get('response') or {}).get('data') or {}).get('statuses') or []
    if len(statuses) != 3:
        out['reason'] = f"bulk_bad_status_count:{len(statuses)}"
        log_(f"atomic_entry {coin} expected 3 statuses, got {len(statuses)}: {statuses}")
        return out

    entry_st, sl_st, tp_st = statuses

    # Entry — must be filled or resting (not error)
    if 'error' in entry_st:
        out['reason'] = f"entry_error:{entry_st['error']}"
        log_(f"atomic_entry {coin} entry rejected: {entry_st['error']}")
        # SL/TP may still have been accepted as resting — caller cleanup
        if 'resting' in sl_st: out['sl_oid'] = sl_st['resting'].get('oid')
        if 'resting' in tp_st: out['tp_oid'] = tp_st['resting'].get('oid')
        return out
    if 'filled' in entry_st:
        f = entry_st['filled']
        out['entry_oid'] = f.get('oid')
        out['fill_px']   = float(f.get('avgPx') or 0) or None
    elif 'resting' in entry_st:
        # Shouldn't happen with IOC, but handle defensively
        out['entry_oid'] = entry_st['resting'].get('oid')
    else:
        out['reason'] = f"entry_unknown_status:{entry_st}"
        log_(f"atomic_entry {coin} entry unknown status: {entry_st}")
        return out

    # SL — should be resting; error tolerated (caller may retry just SL)
    if 'error' in sl_st:
        log_(f"atomic_entry {coin} SL rejected: {sl_st['error']}")
    elif 'resting' in sl_st:
        out['sl_oid'] = sl_st['resting'].get('oid')
    elif 'filled' in sl_st:
        # Shouldn't happen for resting trigger but accept
        out['sl_oid'] = sl_st['filled'].get('oid')

    # TP — same logic
    if 'error' in tp_st:
        log_(f"atomic_entry {coin} TP rejected: {tp_st['error']}")
    elif 'resting' in tp_st:
        out['tp_oid'] = tp_st['resting'].get('oid')
    elif 'filled' in tp_st:
        out['tp_oid'] = tp_st['filled'].get('oid')

    # ─── Update ledger ────────────────────────────────────────────────
    try:
        position_ledger.begin_pending(
            coin=coin, is_long=is_buy, size=size,
            entry_px=out['fill_px'] or mark_px,
            sl_px=sl_px, tp_px=tp_px,
            cloid_entry=str(cloid_entry) if cloid_entry else None,
            cloid_sl=str(cloid_sl) if cloid_sl else None,
            cloid_tp=str(cloid_tp) if cloid_tp else None,
            entry_oid=out['entry_oid'],
            sl_oid=out['sl_oid'],
            tp_oid=out['tp_oid'],
            # PROVISIONAL — atomic_reconciler will compare actual fill
            # against intent_size and either CONFIRM or RESIZE the
            # SL/TP brackets within ~1-2s of webData2 ticking.
            protection_state='PROVISIONAL',
        )
        # If the entry already filled (IOC), simulate a fill event
        # locally — WS userFills will hit too, but this is idempotent
        # and removes a race window where ledger says PENDING but
        # the position is already LIVE.
        if out['fill_px']:
            position_ledger.on_fill(
                coin=coin,
                side='B' if is_buy else 'A',
                sz=size,
                px=out['fill_px'],
                ts_ms=int(time.time() * 1000),
                oid=out['entry_oid'],
                cloid=str(cloid_entry) if cloid_entry else None,
            )
    except Exception as le:
        log_(f"atomic_entry {coin} ledger update err (non-fatal): {le}")

    # Success requires entry filled/resting AND BOTH brackets resting
    if out['fill_px'] is None and out['entry_oid'] is None:
        out['reason'] = 'entry_no_fill_no_oid'
        return out
    if out['sl_oid'] is None and out['tp_oid'] is None:
        out['reason'] = 'no_brackets'
        log_(f"atomic_entry {coin} both brackets failed — entry filled "
             f"but unprotected; caller should emergency close")
        return out

    out['success'] = True
    return out
