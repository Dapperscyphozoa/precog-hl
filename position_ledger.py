"""
position_ledger.py — Thread-safe in-memory position ledger.

Single source of truth for position state in the hot execution path. Replaces
the REST-driven _ep_fetch_size() / info.user_state() calls that were hitting
CloudFront 429 cascades during the cancel/replace/grace cycle.

State machine per coin:

    EMPTY (no row)
        ↓ atomic_entry submitted
    PENDING_ENTRY (bulk_orders sent, no fill yet, sl_oid/tp_oid known from response)
        ↓ WS userFills event for entry cloid
    LIVE (entry confirmed; size, entry_px, sl_oid, tp_oid all populated)
        ↓ size goes to 0 via WS webData2
    CLOSED → row removed

    PENDING_ENTRY also has a timeout path:
        ↓ no fill within entry_timeout_sec
    FAILED_ENTRY (logged, row removed; cleanup of any resting SL/TP is caller's job)

Concurrency:
    - All accessors take a single RLock
    - Writes from WS feeder thread (hl_user_ws)
    - Reads from main tick path + execution path
    - get_size(), get_protection() are O(1) dict lookups — no I/O

Authority order (when WS and bulk_orders response disagree):
    WS event > bulk_orders response > REST reconciliation
    (WS is closest to truth — exchange's broadcast-after-commit signal)
"""

import time
import threading
from collections import defaultdict


# ─── State enum (string for JSON friendliness) ────────────────────────
EMPTY         = 'EMPTY'
PENDING_ENTRY = 'PENDING_ENTRY'
LIVE          = 'LIVE'
FAILED_ENTRY  = 'FAILED_ENTRY'
CLOSED        = 'CLOSED'


class PositionLedger:
    def __init__(self):
        self._lock = threading.RLock()
        # coin (uppercase) -> dict
        self._rows = {}
        # WS feed health
        self._last_ws_msg_ts = 0.0
        self._ws_connected = False
        self.stats = {
            'pending_created':  0,
            'live_transitions': 0,
            'failed_entries':   0,
            'closed':           0,
            'ws_msgs':          0,
            'ws_reconnects':    0,
        }

    # ─── Mutators (called by dispatcher / WS feeder) ──────────────────
    def begin_pending(self, coin, is_long, size, entry_px, sl_px, tp_px,
                      cloid_entry, cloid_sl=None, cloid_tp=None,
                      sl_oid=None, tp_oid=None, entry_oid=None,
                      protection_state='PROVISIONAL'):
        """Called by atomic_entry immediately after bulk_orders submission.

        Records intent + resting trigger oids returned in the bulk response.
        Entry fill arrives later via WS, transitioning PENDING → LIVE.

        protection_state lifecycle (set by atomic_reconciler):
          PROVISIONAL  → atomic placed, SL/TP sized to intent_size (may be wrong)
          CONFIRMED    → actual fill matches intent within tolerance
          RESIZED      → actual ≠ intent; SL/TP cancelled + replaced with correct size
          RECONCILE_FAIL → reconciliation failed; emergency close fired
          CONFIRMED is also the default state for non-atomic (legacy) entries
          where enforce_protection synchronously verified post-fill size.
        """
        coin = coin.upper()
        now = time.time()
        with self._lock:
            self._rows[coin] = {
                'state':      PENDING_ENTRY,
                'is_long':    bool(is_long),
                'size':       0.0,                  # not filled yet
                'intent_size': float(size),
                'entry_px':   None,                 # actual fill price
                'intent_entry_px': float(entry_px),
                'sl_px':      float(sl_px),
                'tp_px':      float(tp_px),
                'entry_oid':  entry_oid,
                'sl_oid':     sl_oid,
                'tp_oid':     tp_oid,
                'cloid_entry': cloid_entry,
                'cloid_sl':   cloid_sl,
                'cloid_tp':   cloid_tp,
                'created_ts': now,
                'updated_ts': now,
                'fills':      [],   # list of (ts_ms, side, sz, px, oid, cloid)
                'protection_state': protection_state,
            }
            self.stats['pending_created'] += 1

    def set_protection_state(self, coin, new_state, reason=None):
        """Update protection_state. Returns True if row exists, else False.
        Valid states: PROVISIONAL, CONFIRMED, RESIZED, RECONCILE_FAIL."""
        coin = coin.upper()
        with self._lock:
            row = self._rows.get(coin)
            if not row:
                return False
            row['protection_state'] = new_state
            if reason:
                row['protection_reason'] = reason
            row['updated_ts'] = time.time()
            return True

    def get_protection_state(self, coin):
        """Returns current protection_state or None if no row."""
        coin = coin.upper()
        with self._lock:
            row = self._rows.get(coin)
            return row.get('protection_state') if row else None

    def on_fill(self, coin, side, sz, px, ts_ms, oid=None, cloid=None):
        """WS userFills event. Side is 'B' or 'A' (Ask=Sell).
        Buy fill on PENDING long → LIVE.
        Sell fill on LIVE long → reduces size; size=0 → CLOSED.
        """
        coin = coin.upper()
        now = time.time()
        with self._lock:
            row = self._rows.get(coin)
            if row is None:
                # Unknown coin — just log it as orphan fill (still accept)
                self._rows[coin] = {
                    'state':      LIVE if side == 'B' else CLOSED,
                    'is_long':    side == 'B',
                    'size':       float(sz) if side == 'B' else 0.0,
                    'intent_size': float(sz),
                    'entry_px':   float(px) if side == 'B' else None,
                    'intent_entry_px': float(px),
                    'sl_px':      None, 'tp_px': None,
                    'entry_oid':  oid,
                    'sl_oid':     None, 'tp_oid': None,
                    'cloid_entry': cloid, 'cloid_sl': None, 'cloid_tp': None,
                    'created_ts': now, 'updated_ts': now,
                    'fills':      [(ts_ms, side, float(sz), float(px), oid, cloid)],
                    'orphan':     True,
                }
                return
            row['fills'].append((ts_ms, side, float(sz), float(px), oid, cloid))
            row['updated_ts'] = now
            is_long = row['is_long']
            entry_side = 'B' if is_long else 'A'
            if side == entry_side:
                # Adding to position (entry or pyramid)
                old_sz = row['size']
                new_sz = old_sz + float(sz)
                # VWAP entry price
                if old_sz <= 0:
                    row['entry_px'] = float(px)
                else:
                    row['entry_px'] = ((old_sz * row['entry_px']) +
                                       (float(sz) * float(px))) / new_sz
                row['size'] = new_sz
                if row['state'] == PENDING_ENTRY:
                    row['state'] = LIVE
                    self.stats['live_transitions'] += 1
            else:
                # Reducing position
                row['size'] = max(0.0, row['size'] - float(sz))
                if row['size'] < 1e-12:
                    row['state'] = CLOSED
                    self.stats['closed'] += 1

    def on_webdata2(self, asset_positions, open_orders):
        """webData2 snapshot from WS. Reconciles size + tracks open trigger oids.

        Authority: WS snapshot wins over our internal computed state for SIZE,
        but we preserve sl_oid/tp_oid we know about + cloid mapping.

        asset_positions: list from msg['data']['clearinghouseState']['assetPositions']
        open_orders: list from msg['data']['openOrders'] or similar
        """
        now = time.time()
        with self._lock:
            self._last_ws_msg_ts = now
            self.stats['ws_msgs'] += 1
            # Snapshot of all coins with non-zero size
            ws_positions = {}
            for ap in asset_positions or []:
                p = ap.get('position') or {}
                coin = (p.get('coin') or '').upper()
                if not coin: continue
                szi = float(p.get('szi') or 0)
                if abs(szi) < 1e-12: continue
                ws_positions[coin] = {
                    'is_long': szi > 0,
                    'size':    abs(szi),
                    'entry_px': float(p.get('entryPx') or 0) or None,
                }
            # Update existing rows from snapshot
            for coin, row in list(self._rows.items()):
                ws = ws_positions.get(coin)
                if ws is None:
                    # No position on exchange — if we thought we had one, close it
                    if row['state'] in (LIVE, PENDING_ENTRY):
                        if row['state'] == PENDING_ENTRY and \
                           (now - row['created_ts']) < 30:
                            # Still within entry timeout; don't close yet
                            continue
                        row['state'] = CLOSED
                        row['size'] = 0.0
                        row['updated_ts'] = now
                        self.stats['closed'] += 1
                else:
                    # Position exists — sync size + entry_px
                    row['size'] = ws['size']
                    if ws['entry_px']:
                        row['entry_px'] = ws['entry_px']
                    row['is_long'] = ws['is_long']
                    if row['state'] == PENDING_ENTRY:
                        row['state'] = LIVE
                        self.stats['live_transitions'] += 1
                    row['updated_ts'] = now
            # Find positions on exchange we don't know about (orphans)
            for coin, ws in ws_positions.items():
                if coin not in self._rows:
                    self._rows[coin] = {
                        'state':      LIVE,
                        'is_long':    ws['is_long'],
                        'size':       ws['size'],
                        'intent_size': ws['size'],
                        'entry_px':   ws['entry_px'],
                        'intent_entry_px': ws['entry_px'],
                        'sl_px':      None, 'tp_px': None,
                        'entry_oid':  None,
                        'sl_oid':     None, 'tp_oid': None,
                        'cloid_entry': None, 'cloid_sl': None, 'cloid_tp': None,
                        'created_ts': now, 'updated_ts': now,
                        'fills':      [],
                        'orphan':     True,
                        # Orphans from webData2 (non-atomic legacy entries OR
                        # orphan recovery): mark CONFIRMED so atomic_reconciler
                        # never touches them. Only atomic_entry sets PROVISIONAL.
                        'protection_state': 'CONFIRMED',
                    }
            # Update trigger oids from open orders (stays even when position size syncs)
            for o in open_orders or []:
                coin = (o.get('coin') or '').upper()
                if not coin or coin not in self._rows: continue
                row = self._rows[coin]
                if not o.get('isTrigger'): continue
                tpsl = (o.get('tpsl') or '').lower()
                oid = o.get('oid')
                if tpsl == 'sl': row['sl_oid'] = oid
                elif tpsl == 'tp': row['tp_oid'] = oid

    def on_order_update(self, coin, oid, status, cloid=None):
        """orderUpdates event. status in {'open','filled','canceled','rejected'}."""
        coin = coin.upper()
        with self._lock:
            row = self._rows.get(coin)
            if row is None: return
            row['updated_ts'] = time.time()
            if status == 'rejected':
                # If our entry was rejected, transition to FAILED_ENTRY
                if oid == row.get('entry_oid') or cloid == row.get('cloid_entry'):
                    if row['state'] == PENDING_ENTRY:
                        row['state'] = FAILED_ENTRY
                        self.stats['failed_entries'] += 1
            elif status == 'canceled':
                # If a tracked SL/TP got canceled externally, clear the oid
                if oid == row.get('sl_oid'): row['sl_oid'] = None
                if oid == row.get('tp_oid'): row['tp_oid'] = None

    def remove(self, coin):
        """Hard-remove a row (e.g. after FAILED_ENTRY cleanup)."""
        coin = coin.upper()
        with self._lock:
            self._rows.pop(coin, None)

    def expire_pending(self, max_age_sec=30):
        """Sweep PENDING_ENTRY rows older than max_age. Mark as FAILED_ENTRY.
        Caller is responsible for cleanup of any resting SL/TP oids."""
        now = time.time()
        expired = []
        with self._lock:
            for coin, row in list(self._rows.items()):
                if row['state'] != PENDING_ENTRY: continue
                if now - row['created_ts'] > max_age_sec:
                    row['state'] = FAILED_ENTRY
                    self.stats['failed_entries'] += 1
                    expired.append(coin)
        return expired

    # ─── Read accessors (hot path) ────────────────────────────────────
    def get(self, coin):
        """Return a copy of the row for coin, or None."""
        coin = coin.upper()
        with self._lock:
            row = self._rows.get(coin)
            return dict(row) if row else None

    def get_size(self, coin):
        """Drop-in replacement for _ep_fetch_size().
        Returns abs(size) if LIVE, None otherwise."""
        coin = coin.upper()
        with self._lock:
            row = self._rows.get(coin)
            if not row: return None
            if row['state'] != LIVE: return None
            sz = row.get('size', 0)
            return abs(sz) if abs(sz) > 1e-12 else None

    def get_protection(self, coin):
        """Returns dict {sl_oid, tp_oid, sl_px, tp_px} or None."""
        coin = coin.upper()
        with self._lock:
            row = self._rows.get(coin)
            if not row: return None
            return {
                'sl_oid': row.get('sl_oid'),
                'tp_oid': row.get('tp_oid'),
                'sl_px':  row.get('sl_px'),
                'tp_px':  row.get('tp_px'),
            }

    def is_live(self, coin):
        return self.get_size(coin) is not None

    def all_rows(self):
        with self._lock:
            return {c: dict(r) for c, r in self._rows.items()}

    # ─── WS health ────────────────────────────────────────────────────
    def mark_ws_connected(self, ok=True):
        with self._lock:
            self._ws_connected = bool(ok)
            if not ok:
                self.stats['ws_reconnects'] += 1

    def ws_is_fresh(self, max_age_sec=30):
        with self._lock:
            return (self._ws_connected and
                    (time.time() - self._last_ws_msg_ts) < max_age_sec)

    # ─── /health ──────────────────────────────────────────────────────
    def status(self):
        with self._lock:
            states = defaultdict(int)
            for r in self._rows.values():
                states[r['state']] += 1
            return {
                'ws_connected':     self._ws_connected,
                'ws_last_msg_age':  round(time.time() - self._last_ws_msg_ts, 1)
                                    if self._last_ws_msg_ts else None,
                'rows_total':       len(self._rows),
                'rows_by_state':    dict(states),
                **self.stats,
            }


# ─── Singleton ────────────────────────────────────────────────────────
_LEDGER = PositionLedger()

# Public API
begin_pending      = _LEDGER.begin_pending
on_fill            = _LEDGER.on_fill
on_webdata2        = _LEDGER.on_webdata2
on_order_update    = _LEDGER.on_order_update
remove             = _LEDGER.remove
expire_pending     = _LEDGER.expire_pending
get                = _LEDGER.get
get_size           = _LEDGER.get_size
get_protection     = _LEDGER.get_protection
is_live            = _LEDGER.is_live
all_rows           = _LEDGER.all_rows
mark_ws_connected  = _LEDGER.mark_ws_connected
ws_is_fresh        = _LEDGER.ws_is_fresh
status             = _LEDGER.status
set_protection_state = _LEDGER.set_protection_state
get_protection_state = _LEDGER.get_protection_state
