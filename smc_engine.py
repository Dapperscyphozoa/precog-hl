"""
smc_engine.py — 13-gate alert handler.

Receives Pine webhook payloads, runs gate sequence, hands accepted alerts
to smc_execution.submit_smc_trade.
"""
import os
import time
import logging
from datetime import datetime, timezone

import smc_trade_log
import smc_pl_compat
from smc_config import SMC_CONFIG, REQUIRED_PAYLOAD_FIELDS
from smc_state import state, persist as state_persist

log = logging.getLogger(__name__)

WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')


# ---------------- Helpers ----------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _in_skip_session() -> bool:
    h = datetime.now(timezone.utc).hour
    lo, hi = SMC_CONFIG['skip_session_utc']
    return lo <= h < hi


def _utc_hour() -> int:
    return datetime.now(timezone.utc).hour


def _coin_armed(coin: str) -> bool:
    return any(a.get('coin') == coin for a in state.armed.values())


def _smc_position_count() -> int:
    return sum(
        1 for p in state.positions.values()
        if p.get('trade_id', '').startswith('smc-')
    )


def _funding_rate_for(coin: str) -> float:
    cache = state.funding_cache.get(coin) or {}
    return float(cache.get('rate_per_hour', 0) or 0)


def dedupe_check(alert_id: str) -> bool:
    """Return True if alert_id was seen within dedupe_window. Records seen ts."""
    now = _now_ms()
    window_ms = SMC_CONFIG['dedupe_window_seconds'] * 1000
    # purge old
    state.recent_alert_ids = {
        k: v for k, v in state.recent_alert_ids.items() if now - v < window_ms
    }
    if alert_id in state.recent_alert_ids:
        return True
    state.recent_alert_ids[alert_id] = now
    return False


def _build_decision_context() -> dict:
    return {
        'btc_trend_up': state.btc_trend_up,
        'btc_trend_age_min': (
            (time.time() - state.btc_trend_updated_ms / 1000) / 60
            if state.btc_trend_updated_ms else None
        ),
        'funding_rate': None,            # filled per-coin during gate 10
        'session_utc_hour': _utc_hour(),
        'concurrent_positions': _smc_position_count(),
        'equity_at_decision': smc_pl_compat.get_equity(),
    }


def _gate_value(num: int, payload: dict, ctx: dict):
    """Best-effort capture of the value that failed each gate (for log)."""
    if num == 1:  return 'secret_present' if payload.get('secret') else 'missing'
    if num == 2:  return [f for f in REQUIRED_PAYLOAD_FIELDS if f not in payload]
    if num == 3:  return 'within_dedupe_window'
    if num == 4:  return payload.get('side')
    if num == 6:  return payload.get('coin')
    if num == 8:  return payload.get('rr_to_tp2')
    if num == 9:  return state.btc_trend_up
    if num == 10: return ctx.get('funding_rate')
    if num == 11: return ctx.get('concurrent_positions')
    if num == 12: return payload.get('coin')
    if num == 13: return payload.get('coin')
    return None


def pushover_alert(message: str):
    token = os.environ.get('PUSHOVER_TOKEN')
    user = os.environ.get('PUSHOVER_USER')
    if not (token and user):
        log.warning(f"PUSHOVER not configured. msg='{message}'")
        return
    try:
        import requests
        requests.post('https://api.pushover.net/1/messages.json', data={
            'token': token, 'user': user, 'message': message,
        }, timeout=5)
    except Exception as e:
        log.warning(f"pushover_alert failed: {e}")


def _normalize_coin(raw: str) -> str:
    """TV ticker → HL coin. BINANCE:LINKUSDT → LINK, OKX:JUPUSDT → JUP, 1000PEPEUSDT.P → 1000PEPE."""
    if not raw:
        return raw
    if ':' in raw:
        raw = raw.split(':', 1)[1]
    if raw.endswith('.P'):
        raw = raw[:-2]
    for suf in ('USDT', 'USDC', 'USD'):
        if raw.endswith(suf):
            raw = raw[:-len(suf)]
            break
    # HL k-prefix coins (kNEIRO, kBONK, kPEPE, kFLOKI, kSHIB, kDOGS) keep lowercase k
    if len(raw) > 1 and raw[0] == 'k' and raw[1].isupper():
        return raw
    return raw.upper()


# ---------------- Public API ----------------

def handle_smc_alert(payload: dict):
    """
    Returns (response_dict, http_status_int).
    Logs ALERT_RECV unconditionally; logs GATE_FAIL on first failed gate.
    """
    webhook_recv_ms = _now_ms()
    payload['webhook_recv_ms'] = webhook_recv_ms

    # Normalize coin (TV ticker → HL coin)
    if payload.get('coin'):
        payload['coin'] = _normalize_coin(payload['coin'])

    smc_trade_log.log_alert_recv(payload, webhook_recv_ms)
    state.last_alert_ms = webhook_recv_ms

    ctx = _build_decision_context()

    # Pre-load funding for the coin into ctx
    if payload.get('coin'):
        ctx['funding_rate'] = _funding_rate_for(payload['coin'])

    gates = [
        (1,  'webhook_secret', lambda: payload.get('secret') == WEBHOOK_SECRET),
        (2,  'schema',         lambda: all(f in payload for f in REQUIRED_PAYLOAD_FIELDS)),
        (3,  'dedupe',         lambda: not dedupe_check(payload['alert_id'])),
        (4,  'short_signal',   lambda: payload.get('side') != 'SELL'),
        (6,  'major_excluded', lambda: payload['coin'] not in SMC_CONFIG['excluded_majors']),
        (8,  'rr_min',         lambda: float(payload.get('rr_to_tp2', 0)) >= SMC_CONFIG['min_rr_to_take']),
        (9,  'btc_trend',      lambda: bool(state.btc_trend_up)),
        (10, 'funding_max',    lambda: ctx['funding_rate'] < SMC_CONFIG['funding_max_adverse_per_hour']),
        (11, 'position_cap',   lambda: _smc_position_count() < SMC_CONFIG['max_concurrent_positions']),
        (12, 'coin_open',      lambda: payload['coin'] not in state.positions),
        (13, 'coin_armed',     lambda: not _coin_armed(payload['coin'])),
    ]

    for num, name, check in gates:
        try:
            ok = check()
        except Exception as e:
            smc_trade_log.log_gate_fail(payload, num, name, str(e), ctx)
            return {'status': 'gate_error', 'gate': name, 'error': str(e)}, 500

        if ok:
            continue

        # Gate failed
        value = _gate_value(num, payload, ctx)
        smc_trade_log.log_gate_fail(payload, num, name, value, ctx)

        # Gate 4: short signal rejected
        if num == 4:
            pushover_alert(
                f"SMC: SHORT signal rejected on {payload.get('coin')} "
                f"({payload.get('alert_id')})"
            )

        return {'status': f'gate_fail_{name}', 'gate': num, 'value': value}, 200

    # All gates passed
    smc_trade_log.append({
        'event': 'GATE_PASS',
        'alert_id': payload.get('alert_id'),
        'coin': payload.get('coin'),
        'side': payload.get('side'),
    })

    # Hand off to execution module
    try:
        from smc_execution import submit_smc_trade
        return submit_smc_trade(payload, ctx)
    except Exception as e:
        log.exception("submit_smc_trade raised")
        return {'status': 'submit_exception', 'error': str(e)}, 500
