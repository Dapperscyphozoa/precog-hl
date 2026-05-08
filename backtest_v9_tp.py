#!/usr/bin/env python3
"""backtest_v9_tp.py — Compare 1R vs 1.5R vs 2R TP on identical V9-style signals.

Wall proxy: 5m pivot lows (bid wall) / pivot highs (ask wall) with lookback=5.
This stands in for live L2 walls — both represent "level where price reacted
and that survived multiple bars". Persistence (lookback=5) ≈ V9's
min_persistence_polls=5.

Signal pipeline (mirrors V9):
  1. Pivot detected at bar i (confirmed at i+5)
  2. Wait for price to retrace back toward pivot level
  3. Entry on touch (within 0.1% of pivot extreme)
  4. SL beyond pivot wick + 0.2% buffer
  5. R = entry - SL
  6. TP variant: 1.0R / 1.5R / 2.0R (compare all three)
  7. Walk forward, take whichever hits first (SL or TP), max hold 4h
  8. Same fee + slippage assumptions for all variants

Apples-to-apples: identical signals, only TP differs.
"""
import argparse
import json
import sys
import time
import urllib.request
from typing import List, Dict, Tuple

HL_API = 'https://api.hyperliquid.xyz/info'

# Match V9 tier-1..4 + sample of tier-5 to span the universe
COINS = ['BTC','ETH','SOL','HYPE','TON','DOGE','XRP','PUMP','VVV','JTO',
          'AAVE','BNB','LINK','SUI','TAO','WIF','ENA','PENDLE','PAXG',
          'AVAX','APT','ARB','RUNE','SEI','OP','LTC','UNI','TIA','TRX']

DAYS = 7
INTERVAL = '5m'
LOOKBACK = 5            # pivot lookback each side
ENTRY_TOL_PCT = 0.001   # entry triggers when bar.low/high is within 0.1% of pivot
SL_BUFFER_PCT = 0.002   # 0.2% beyond pivot wick
MAX_HOLD_BARS = 48      # 48 × 5m = 4h
COOLDOWN_BARS = 12      # 1h cooldown between same-coin entries
FEE_RT = 0.0009         # round-trip fees (taker bounce: limit-in maker, market-out taker, ~9bps)
SLIP_RT = 0.0010        # 10bp round-trip slippage on bounce limits


def hl_post(body):
    req = urllib.request.Request(HL_API, data=json.dumps(body).encode(),
                                  headers={'Content-Type':'application/json'})
    try: return json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception as e:
        sys.stderr.write(f"hl err: {e}\n"); return None


def fetch(coin, days, interval):
    end = int(time.time()*1000); start = end - days*86400000
    out = []; batch_end = end
    for _ in range(20):
        b = hl_post({'type':'candleSnapshot','req':{'coin':coin,'interval':interval,
                     'startTime':start,'endTime':batch_end}})
        if not b: break
        new = [x for x in b if start <= x['t'] < batch_end]
        if not new: break
        out = new + out
        batch_end = b[0]['t']
        if batch_end <= start: break
        time.sleep(0.1)
    seen = set(); ret = []
    for b in sorted(out, key=lambda x: x['t']):
        if b['t'] in seen: continue
        seen.add(b['t'])
        ret.append({'t':b['t'],'o':float(b['o']),'h':float(b['h']),
                     'l':float(b['l']),'c':float(b['c']),'v':float(b['v'])})
    return ret


def find_pivots(bars: List[dict], lb: int = LOOKBACK):
    """Return (pivot_lows, pivot_highs) as [(idx, price), ...]."""
    plows, phighs = [], []
    for i in range(lb, len(bars) - lb):
        l = bars[i]['l']; h = bars[i]['h']
        win = bars[i-lb : i+lb+1]
        if l == min(b['l'] for b in win): plows.append((i, l))
        if h == max(b['h'] for b in win): phighs.append((i, h))
    return plows, phighs


def simulate(bars, entry_idx, side, entry_px, sl_px, tp_px):
    """Walk forward from entry_idx+1, return (outcome, ret_pct, hold_bars)."""
    end_idx = min(entry_idx + MAX_HOLD_BARS, len(bars) - 1)
    # Apply entry slip (limit fills can have small slip on volatile bars)
    entry_fill = entry_px * (1 + SLIP_RT/2) if side == 'BUY' else entry_px * (1 - SLIP_RT/2)
    for j in range(entry_idx + 1, end_idx + 1):
        bar = bars[j]
        if side == 'BUY':
            if bar['l'] <= sl_px:
                exit_px = sl_px * (1 - SLIP_RT/2)
                ret = (exit_px - entry_fill) / entry_fill - FEE_RT
                return 'SL', ret, j - entry_idx
            if bar['h'] >= tp_px:
                exit_px = tp_px * (1 - SLIP_RT/2)
                ret = (exit_px - entry_fill) / entry_fill - FEE_RT
                return 'TP', ret, j - entry_idx
        else:  # SELL
            if bar['h'] >= sl_px:
                exit_px = sl_px * (1 + SLIP_RT/2)
                ret = (entry_fill - exit_px) / entry_fill - FEE_RT
                return 'SL', ret, j - entry_idx
            if bar['l'] <= tp_px:
                exit_px = tp_px * (1 + SLIP_RT/2)
                ret = (entry_fill - exit_px) / entry_fill - FEE_RT
                return 'TP', ret, j - entry_idx
    last = bars[end_idx]
    if side == 'BUY':
        exit_px = last['c'] * (1 - SLIP_RT/2)
        ret = (exit_px - entry_fill) / entry_fill - FEE_RT
    else:
        exit_px = last['c'] * (1 + SLIP_RT/2)
        ret = (entry_fill - exit_px) / entry_fill - FEE_RT
    return 'TIMEOUT', ret, end_idx - entry_idx


def backtest_coin(coin: str, days: int, tp_multiples: List[float]):
    bars = fetch(coin, days, INTERVAL)
    if not bars or len(bars) < 100:
        return {tp: [] for tp in tp_multiples}

    plows, phighs = find_pivots(bars)
    results = {tp: [] for tp in tp_multiples}

    # Track last entry bar per direction for cooldown
    last_buy_bar = -COOLDOWN_BARS
    last_sell_bar = -COOLDOWN_BARS

    # BUY signals from pivot lows (bid walls)
    for pivot_idx, pivot_low in plows:
        confirmed_at = pivot_idx + LOOKBACK  # need lookback bars after to confirm pivot
        if confirmed_at >= len(bars): continue
        # Search forward for retest entry
        for j in range(confirmed_at, min(confirmed_at + 96, len(bars))):  # max 8h to find retest
            if j - last_buy_bar < COOLDOWN_BARS: break
            bar = bars[j]
            # Invalidation: any close below pivot before retest
            if bar['c'] < pivot_low: break
            # Entry trigger: bar low touched pivot zone
            if bar['l'] <= pivot_low * (1 + ENTRY_TOL_PCT):
                entry_px = pivot_low * (1 + ENTRY_TOL_PCT)
                sl_px = pivot_low * (1 - SL_BUFFER_PCT)
                R = entry_px - sl_px
                if R <= 0: break
                last_buy_bar = j
                for tp_mult in tp_multiples:
                    tp_px = entry_px + tp_mult * R
                    outcome, ret_pct, hold = simulate(bars, j, 'BUY', entry_px, sl_px, tp_px)
                    R_units = ret_pct / (R / entry_px) if (R / entry_px) > 0 else 0
                    results[tp_mult].append({
                        'coin': coin, 'side': 'BUY', 'pivot_idx': pivot_idx,
                        'entry_idx': j, 'entry': entry_px, 'sl': sl_px, 'tp': tp_px,
                        'tp_mult': tp_mult, 'outcome': outcome, 'ret_pct': ret_pct,
                        'R_units': R_units, 'hold_bars': hold,
                    })
                break

    # SELL signals from pivot highs
    for pivot_idx, pivot_high in phighs:
        confirmed_at = pivot_idx + LOOKBACK
        if confirmed_at >= len(bars): continue
        for j in range(confirmed_at, min(confirmed_at + 96, len(bars))):
            if j - last_sell_bar < COOLDOWN_BARS: break
            bar = bars[j]
            if bar['c'] > pivot_high: break
            if bar['h'] >= pivot_high * (1 - ENTRY_TOL_PCT):
                entry_px = pivot_high * (1 - ENTRY_TOL_PCT)
                sl_px = pivot_high * (1 + SL_BUFFER_PCT)
                R = sl_px - entry_px
                if R <= 0: break
                last_sell_bar = j
                for tp_mult in tp_multiples:
                    tp_px = entry_px - tp_mult * R
                    outcome, ret_pct, hold = simulate(bars, j, 'SELL', entry_px, sl_px, tp_px)
                    R_units = ret_pct / (R / entry_px) if (R / entry_px) > 0 else 0
                    results[tp_mult].append({
                        'coin': coin, 'side': 'SELL', 'pivot_idx': pivot_idx,
                        'entry_idx': j, 'entry': entry_px, 'sl': sl_px, 'tp': tp_px,
                        'tp_mult': tp_mult, 'outcome': outcome, 'ret_pct': ret_pct,
                        'R_units': R_units, 'hold_bars': hold,
                    })
                break

    return results


def report(label: str, trades: List[dict]):
    n = len(trades)
    if n == 0:
        print(f"\n=== {label} ===\nNO TRADES"); return
    wins = [t for t in trades if t['outcome'] == 'TP']
    losses = [t for t in trades if t['outcome'] == 'SL']
    timeouts = [t for t in trades if t['outcome'] == 'TIMEOUT']
    wr = len(wins) / n * 100
    avg_R = sum(t['R_units'] for t in trades) / n
    total_R = sum(t['R_units'] for t in trades)
    win_R = sum(t['R_units'] for t in wins) / len(wins) if wins else 0
    loss_R = sum(t['R_units'] for t in losses) / len(losses) if losses else 0
    timeout_R = sum(t['R_units'] for t in timeouts) / len(timeouts) if timeouts else 0
    pf_num = sum(t['R_units'] for t in trades if t['R_units'] > 0)
    pf_den = abs(sum(t['R_units'] for t in trades if t['R_units'] < 0))
    pf = pf_num / pf_den if pf_den > 0 else float('inf')
    avg_hold = sum(t['hold_bars'] for t in trades) / n * 5  # bars × 5m

    print(f"\n=== {label} ===")
    print(f"  Trades:  {n}")
    print(f"  WR:      {wr:.1f}%   ({len(wins)}TP / {len(losses)}SL / {len(timeouts)}T/O)")
    print(f"  EV/trd:  {avg_R:+.3f}R")
    print(f"  Total:   {total_R:+.1f}R")
    print(f"  Win avg: {win_R:+.2f}R    Loss avg: {loss_R:+.2f}R    T/O avg: {timeout_R:+.2f}R")
    print(f"  PF:      {pf:.2f}")
    print(f"  Hold:    {avg_hold:.0f} min avg")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=DAYS)
    ap.add_argument('--coins', type=str, default=','.join(COINS))
    args = ap.parse_args()
    coins = [c.strip().upper() for c in args.coins.split(',') if c.strip()]
    tp_multiples = [1.0, 1.5, 2.0]

    sys.stderr.write(f"Backtest {len(coins)} coins, {args.days}d, TP mults {tp_multiples}\n")
    all_results = {tp: [] for tp in tp_multiples}
    for c in coins:
        sys.stderr.write(f"  {c}... ")
        try:
            r = backtest_coin(c, args.days, tp_multiples)
            for tp, trades in r.items():
                all_results[tp].extend(trades)
            sys.stderr.write(f"{len(r[tp_multiples[0]])} signals\n")
        except Exception as e:
            sys.stderr.write(f"err: {e}\n")

    for tp in tp_multiples:
        report(f"V9-style bounce | TP = {tp}R | {args.days}d × {len(coins)} coins",
                all_results[tp])

    # Per-coin breakdown for 1R variant (the proposed config)
    print(f"\n=== PER-COIN @ TP=1R ===")
    by_coin = {}
    for t in all_results[1.0]:
        by_coin.setdefault(t['coin'], []).append(t)
    print(f"{'coin':8s} {'n':>4s} {'WR':>6s} {'EV/R':>7s} {'tot_R':>7s}")
    for c in sorted(by_coin.keys(), key=lambda c: -sum(t['R_units'] for t in by_coin[c])):
        ts = by_coin[c]
        n = len(ts)
        wr = sum(1 for t in ts if t['outcome']=='TP') / n * 100
        ev = sum(t['R_units'] for t in ts) / n
        tot = sum(t['R_units'] for t in ts)
        print(f"  {c:6s} {n:>4d} {wr:>5.1f}% {ev:>+6.2f}R {tot:>+6.1f}R")

    with open('/tmp/v9_tp_bt.json','w') as f:
        json.dump(all_results, f, default=str)


if __name__ == '__main__':
    main()
