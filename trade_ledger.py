"""Trade Ledger — single source of truth for trade lifecycle events.

Append-only CSV at /var/data/trades.csv. Single writer guaranteed by module-level lock.
Schema extends legacy format backward-compatibly; migration run on module import.

PUBLIC API:
    new_trade_id()             -> str
    append_entry(...)          -> trade_id
    append_close(trade_id, ..)
    open_trades()              -> list[dict]
    get_by_trade_id(tid)       -> dict | None
    get_by_coin(coin)           -> list[dict]
    is_closed(trade_id)        -> bool
    stats()                    -> dict

PRIVATE — no module outside reconciler should write via anything but this module.
"""
import csv
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

_LOCK = threading.Lock()
_PATH = os.environ.get('TRADE_LEDGER_PATH', '/var/data/trades.csv')
_LEGACY_PATH = _PATH  # same file — we upgrade schema in place
_MIGRATION_MARKER = os.environ.get('LEDGER_MIGRATION_MARKER', '/var/data/trades.csv.migrated')

# Full schema. Legacy rows without new fields treated as nullable.
SCHEMA = [
    'event_seq',           # monotonic int per process start; ordering truth
    'event_type',          # ENTRY | CLOSE
    'trade_id',            # 12-char hex
    'timestamp',           # ISO8601 UTC
    'engine',              # PIVOT | BB_REJ | INSIDE_BAR | PULLBACK | WALL_BNC | ...
    'coin',
    'side',                # BUY/SELL for ENTRY, L/S for legacy CLOSE rows
    'entry_price',         # present on ENTRY
    'exit_price',          # present on CLOSE
    'pnl',                 # USD, present on CLOSE
    'close_reason',        # tp | sl | timeout | exchange_fill | manual | protection | signal_reversal | contract_close | reconcile_missing | legacy_unknown
    'exchange_fill_id',    # HL oid, nullable
    'cloid',               # client order id, nullable (future entries)
    'source',              # precog_signal | webhook | reconcile | admin
    'sl_pct',
    'tp_pct',
    # legacy compatibility columns (written as aliases, not read authoritatively)
    'direction',           # mirrors side for legacy parsers
    'entry',               # mirrors entry_price for legacy parsers
]

# In-memory index for fast lookups. Rebuilt on boot from CSV.
_INDEX = {
    'by_trade_id': {},     # trade_id -> latest event dict
    'open_trades': set(),  # trade_ids with ENTRY but no CLOSE
    'by_coin_open': {},    # coin -> set of open trade_ids
    'coin_to_latest_open': {},  # coin -> trade_id (most recent open for that coin)
}

_EVENT_SEQ = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def new_trade_id() -> str:
    """12-char hex uuid4 prefix."""
    return uuid.uuid4().hex[:12]


def _next_seq() -> int:
    """Monotonic sequence. Caller must hold _LOCK."""
    global _EVENT_SEQ
    _EVENT_SEQ += 1
    return _EVENT_SEQ


def _read_all() -> list:
    """Read every row from CSV. Returns list of dicts (with all schema fields, nulls for missing)."""
    if not os.path.exists(_PATH):
        return []
    rows = []
    with open(_PATH, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for r in reader:
            # Normalize — fill missing schema fields with ''
            for k in SCHEMA:
                if k not in r:
                    r[k] = ''
            rows.append(r)
    return rows


def _write_header_if_missing():
    """Ensure CSV has the full schema header. Caller must hold _LOCK."""
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    if not os.path.exists(_PATH):
        with open(_PATH, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(SCHEMA)
        return
    # File exists — check header
    with open(_PATH, 'r', newline='') as f:
        first = f.readline().strip()
    if not first:
        with open(_PATH, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(SCHEMA)


def _migrate_legacy_rows():
    """Upgrade existing trades.csv to include trade_id, event_seq, event_type.

    Runs once. Idempotent — marker file prevents re-run.
    Legacy rows are rewritten with:
      - event_type inferred from direction (CLOSE if 'CLOSE', else ENTRY)
      - trade_id generated per ENTRY row; CLOSE rows matched by coin to most recent
        open ENTRY (best-effort; historical data has no true ID)
      - close_reason = 'legacy_unknown' unless inferable from source
      - event_seq assigned sequentially in timestamp order
    """
    global _EVENT_SEQ

    if os.path.exists(_MIGRATION_MARKER):
        return 'already_migrated'

    if not os.path.exists(_PATH):
        # Fresh file — no migration needed, just mark done
        _write_header_if_missing()
        with open(_MIGRATION_MARKER, 'w') as f:
            f.write(_now_iso())
        return 'no_prior_data'

    # Read existing rows (may be old 8-field schema or partial new schema)
    legacy_rows = []
    with open(_PATH, 'r', newline='') as f:
        reader = csv.DictReader(f)
        legacy_rows = list(reader)

    if not legacy_rows:
        _write_header_if_missing()
        with open(_MIGRATION_MARKER, 'w') as f:
            f.write(_now_iso())
        return 'empty'

    # Sort by timestamp (legacy rows have ISO timestamps)
    def _parse_ts(r):
        try:
            return datetime.fromisoformat(r.get('timestamp', '').replace('Z', '+00:00'))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    legacy_rows.sort(key=_parse_ts)

    # Backfill: walk chronologically, assign trade_ids
    open_by_coin = {}  # coin -> trade_id
    upgraded = []
    seq = 0
    for r in legacy_rows:
        seq += 1
        coin = (r.get('coin') or '').strip()
        direction = (r.get('direction') or '').strip().upper()
        timestamp = r.get('timestamp') or _now_iso()

        new_row = {k: '' for k in SCHEMA}
        new_row['event_seq'] = str(seq)
        new_row['timestamp'] = timestamp
        new_row['coin'] = coin
        new_row['engine'] = r.get('engine') or ''
        new_row['source'] = r.get('source') or 'legacy'
        new_row['sl_pct'] = r.get('sl_pct') or ''
        new_row['direction'] = direction  # legacy mirror

        if direction == 'CLOSE':
            new_row['event_type'] = 'CLOSE'
            new_row['exit_price'] = r.get('entry') or ''  # legacy stored exit in 'entry' col
            new_row['pnl'] = r.get('pnl') or ''
            new_row['close_reason'] = _infer_close_reason(r.get('source') or '')
            # Match to most recent open trade for this coin
            tid = open_by_coin.pop(coin, None)
            if tid is None:
                # Orphan close (close without preceding entry in our history)
                tid = new_trade_id()
                new_row['trade_id'] = tid
                new_row['close_reason'] = 'legacy_orphan_close'
            else:
                new_row['trade_id'] = tid
        else:
            new_row['event_type'] = 'ENTRY'
            new_row['side'] = direction  # BUY/SELL
            new_row['entry_price'] = r.get('entry') or ''
            new_row['entry'] = r.get('entry') or ''
            new_row['pnl'] = ''
            tid = new_trade_id()
            new_row['trade_id'] = tid
            # Track as open — may be closed by later CLOSE row
            if coin in open_by_coin:
                # Stacked entry without intervening close — mark legacy duplicate
                # Keep latest; the earlier open becomes a "never-closed" ledger trade
                pass
            open_by_coin[coin] = tid

        upgraded.append(new_row)

    # Rewrite file atomically
    tmp_path = _PATH + '.migrating'
    with open(tmp_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA)
        w.writeheader()
        for row in upgraded:
            w.writerow(row)
    os.replace(tmp_path, _PATH)

    # Update sequence counter and marker
    _EVENT_SEQ = seq
    with open(_MIGRATION_MARKER, 'w') as f:
        f.write(json.dumps({
            'migrated_at': _now_iso(),
            'rows_migrated': len(upgraded),
            'open_after_migration': len(open_by_coin),
            'final_event_seq': seq,
        }))

    return f'migrated {len(upgraded)} rows, {len(open_by_coin)} left open'


def _infer_close_reason(source: str) -> str:
    """Best-effort mapping from legacy 'source' column to close_reason."""
    s = (source or '').lower()
    if 'tp' in s: return 'tp'
    if 'sl' in s: return 'sl'
    if 'trail' in s: return 'trail_exit'
    if 'max_hold' in s or 'timeout' in s: return 'timeout'
    if 'funding' in s: return 'funding_cut'
    if 'dust' in s: return 'dust_sweep'
    if 'reversal' in s: return 'signal_reversal'
    if 'manual' in s or 'admin' in s: return 'manual'
    if 'close' == s: return 'legacy_unknown'
    return 'legacy_unknown'


def _rebuild_index():
    """Rebuild in-memory index from CSV. Caller must hold _LOCK."""
    global _EVENT_SEQ
    _INDEX['by_trade_id'].clear()
    _INDEX['open_trades'].clear()
    _INDEX['by_coin_open'].clear()
    _INDEX['coin_to_latest_open'].clear()

    max_seq = 0
    rows = _read_all()
    for r in rows:
        tid = r.get('trade_id') or ''
        if not tid:
            continue
        try:
            seq = int(r.get('event_seq') or 0)
            if seq > max_seq:
                max_seq = seq
        except ValueError:
            pass

        _INDEX['by_trade_id'][tid] = r

        if r.get('event_type') == 'ENTRY':
            _INDEX['open_trades'].add(tid)
            coin = r.get('coin') or ''
            if coin:
                _INDEX['by_coin_open'].setdefault(coin, set()).add(tid)
                _INDEX['coin_to_latest_open'][coin] = tid
        elif r.get('event_type') == 'CLOSE':
            _INDEX['open_trades'].discard(tid)
            coin = r.get('coin') or ''
            if coin:
                _INDEX['by_coin_open'].get(coin, set()).discard(tid)
                if _INDEX['coin_to_latest_open'].get(coin) == tid:
                    # find another open trade for same coin, if any
                    remaining = _INDEX['by_coin_open'].get(coin, set())
                    if remaining:
                        _INDEX['coin_to_latest_open'][coin] = next(iter(remaining))
                    else:
                        _INDEX['coin_to_latest_open'].pop(coin, None)

    _EVENT_SEQ = max_seq


# ─────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────

def append_entry(coin, side, entry_price, engine=None, source='precog_signal',
                 sl_pct=None, tp_pct=None, cloid=None, trade_id=None):
    """Append ENTRY event. Returns trade_id.

    If trade_id is None, generates a new one.
    """
    with _LOCK:
        _write_header_if_missing()
        if trade_id is None:
            trade_id = new_trade_id()

        row = {k: '' for k in SCHEMA}
        row.update({
            'event_seq': _next_seq(),
            'event_type': 'ENTRY',
            'trade_id': trade_id,
            'timestamp': _now_iso(),
            'engine': engine or '',
            'coin': coin,
            'side': side,
            'entry_price': entry_price,
            'entry': entry_price,  # legacy mirror
            'direction': side,      # legacy mirror
            'source': source,
            'sl_pct': sl_pct if sl_pct is not None else '',
            'tp_pct': tp_pct if tp_pct is not None else '',
            'cloid': cloid or '',
        })

        with open(_PATH, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=SCHEMA)
            w.writerow(row)

        # Update index
        _INDEX['by_trade_id'][trade_id] = row
        _INDEX['open_trades'].add(trade_id)
        _INDEX['by_coin_open'].setdefault(coin, set()).add(trade_id)
        _INDEX['coin_to_latest_open'][coin] = trade_id

        return trade_id


def append_close(trade_id, exit_price, pnl, close_reason,
                 exchange_fill_id=None, source='reconcile'):
    """Append CLOSE event. Idempotent — returns False if trade already closed.

    Returns True if close was recorded, False if already closed (duplicate).
    """
    with _LOCK:
        existing = _INDEX['by_trade_id'].get(trade_id)
        if not existing:
            # Unknown trade — still record as orphan close
            coin = ''
            side = ''
            source = 'orphan_close'
        else:
            coin = existing.get('coin', '')
            side = existing.get('side', '')

        # Idempotency check — is this trade already closed?
        if trade_id not in _INDEX['open_trades'] and existing and existing.get('event_type') == 'CLOSE':
            return False

        _write_header_if_missing()

        row = {k: '' for k in SCHEMA}
        row.update({
            'event_seq': _next_seq(),
            'event_type': 'CLOSE',
            'trade_id': trade_id,
            'timestamp': _now_iso(),
            'coin': coin,
            'side': side,
            'exit_price': exit_price if exit_price is not None else '',
            'pnl': pnl if pnl is not None else '',
            'close_reason': close_reason,
            'exchange_fill_id': exchange_fill_id or '',
            'source': source,
            'direction': 'CLOSE',  # legacy mirror
            'entry': exit_price if exit_price is not None else '',  # legacy mirror (old schema put exit here)
        })

        with open(_PATH, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=SCHEMA)
            w.writerow(row)

        # Update index
        _INDEX['by_trade_id'][trade_id] = row
        _INDEX['open_trades'].discard(trade_id)
        if coin:
            _INDEX['by_coin_open'].get(coin, set()).discard(trade_id)
            if _INDEX['coin_to_latest_open'].get(coin) == trade_id:
                remaining = _INDEX['by_coin_open'].get(coin, set())
                if remaining:
                    _INDEX['coin_to_latest_open'][coin] = next(iter(remaining))
                else:
                    _INDEX['coin_to_latest_open'].pop(coin, None)

        return True


def open_trades() -> list:
    """Return list of trade dicts with ENTRY but no CLOSE."""
    with _LOCK:
        return [dict(_INDEX['by_trade_id'][tid]) for tid in _INDEX['open_trades']
                if tid in _INDEX['by_trade_id']]


def get_by_trade_id(trade_id: str):
    """Return latest event for this trade_id, or None."""
    with _LOCK:
        r = _INDEX['by_trade_id'].get(trade_id)
        return dict(r) if r else None


def get_by_coin(coin: str) -> list:
    """Return all open trade dicts for a coin, most recent first."""
    with _LOCK:
        tids = list(_INDEX['by_coin_open'].get(coin, set()))
    return [get_by_trade_id(tid) for tid in tids if get_by_trade_id(tid)]


def latest_open_trade_id_for_coin(coin: str):
    """Return trade_id of most recent open trade for coin, or None."""
    with _LOCK:
        return _INDEX['coin_to_latest_open'].get(coin)


def is_closed(trade_id: str) -> bool:
    with _LOCK:
        return trade_id not in _INDEX['open_trades']


def stats() -> dict:
    with _LOCK:
        open_count = len(_INDEX['open_trades'])
        total_known = len(_INDEX['by_trade_id'])
        open_coins = list(_INDEX['by_coin_open'].keys())
        return {
            'ledger_path': _PATH,
            'open_trades_count': open_count,
            'total_trade_ids_known': total_known,
            'event_seq': _EVENT_SEQ,
            'open_coins': sorted([c for c in open_coins if _INDEX['by_coin_open'].get(c)]),
        }


def dedupe_open_trades():
    """One-time cleanup — for each coin with multiple open trades, keep the earliest
    (lowest event_seq) and close the rest with reason='reconcile_duplicate_entry'.

    Returns dict of dedup actions taken:
        {'coins_affected': int, 'dupes_closed': int, 'details': [...]}
    """
    actions = {'coins_affected': 0, 'dupes_closed': 0, 'details': []}
    with _LOCK:
        by_coin_open = dict(_INDEX['by_coin_open'])

    for coin, tid_set in by_coin_open.items():
        if len(tid_set) <= 1:
            continue
        # Sort by event_seq ascending — keep earliest
        tids_sorted = sorted(
            tid_set,
            key=lambda t: int(_INDEX['by_trade_id'].get(t, {}).get('event_seq') or 0)
        )
        keeper = tids_sorted[0]
        dupes = tids_sorted[1:]
        actions['coins_affected'] += 1
        for dup_tid in dupes:
            ok = append_close(
                trade_id=dup_tid,
                exit_price=None,
                pnl=None,
                close_reason='reconcile_duplicate_entry',
                source='ledger_dedupe',
            )
            if ok:
                actions['dupes_closed'] += 1
                actions['details'].append({
                    'coin': coin,
                    'kept': keeper[:8],
                    'closed': dup_tid[:8],
                })
    return actions


def close_missing_on_exchange(live_exchange_coins):
    """One-time cleanup — for each open ledger trade whose coin is NOT in
    live_exchange_coins, record a CLOSE with reason='reconcile_missing_on_cleanup'.

    Purpose: flush stale open trades left over from the Step 2 adoption-bug period
    where positions closed on exchange but ledger never recorded the close
    (observe mode couldn't execute closes).

    live_exchange_coins: iterable of coin strings currently open on exchange.
    """
    exch_set = set(live_exchange_coins or [])
    actions = {'closed_missing': 0, 'details': []}
    with _LOCK:
        # snapshot open coins
        open_trades = []
        for tid, rec in _INDEX['by_trade_id'].items():
            if tid in _INDEX['open_trades']:
                open_trades.append((tid, rec.get('coin', '')))

    for tid, coin in open_trades:
        if not coin or coin in exch_set:
            continue
        ok = append_close(
            trade_id=tid,
            exit_price=None,
            pnl=None,
            close_reason='reconcile_missing_on_cleanup',
            source='ledger_cleanup',
        )
        if ok:
            actions['closed_missing'] += 1
            actions['details'].append({'coin': coin, 'closed': tid[:8]})
    return actions


# ─────────────────────────────────────────────────────────
# Boot: migrate + index
# ─────────────────────────────────────────────────────────

def _boot():
    with _LOCK:
        result = _migrate_legacy_rows()
        _rebuild_index()
    print(f'[trade_ledger] boot: migration={result} event_seq={_EVENT_SEQ} '
          f'open_trades={len(_INDEX["open_trades"])}', flush=True)


# Run boot on import
try:
    _boot()
except Exception as _e:
    print(f'[trade_ledger] boot error (non-fatal): {_e}', flush=True)
