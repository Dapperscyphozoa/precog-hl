"""One-shot CSV cleanup for kFLOKI 1000x PnL corruption.

Background: a kFLOKI close at 2026-04-26T22:04 logged pnl=$999 due to a
unit-scale mismatch between entry vs fill price (HL reports k-coins in
two different scales). The trade_ledger sanity guard (|pnl|>$50 → null)
was added at 22:23 UTC, 19min after the bogus row.

This script:
  1. Backs up /var/data/trades.csv to trades.csv.bak.<timestamp>
  2. Scans for rows matching: (coin starts with 'k') AND (|pnl|>50 OR |mfe_pct|>0.5 OR |mae_pct|>0.5)
  3. Nulls pnl, mfe_pct, mae_pct on those rows
  4. Writes back atomic (write to .tmp, fsync, rename)

Run on the live host:  python cleanup_kfloki_pnl.py
Idempotent — safe to re-run.
"""
import csv
import os
import shutil
import sys
import time

PATH = os.environ.get('TRADE_LEDGER_PATH', '/var/data/trades.csv')
PNL_THRESHOLD = 50.0
PCT_THRESHOLD = 0.5  # 50%


def _parse_float(s):
    try:
        if s is None or s == '':
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def main():
    if not os.path.exists(PATH):
        print(f"[cleanup] {PATH} not found", file=sys.stderr)
        return 1

    backup = f"{PATH}.bak.{int(time.time())}"
    shutil.copy2(PATH, backup)
    print(f"[cleanup] backup: {backup}")

    with open(PATH, 'r', newline='') as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)

    if not fields:
        print(f"[cleanup] empty header in {PATH}")
        return 1

    cleaned = 0
    affected_trade_ids = []
    for row in rows:
        coin = (row.get('coin') or '').strip()
        if not coin or not (coin.startswith('k') and len(coin) >= 4 and coin[1].isupper()):
            continue
        pnl = _parse_float(row.get('pnl'))
        mfe = _parse_float(row.get('mfe_pct'))
        mae = _parse_float(row.get('mae_pct'))
        bogus = (
            (pnl is not None and abs(pnl) > PNL_THRESHOLD)
            or (mfe is not None and abs(mfe) > PCT_THRESHOLD)
            or (mae is not None and abs(mae) > PCT_THRESHOLD)
        )
        if bogus:
            affected_trade_ids.append(row.get('trade_id', '?'))
            row['pnl'] = ''
            row['mfe_pct'] = ''
            row['mae_pct'] = ''
            cleaned += 1

    if cleaned == 0:
        print("[cleanup] no bogus rows found — already clean")
        return 0

    tmp = PATH + '.cleanup.tmp'
    with open(tmp, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, PATH)

    print(f"[cleanup] nulled pnl/mfe/mae on {cleaned} bogus k-coin rows")
    print(f"[cleanup] affected trade_ids: {affected_trade_ids[:10]}"
          f"{' ...' if len(affected_trade_ids) > 10 else ''}")
    print(f"[cleanup] restore via:  mv {backup} {PATH}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
