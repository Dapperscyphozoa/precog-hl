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
    # Profitability instrumentation (added apr-2026, optional — empty for legacy rows)
    'expected_edge_at_entry',  # net TP edge after friction; computed by gates.compute_expected_edge
    'funding_paid_pct',        # signed funding cost on notional; >0 = paid, <0 = received
    # Diagnostic instrumentation v2 (added 2026-04-26)
    'regime',                  # regime_detector output at entry time (chop|bull-calm|bear-calm|...)
    'realized_slippage_pct',   # signed (actual_fill_px - signal_px) / signal_px; on ENTRY rows only
    'mfe_pct',                 # max favourable excursion as fraction; on CLOSE rows only
    'mae_pct',                 # max adverse excursion as fraction; on CLOSE rows only
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
    """Ensure CSV has the full schema header. Caller must hold _LOCK.

    Also performs a one-time schema-extension upgrade: if the existing header
    is a subset of SCHEMA (i.e. SCHEMA has new fields appended), rewrites the
    file under the new SCHEMA, padding old rows with empty strings.
    """
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
        return
    existing_cols = [c.strip() for c in first.split(',')]
    missing_in_header = [c for c in SCHEMA if c not in existing_cols]
    if missing_in_header:
        # One-time upgrade: rewrite under full SCHEMA so DictReader can resolve
        # the new columns going forward.
        with open(_PATH, 'r', newline='') as f:
            reader = csv.DictReader(f)
            old_rows = list(reader)
        tmp = _PATH + '.upgrading'
        with open(tmp, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=SCHEMA, extrasaction='ignore')
            w.writeheader()
            for r in old_rows:
                w.writerow({k: r.get(k, '') for k in SCHEMA})
        os.replace(tmp, _PATH)


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
        elif r.get('event_type') == 'ENTRY_UPDATE':
            # Don't overwrite the ENTRY record — merge fields into it.
            # This row carries post-fill protection params + fill-corrected
            # entry_price + realized slippage that should appear on the
            # canonical ENTRY record after restart.
            entry_row = None
            for prior in rows:
                if prior.get('trade_id') == tid and prior.get('event_type') == 'ENTRY':
                    entry_row = prior
                    break
            if entry_row is not None:
                for k in ('sl_pct', 'tp_pct', 'expected_edge_at_entry',
                          'realized_slippage_pct', 'entry_price', 'entry'):
                    v = r.get(k)
                    if v not in (None, ''):
                        entry_row[k] = v
                # Restore by_trade_id pointer to the merged ENTRY row
                _INDEX['by_trade_id'][tid] = entry_row
        elif r.get('event_type') == 'CLOSE':
            # Carry through entry-time fields (sl_pct, tp_pct, edge, regime,
            # slippage) from any prior ENTRY/ENTRY_UPDATE so post-close lookups
            # see the full picture, not just the close row.
            for k in ('sl_pct', 'tp_pct', 'expected_edge_at_entry', 'engine',
                      'side', 'coin', 'cloid', 'entry_price',
                      'regime', 'realized_slippage_pct'):
                if r.get(k) in (None, ''):
                    for prior in rows:
                        if (prior.get('trade_id') == tid
                                and prior.get('event_type') in ('ENTRY', 'ENTRY_UPDATE')
                                and prior.get(k) not in (None, '')):
                            r[k] = prior.get(k)
                            break
            _INDEX['by_trade_id'][tid] = r
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
                 sl_pct=None, tp_pct=None, cloid=None, trade_id=None,
                 expected_edge_at_entry=None,
                 regime=None, realized_slippage_pct=None):
    """Append ENTRY event. Returns trade_id.

    If trade_id is None, generates a new one.
    `expected_edge_at_entry` is optional; pass gates.compute_expected_edge(tp_pct, sl_pct)
    when tp/sl are known at entry time so the analyzer can correlate edge with outcome.
    `regime` is the regime_detector classification at entry (chop/bull/bear/etc).
    `realized_slippage_pct` is signed (actual_fill_px - signal_entry_px)/signal_entry_px
    so the analyzer can break out true net PnL after fill cost.
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
            'expected_edge_at_entry': (expected_edge_at_entry
                                       if expected_edge_at_entry is not None else ''),
            'regime': regime or '',
            'realized_slippage_pct': (realized_slippage_pct
                                      if realized_slippage_pct is not None else ''),
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


def update_entry_fields(trade_id, sl_pct=None, tp_pct=None,
                        expected_edge_at_entry=None,
                        realized_slippage_pct=None,
                        entry_price=None):
    """Record post-entry-fill protection params and/or fill-realized fields
    for an existing trade.

    Appends an ENTRY_UPDATE event row and merges the fields into the
    in-memory ENTRY record so subsequent get_by_trade_id calls see them.
    Use AFTER enforce_protection completes (sl_pct/tp_pct/edge) and/or
    AFTER fill returns (realized_slippage_pct, corrected entry_price).

    No-op (returns False) if trade_id is unknown.
    """
    if not trade_id:
        return False
    with _LOCK:
        existing = _INDEX['by_trade_id'].get(trade_id)
        if not existing:
            return False

        _write_header_if_missing()

        # Inherit immutable fields from the ENTRY record
        coin = existing.get('coin', '')
        side = existing.get('side', '')
        engine = existing.get('engine', '')

        row = {k: '' for k in SCHEMA}
        row.update({
            'event_seq': _next_seq(),
            'event_type': 'ENTRY_UPDATE',
            'trade_id': trade_id,
            'timestamp': _now_iso(),
            'coin': coin,
            'side': side,
            'engine': engine,
            'sl_pct': sl_pct if sl_pct is not None else '',
            'tp_pct': tp_pct if tp_pct is not None else '',
            'expected_edge_at_entry': (expected_edge_at_entry
                                       if expected_edge_at_entry is not None else ''),
            'realized_slippage_pct': (realized_slippage_pct
                                      if realized_slippage_pct is not None else ''),
            'entry_price': entry_price if entry_price is not None else '',
            'source': 'protection_placed',
            'direction': side,  # legacy mirror
        })

        with open(_PATH, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=SCHEMA)
            w.writerow(row)

        # Merge fields into the canonical ENTRY record so live readers see them.
        if sl_pct is not None:
            existing['sl_pct'] = sl_pct
        if tp_pct is not None:
            existing['tp_pct'] = tp_pct
        if expected_edge_at_entry is not None:
            existing['expected_edge_at_entry'] = expected_edge_at_entry
        if realized_slippage_pct is not None:
            existing['realized_slippage_pct'] = realized_slippage_pct
        if entry_price is not None:
            existing['entry_price'] = entry_price
            existing['entry'] = entry_price  # legacy mirror

        return True


def append_close(trade_id, exit_price, pnl, close_reason,
                 exchange_fill_id=None, source='reconcile',
                 funding_paid_pct=None,
                 mfe_pct=None, mae_pct=None):
    """Append CLOSE event. Idempotent — returns False if trade already closed.

    `funding_paid_pct` is optional; pass funding_accrual.compute_funding_paid_pct(...)
    output so realized PnL can be reconciled with funding cost in analyze_trades.
    Sign: positive = position paid (cost), negative = position received (credit).
    `mfe_pct` / `mae_pct` are max favourable / adverse excursion as signed
    fractions of entry price (e.g. mfe_pct=0.012 = position went +1.2% in our
    favour during the hold; mae_pct=-0.008 = went -0.8% against us).

    Returns True if close was recorded, False if already closed (duplicate).

    2026-04-26: PnL sanity guard. If |pnl| > $50 OR |mfe_pct/mae_pct| > 0.5
    (50%), the trade has a unit-conversion bug (most likely a k-prefix coin
    where entry vs fill prices got reported in mismatched scales). Set the
    bogus values to None rather than poison the ledger; log the trade_id
    for investigation.
    """
    # ─── PnL sanity guard ────────────────────────────────────
    try:
        if pnl is not None and abs(float(pnl)) > 50.0:
            print(f"[ledger] WARN bogus pnl on close trade_id={trade_id} "
                  f"pnl={pnl} reason={close_reason} — DISCARDING (probable unit bug)",
                  flush=True)
            pnl = None
    except (TypeError, ValueError):
        pass
    try:
        if mfe_pct is not None and abs(float(mfe_pct)) > 0.5:
            print(f"[ledger] WARN bogus mfe_pct={mfe_pct} on trade_id={trade_id} — clearing", flush=True)
            mfe_pct = None
    except (TypeError, ValueError):
        pass
    try:
        if mae_pct is not None and abs(float(mae_pct)) > 0.5:
            print(f"[ledger] WARN bogus mae_pct={mae_pct} on trade_id={trade_id} — clearing", flush=True)
            mae_pct = None
    except (TypeError, ValueError):
        pass

    with _LOCK:
        existing = _INDEX['by_trade_id'].get(trade_id)
        # Defaults for orphan close (no prior ENTRY)
        coin = ''; side = ''; engine = ''
        _carry_regime = ''; _carry_slip = ''; _carry_edge = ''
        _carry_sl = ''; _carry_tp = ''; _carry_entry_px = ''; _carry_cloid = ''
        if not existing:
            source = 'orphan_close'
        else:
            coin = existing.get('coin', '')
            side = existing.get('side', '')
            engine = existing.get('engine', '')   # inherit engine tag from entry
            # Carry through entry-time diagnostic fields so the CLOSE row
            # also surfaces them in /trades/recent and analyze_trades.
            _carry_regime = existing.get('regime', '')
            _carry_slip = existing.get('realized_slippage_pct', '')
            _carry_edge = existing.get('expected_edge_at_entry', '')
            _carry_sl = existing.get('sl_pct', '')
            _carry_tp = existing.get('tp_pct', '')
            _carry_entry_px = existing.get('entry_price', '')
            _carry_cloid = existing.get('cloid', '')

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
            'engine': engine,                          # carry-over so by_engine attribution works
            'exit_price': exit_price if exit_price is not None else '',
            'pnl': pnl if pnl is not None else '',
            'close_reason': close_reason,
            'exchange_fill_id': exchange_fill_id or '',
            'source': source,
            'direction': 'CLOSE',  # legacy mirror
            'entry': exit_price if exit_price is not None else '',  # legacy mirror (old schema put exit here)
            'funding_paid_pct': (funding_paid_pct
                                 if funding_paid_pct is not None else ''),
            'mfe_pct': mfe_pct if mfe_pct is not None else '',
            'mae_pct': mae_pct if mae_pct is not None else '',
            # Carry through entry-time diagnostics so /trades/recent CLOSE
            # rows show regime / slippage / edge alongside realized PnL.
            'regime': _carry_regime,
            'realized_slippage_pct': _carry_slip,
            'expected_edge_at_entry': _carry_edge,
            'sl_pct': _carry_sl,
            'tp_pct': _carry_tp,
            'entry_price': _carry_entry_px,
            'cloid': _carry_cloid,
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


def recent_close_ts(coin, max_age_sec=45):
    """Return the most recent CLOSE event timestamp (unix seconds) for `coin`
    within `max_age_sec`, or None if no qualifying close.

    Used by lifecycle_reconciler to suppress orphan-adopt for coins that
    were just closed via any path (confluence_close, precog close_trade,
    webhook). Without this, the post-close exchange-snapshot lag (~5-15s)
    causes the reconciler to adopt the still-flattening position as a
    fresh RECONCILED trade — the bug pattern we saw on XRP/WLFI/UNI/etc
    after the WR-fix deploy.

    Implementation: scan in-memory CSV rows, take max CLOSE timestamp.
    O(n) but n is bounded — only ledger rows in the active process.
    """
    if not coin:
        return None
    coin_u = coin.upper()
    cutoff = time.time() - max_age_sec
    latest = None
    with _LOCK:
        for tid, row in _INDEX['by_trade_id'].items():
            if (row.get('coin') or '').upper() != coin_u:
                continue
            if row.get('event_type') != 'CLOSE':
                continue
            ts_iso = row.get('timestamp', '')
            if not ts_iso:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_iso).replace('Z', '+00:00')).timestamp()
            except Exception:
                continue
            if ts < cutoff:
                continue
            if latest is None or ts > latest:
                latest = ts
    return latest


def recent_consecutive_losses(coin, hours=4.0):
    """Count consecutive losing closes for `coin` going backwards from now,
    within `hours`. Stops counting at the first non-loss (win or breakeven).

    Returns (consecutive_loss_count, last_close_ts) where last_close_ts is
    unix seconds of the most recent close (None if none).

    Used by entry dispatcher to circuit-break coins on a recent losing
    streak — 2026-04-27: bleeders (W, UMA, STX) ate winners' gains.
    """
    if not coin:
        return 0, None
    coin_u = coin.upper()
    cutoff = time.time() - hours * 3600.0
    closes = []
    with _LOCK:
        for tid, row in _INDEX['by_trade_id'].items():
            if (row.get('coin') or '').upper() != coin_u:
                continue
            if row.get('event_type') != 'CLOSE':
                continue
            ts_iso = row.get('timestamp', '')
            if not ts_iso:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_iso).replace('Z', '+00:00')).timestamp()
            except Exception:
                continue
            if ts < cutoff:
                continue
            try:
                pnl = float(row.get('pnl', '') or 0)
            except (TypeError, ValueError):
                pnl = 0.0
            closes.append((ts, pnl))
    if not closes:
        return 0, None
    closes.sort(key=lambda x: -x[0])  # newest first
    consec = 0
    for ts, pnl in closes:
        if pnl < 0:
            consec += 1
        else:
            break
    return consec, closes[0][0]


def engine_rolling_wr(engine, n_window=5, hours=24.0):
    """Compute rolling WR for a given engine over the last N closed trades
    within `hours`. Returns (wr_pct, n_decided, last_close_ts) where:
      wr_pct: percentage 0-100, or None if n_decided < 2
      n_decided: count of W+L trades (excludes b/breakeven and unrecorded pnl)
      last_close_ts: unix seconds of most recent close (None if no trades)

    Used by auto engine-pause: if WR drops below threshold over last N
    trades, engine is disabled for cooldown period.
    """
    if not engine:
        return None, 0, None
    cutoff = time.time() - hours * 3600.0
    closes = []
    with _LOCK:
        for tid, row in _INDEX['by_trade_id'].items():
            if (row.get('engine') or '') != engine:
                continue
            if row.get('event_type') != 'CLOSE':
                continue
            ts_iso = row.get('timestamp', '')
            if not ts_iso:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_iso).replace('Z', '+00:00')).timestamp()
            except Exception:
                continue
            if ts < cutoff:
                continue
            try:
                pnl_raw = row.get('pnl', '')
                if pnl_raw == '' or pnl_raw is None:
                    continue
                pnl = float(pnl_raw)
            except (TypeError, ValueError):
                continue
            closes.append((ts, pnl))
    if not closes:
        return None, 0, None
    closes.sort(key=lambda x: -x[0])
    recent = closes[:n_window]
    wins = sum(1 for _, p in recent if p > 0)
    losses = sum(1 for _, p in recent if p < 0)
    decided = wins + losses
    if decided < 2:
        return None, decided, recent[0][0]
    return (wins / decided * 100.0), decided, recent[0][0]


def system_aggregate(system='a', hours=12.0):
    """Aggregate trade stats for System A (precog engines) or System B
    (confluence engines = engine name starts with 'CONFLUENCE_').

    Returns dict shape mirroring /confluence response so System A and
    System B can be displayed uniformly:
      {
        'system': 'a' | 'b',
        'window_hours': float,
        'closed_count': int,
        'wins': int, 'losses': int, 'breakevens': int,
        'wr_pct': float | None,
        'total_pnl_usd': float, 'total_pnl_pct': float,
        'avg_win_usd': float, 'avg_loss_usd': float,
        'by_engine': { engine: {n, w, l, wr_pct, pnl_usd} },
        'by_coin': { coin: {n, w, l, wr_pct, pnl_usd} },
      }
    """
    cutoff = time.time() - hours * 3600.0
    closes = []
    with _LOCK:
        for tid, row in _INDEX['by_trade_id'].items():
            if row.get('event_type') != 'CLOSE':
                continue
            ts_iso = row.get('timestamp', '')
            if not ts_iso:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_iso).replace('Z', '+00:00')).timestamp()
            except Exception:
                continue
            if ts < cutoff:
                continue
            engine = (row.get('engine') or '').strip()
            is_b = engine.startswith('CONFLUENCE_')
            if (system == 'b' and not is_b) or (system == 'a' and (is_b or not engine)):
                continue
            try:
                pnl_raw = row.get('pnl', '')
                pnl = float(pnl_raw) if pnl_raw not in (None, '') else None
            except (TypeError, ValueError):
                pnl = None
            closes.append({
                'ts': ts,
                'engine': engine,
                'coin': (row.get('coin') or '').upper(),
                'pnl': pnl,
            })

    n_closed = len(closes)
    decided = [c for c in closes if c['pnl'] is not None]
    wins = [c for c in decided if c['pnl'] > 0]
    losses = [c for c in decided if c['pnl'] < 0]
    breakevens = [c for c in decided if c['pnl'] == 0]
    n_dec = len(wins) + len(losses)

    total_usd = sum(c['pnl'] for c in decided)
    avg_win = (sum(c['pnl'] for c in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(c['pnl'] for c in losses) / len(losses)) if losses else 0.0
    wr_pct = (len(wins) / n_dec * 100) if n_dec else None

    by_engine = {}
    for c in closes:
        e = c['engine'] or '_untagged'
        by_engine.setdefault(e, {'n': 0, 'w': 0, 'l': 0, 'b': 0, 'pnl_usd': 0.0})
        by_engine[e]['n'] += 1
        if c['pnl'] is not None:
            by_engine[e]['pnl_usd'] += c['pnl']
            if c['pnl'] > 0: by_engine[e]['w'] += 1
            elif c['pnl'] < 0: by_engine[e]['l'] += 1
            else: by_engine[e]['b'] += 1
    for e, v in by_engine.items():
        dec = v['w'] + v['l']
        v['wr_pct'] = round(v['w'] / dec * 100, 1) if dec else None
        v['pnl_usd'] = round(v['pnl_usd'], 4)

    by_coin = {}
    for c in closes:
        ck = c['coin'] or '_unknown'
        by_coin.setdefault(ck, {'n': 0, 'w': 0, 'l': 0, 'b': 0, 'pnl_usd': 0.0})
        by_coin[ck]['n'] += 1
        if c['pnl'] is not None:
            by_coin[ck]['pnl_usd'] += c['pnl']
            if c['pnl'] > 0: by_coin[ck]['w'] += 1
            elif c['pnl'] < 0: by_coin[ck]['l'] += 1
            else: by_coin[ck]['b'] += 1
    for ck, v in by_coin.items():
        dec = v['w'] + v['l']
        v['wr_pct'] = round(v['w'] / dec * 100, 1) if dec else None
        v['pnl_usd'] = round(v['pnl_usd'], 4)

    return {
        'system': system,
        'window_hours': hours,
        'closed_count': n_closed,
        'wins': len(wins),
        'losses': len(losses),
        'breakevens': len(breakevens),
        'wr_pct': round(wr_pct, 1) if wr_pct is not None else None,
        'total_pnl_usd': round(total_usd, 4),
        'avg_win_usd': round(avg_win, 4),
        'avg_loss_usd': round(avg_loss, 4),
        'by_engine': by_engine,
        'by_coin': by_coin,
    }


def coin_engine_rolling_wr(coin, engine, n_window=5, hours=24.0):
    """Rolling WR for a (coin, engine) pair. Returns (wr_pct, n_decided,
    last_close_ts). Used by per-coin x per-engine gate to block fires on
    pairs that have a proven negative edge.

    Conservative: returns None for n<3 so new pairs aren't blocked from
    establishing a sample. Caller treats None as "allow".
    """
    if not coin or not engine:
        return None, 0, None
    coin_u = coin.upper()
    cutoff = time.time() - hours * 3600.0
    closes = []
    with _LOCK:
        for tid, row in _INDEX['by_trade_id'].items():
            if (row.get('coin') or '').upper() != coin_u:
                continue
            if (row.get('engine') or '') != engine:
                continue
            if row.get('event_type') != 'CLOSE':
                continue
            ts_iso = row.get('timestamp', '')
            if not ts_iso:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_iso).replace('Z', '+00:00')).timestamp()
            except Exception:
                continue
            if ts < cutoff:
                continue
            try:
                pnl_raw = row.get('pnl', '')
                if pnl_raw == '' or pnl_raw is None:
                    continue
                pnl = float(pnl_raw)
            except (TypeError, ValueError):
                continue
            closes.append((ts, pnl))
    if not closes:
        return None, 0, None
    closes.sort(key=lambda x: -x[0])
    recent = closes[:n_window]
    wins = sum(1 for _, p in recent if p > 0)
    losses = sum(1 for _, p in recent if p < 0)
    decided = wins + losses
    if decided < 3:
        return None, decided, recent[0][0]
    return (wins / decided * 100.0), decided, recent[0][0]


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
