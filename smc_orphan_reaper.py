"""
smc_orphan_reaper.py — Reconcile HL open orders against state.armed.

Why this exists:
  state.armed is in-memory + persisted to /var/data/smc_state.json. If the
  Render disk is unmounted (or any crash mid-write), state is wiped on
  restart but the orders on HL persist. With limit_expiry_minutes=300 (5h)
  on the entry, a leaked order can sit on the book for 5h with no local
  tracker — and worse, fill into an untracked position that smc_monitors
  has no MFE/MAE/BE/time-stop oversight on.

What this does:
  Scans openOrders, identifies any SMC cloid (prefix '0x736d632d' = "smc-"),
  compares against state.armed entry/sl/tp oids. Any SMC cloid order whose
  oid is NOT in state.armed is treated as an orphan and cancelled.

Run cadence:
  - Once at boot (after state.load and after smc_fill_hook.install)
  - Every 10 min via smc_monitors scheduler

Safety:
  - Only touches orders with cloid starting '0x736d632d'. Other engines'
    orders (no cloid, or different prefix) are never touched.
  - Cancellations use flight_guard for write spacing.
"""
import logging
import time
import urllib.request
import urllib.parse
import json as _json

import flight_guard
from smc_state import state

log = logging.getLogger(__name__)

SMC_CLOID_PREFIX = '0x736d632d'  # ASCII "smc-"
HL_INFO_URL = 'https://api.hyperliquid.xyz/info'


def _hl_info(payload):
    body = _json.dumps(payload).encode()
    req = urllib.request.Request(HL_INFO_URL, data=body,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=8) as r:
        return _json.loads(r.read())


def _all_known_oids():
    """Collect every oid (entry/sl/tp) currently in state.armed + state.positions."""
    known = set()
    for armed in state.armed.values():
        for k in ('entry_oid', 'sl_oid', 'tp_oid'):
            v = armed.get(k)
            if v:
                known.add(int(v))
    for pos in state.positions.values():
        for k in ('entry_oid', 'sl_oid', 'tp_oid'):
            v = pos.get(k)
            if v:
                known.add(int(v))
    return known


def reap(exchange, wallet_addr, dry_run=False):
    """Cancel every SMC-cloid open order whose oid is unknown to state.

    Returns dict {scanned, smc_total, orphans, cancelled, errors}.
    """
    out = {'scanned': 0, 'smc_total': 0, 'orphans': 0,
           'cancelled': 0, 'errors': 0, 'detail': []}
    try:
        orders = _hl_info({'type': 'openOrders', 'user': wallet_addr})
    except Exception as e:
        log.exception(f"reaper: openOrders fetch failed: {e}")
        return out

    if not isinstance(orders, list):
        return out
    out['scanned'] = len(orders)

    known = _all_known_oids()
    smc_orders = [o for o in orders
                  if (o.get('cloid') or '').startswith(SMC_CLOID_PREFIX)]
    out['smc_total'] = len(smc_orders)

    for o in smc_orders:
        oid = o.get('oid')
        coin = o.get('coin')
        if not oid or not coin:
            continue
        if int(oid) in known:
            continue   # tracked, skip

        out['orphans'] += 1
        out['detail'].append({
            'coin': coin, 'oid': oid, 'cloid': o.get('cloid'),
            'side': o.get('side'), 'sz': o.get('sz'),
            'limitPx': o.get('limitPx'),
            'reduceOnly': o.get('reduceOnly'),
        })

        if dry_run:
            log.warning(f"reaper DRY: orphan {coin} oid={oid} cloid={o.get('cloid')} px={o.get('limitPx')}")
            continue

        try:
            flight_guard.acquire(coin)
            r = exchange.cancel(coin, int(oid))
            if (r or {}).get('status') == 'ok':
                out['cancelled'] += 1
                log.info(f"reaper: cancelled orphan {coin} oid={oid} cloid={o.get('cloid')}")
            else:
                out['errors'] += 1
                log.warning(f"reaper: cancel non-ok {coin} oid={oid}: {r}")
        except Exception as e:
            out['errors'] += 1
            log.warning(f"reaper: cancel {coin} oid={oid} err: {e}")

    if out['orphans']:
        log.warning(
            f"reaper: scanned={out['scanned']} smc={out['smc_total']} "
            f"orphans={out['orphans']} cancelled={out['cancelled']} errors={out['errors']}"
        )
    else:
        log.info(f"reaper: clean — scanned={out['scanned']} smc_tracked={out['smc_total']}")
    return out
