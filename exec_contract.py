"""Execution contract — enforces exit hierarchy and position safety.

PRIORITY 1 (ABSOLUTE): exchange-side TP/SL fills
PRIORITY 2 (EMERGENCY ONLY): kill switch
PRIORITY 3 (ADVISORY ONLY): signal reversal / regime / drift / dust

Usage:
  from exec_contract import (
      contract_close,           # ONLY sanctioned close wrapper
      can_close,                 # check if a close is permitted
      queue_reversal,            # record desire without closing
      get_queued_reversal,       # check queue after TP/SL resolves
      clear_reversal_queue,
      ensure_tp_sl_placed,       # verify both placed post-entry
      get_fallback_config,       # provide TP/SL for non-elite coins
  )

All existing close(coin) call sites must migrate to contract_close() with
one of the enumerated AUTHORIZED_REASONS. Unauthorized reasons raise an
exception (caught by caller) and the close is REJECTED.
"""
import os, json, time, threading
from collections import defaultdict

# Authorized reasons for close()
AUTHORIZED_REASONS = {
    # PRIORITY 1 — exchange-side
    'tp_fill_confirmed',    # HL TP trigger order was filled on exchange
    'sl_fill_confirmed',    # HL SL trigger order was filled on exchange

    # PRIORITY 2 — catastrophic only
    'kill_switch_manual',   # operator-initiated kill via /close/* endpoint
    'kill_switch_cb',       # circuit-breaker catastrophic loss streak
    'kill_switch_liq',      # imminent liquidation (distance < buffer)

    # INIT FAILURE (special case): if entry opens but TP/SL both fail,
    # immediate emergency close is authorized to prevent a naked position
    'init_tp_sl_failure',
}

# ADVISORY REASONS — must NOT call close, only queue
ADVISORY_REASONS = {
    'signal_reversal',
    'regime_change',
    'drift_detection',
    'dust_sweep',
    'trail_exit',
    'tp_lock_exit',
    'funding_cut',
    'max_hold',
    'long_drift_loss',
    'wall_exit',
    'profit_lock',
}

# Reversal queue: coin → {desired_side, queued_at, reason}
_REVERSAL_QUEUE = {}
_QUEUE_LOCK = threading.Lock()

# Fallback configs for coins not in percoin_configs elite list.
# These are conservative defaults — 5% TP, 2.5% SL (1:2 R:R).
# The goal is NOT optimal sizing; the goal is EXCHANGE PROTECTION.
_FALLBACK_TP_PCT = 0.05
_FALLBACK_SL_PCT = 0.025

# Audit log of rejected closes
_REJECTED_CLOSES = []
_REJECTED_LOCK = threading.Lock()

# Contract violation counters (monitoring)
_VIOLATIONS = defaultdict(int)


def get_fallback_config(coin):
    """Provide TP/SL config for ANY coin, not just elite.

    This breaks the `is_elite()` gate that caused 39% of trades today to
    be opened without exchange-side TP/SL.
    """
    return {
        'TP': _FALLBACK_TP_PCT,
        'SL': _FALLBACK_SL_PCT,
        'source': 'fallback_contract',
    }


def can_close(reason):
    """Check if a close() invocation is permitted.

    Returns True only for authorized reasons. Advisory reasons return False
    and the caller must instead call queue_reversal().
    """
    if reason in AUTHORIZED_REASONS:
        return True
    _VIOLATIONS[f'attempted_{reason}'] += 1
    return False


def contract_close(close_fn, coin, reason, **context):
    """ENFORCED close wrapper. All close() call sites must migrate to this.

    Args:
        close_fn: the underlying close() function (dependency-injected to
                  avoid circular imports)
        coin: coin to close
        reason: one of AUTHORIZED_REASONS
        **context: logging context (position info, engine, etc.)

    Returns:
        pnl_pct on success, None on authorization failure
    """
    if not can_close(reason):
        with _REJECTED_LOCK:
            _REJECTED_CLOSES.append({
                'ts': int(time.time()),
                'coin': coin,
                'reason': reason,
                'context': context,
                'verdict': 'REJECTED (advisory reason — use queue_reversal)',
            })
            if len(_REJECTED_CLOSES) > 500:
                _REJECTED_CLOSES[:] = _REJECTED_CLOSES[-500:]
        print(f"[contract] REJECT close({coin}) reason={reason} "
              f"— not in AUTHORIZED_REASONS. Use queue_reversal().",
              flush=True)
        return None

    # Authorized — proceed
    try:
        pnl = close_fn(coin)
        print(f"[contract] AUTHORIZED close({coin}) reason={reason} pnl={pnl}",
              flush=True)
        return pnl
    except Exception as e:
        print(f"[contract] close({coin}) exec err: {e}", flush=True)
        raise


def queue_reversal(coin, desired_side, advisory_reason):
    """Record intent to reverse without actually closing.

    The reversal will execute AFTER the current position resolves via TP/SL
    (PRIORITY 1) or kill switch (PRIORITY 2). Checked at next signal cycle.

    Args:
        coin: coin with open position
        desired_side: 'BUY' or 'SELL' (the new direction)
        advisory_reason: one of ADVISORY_REASONS (for logging)
    """
    if advisory_reason not in ADVISORY_REASONS:
        print(f"[contract] queue_reversal unknown reason: {advisory_reason}",
              flush=True)
        advisory_reason = 'unknown_advisory'

    with _QUEUE_LOCK:
        _REVERSAL_QUEUE[coin] = {
            'desired_side': desired_side,
            'queued_at': int(time.time()),
            'advisory_reason': advisory_reason,
        }
    print(f"[contract] QUEUED reversal {coin} → {desired_side} "
          f"(advisory: {advisory_reason}). Will fire after TP/SL resolves.",
          flush=True)


def get_queued_reversal(coin):
    """Retrieve queued reversal intent for coin, if any."""
    with _QUEUE_LOCK:
        return _REVERSAL_QUEUE.get(coin)


def clear_reversal_queue(coin):
    """Called after a position closes via TP/SL. Permits reversal to fire."""
    with _QUEUE_LOCK:
        return _REVERSAL_QUEUE.pop(coin, None)


def ensure_tp_sl_placed(coin, tp_pct_used, sl_pct_used, close_fn):
    """Contract assertion post-entry: BOTH TP and SL must be on exchange.

    If either is None (placement failed or gated out), immediately close
    the position. A naked position is a contract violation and must not
    persist.

    Args:
        coin: coin just opened
        tp_pct_used: return value of place_native_tp (None = failed)
        sl_pct_used: return value of place_native_sl (None = failed)
        close_fn: reference to close() for emergency action

    Returns:
        True if contract satisfied, False if emergency close was triggered
    """
    if tp_pct_used is not None and sl_pct_used is not None:
        return True

    missing = []
    if tp_pct_used is None: missing.append('TP')
    if sl_pct_used is None: missing.append('SL')

    _VIOLATIONS['naked_position_attempted'] += 1
    print(f"[contract] ★★★ NAKED POSITION DETECTED {coin}: missing {missing}. "
          f"EMERGENCY CLOSE.", flush=True)

    # Emergency close authorized under init_tp_sl_failure
    try:
        pnl = contract_close(close_fn, coin, 'init_tp_sl_failure',
                             missing=missing)
        return False
    except Exception as e:
        print(f"[contract] emergency close failed {coin}: {e}", flush=True)
        return False


def status():
    """Return contract enforcement state."""
    with _QUEUE_LOCK:
        q = dict(_REVERSAL_QUEUE)
    with _REJECTED_LOCK:
        r = list(_REJECTED_CLOSES[-20:])
    return {
        'authorized_reasons': sorted(AUTHORIZED_REASONS),
        'advisory_reasons': sorted(ADVISORY_REASONS),
        'fallback_tp_pct': _FALLBACK_TP_PCT,
        'fallback_sl_pct': _FALLBACK_SL_PCT,
        'reversal_queue': q,
        'rejected_closes_recent': r,
        'violations_counters': dict(_VIOLATIONS),
        'contract_version': '1.0',
    }
