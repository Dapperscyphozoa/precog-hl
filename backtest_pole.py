#!/usr/bin/env python3
"""backtest_pole.py — Backtest pole_engine on Hyperliquid historical data.

Walks bars chronologically, calls pole_engine.detect() on each rolling
window, simulates entry at signal close, walks forward until SL or TP hits
(whichever first) or max_hold expires. Records WR, EV, PnL.

Usage:
  python3 backtest_pole.py [--days 60] [--coins BTC,ETH,SOL,...]
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

import pole_engine

HL_API = 'https://api.hyperliquid.xyz/info'

DEFAULT_COINS = ['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'ADA', 'AVAX', 'DOGE',
                 'LINK', 'DOT', 'MATIC', 'LTC', 'NEAR', 'APT']

FEE_RT_PCT = 0.0009    # 0.09% (HL 4.5bps × 2)
SLIP_RT_PCT = 0.0016   # 0.16% per leg, see shadow_trades.py


def hl_post(body):
    req = urllib.request.Request(
        HL_API,
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'},
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception as e:
        sys.stderr.write(f"hl post err: {e}\n")
        return None


def fetch_candles(coin, interval, days):
    """Pull historical candles from HL with pagination."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    all_bars = []
    batch_end = end_ms
    for _ in range(20):
        batch = hl_post({
            'type': 'candleSnapshot',
            'req': {'coin': coin, 'interval': interval,
                    'startTime': start_ms, 'endTime': batch_end},
        })
        if not batch:
            break
        new = [b for b in batch if start_ms <= b['t'] < batch_end]
        if not new:
            break
        all_bars = new + all_bars
        batch_end = batch[0]['t']
        if batch_end <= start_ms:
            break
        time.sleep(0.1)

    seen = set()
    out = []
    for b in sorted(all_bars, key=lambda x: x['t']):
        if b['t'] in seen:
            continue
        seen.add(b['t'])
        out.append({
            't': b['t'], 'o': float(b['o']), 'h': float(b['h']),
            'l': float(b['l']), 'c': float(b['c']), 'v': float(b['v']),
        })
    return out


def find_bar_idx(bars, ts):
    """Index of bar containing ts (or first bar after)."""
    for i, b in enumerate(bars):
        if b['t'] >= ts:
            return i
    return -1


def simulate_trade(signal, bars_15m, signal_idx):
    """Walk forward from signal_idx+1 until SL or TP or max_hold."""
    side = signal['side']
    entry = signal['entry']
    sl = signal['sl']
    tp = signal['tp']
    max_hold_bars = signal['max_hold_s'] // 900  # 15m bars
    end_idx = min(signal_idx + max_hold_bars, len(bars_15m) - 1)

    # Apply entry slippage
    if side == 'BUY':
        entry_fill = entry * (1 + SLIP_RT_PCT / 2)
    else:
        entry_fill = entry * (1 - SLIP_RT_PCT / 2)

    for j in range(signal_idx + 1, end_idx + 1):
        bar = bars_15m[j]
        if side == 'BUY':
            # SL hit?
            if bar['l'] <= sl:
                exit_px = sl * (1 - SLIP_RT_PCT / 2)  # adverse slip on stop
                ret_pct = (exit_px - entry_fill) / entry_fill - FEE_RT_PCT
                return {'outcome': 'SL', 'ret_pct': ret_pct, 'exit_px': exit_px,
                        'hold_bars': j - signal_idx, 'exit_t': bar['t']}
            # TP hit?
            if bar['h'] >= tp:
                exit_px = tp * (1 - SLIP_RT_PCT / 2)
                ret_pct = (exit_px - entry_fill) / entry_fill - FEE_RT_PCT
                return {'outcome': 'TP', 'ret_pct': ret_pct, 'exit_px': exit_px,
                        'hold_bars': j - signal_idx, 'exit_t': bar['t']}
        else:  # SELL
            if bar['h'] >= sl:
                exit_px = sl * (1 + SLIP_RT_PCT / 2)
                ret_pct = (entry_fill - exit_px) / entry_fill - FEE_RT_PCT
                return {'outcome': 'SL', 'ret_pct': ret_pct, 'exit_px': exit_px,
                        'hold_bars': j - signal_idx, 'exit_t': bar['t']}
            if bar['l'] <= tp:
                exit_px = tp * (1 + SLIP_RT_PCT / 2)
                ret_pct = (entry_fill - exit_px) / entry_fill - FEE_RT_PCT
                return {'outcome': 'TP', 'ret_pct': ret_pct, 'exit_px': exit_px,
                        'hold_bars': j - signal_idx, 'exit_t': bar['t']}

    # Time out — close at last bar
    last = bars_15m[end_idx]
    if side == 'BUY':
        exit_px = last['c'] * (1 - SLIP_RT_PCT / 2)
        ret_pct = (exit_px - entry_fill) / entry_fill - FEE_RT_PCT
    else:
        exit_px = last['c'] * (1 + SLIP_RT_PCT / 2)
        ret_pct = (entry_fill - exit_px) / entry_fill - FEE_RT_PCT
    return {'outcome': 'TIMEOUT', 'ret_pct': ret_pct, 'exit_px': exit_px,
            'hold_bars': end_idx - signal_idx, 'exit_t': last['t']}


def backtest_coin(coin, days, verbose=False):
    """Backtest pole_engine on one coin."""
    sys.stderr.write(f"  fetching {coin}...\n")
    b15 = fetch_candles(coin, '15m', days)
    b1h = fetch_candles(coin, '1h', days)
    b4h = fetch_candles(coin, '4h', days + 30)  # extra warmup for 4h
    if not b15 or len(b15) < 200:
        sys.stderr.write(f"  {coin}: insufficient 15m data ({len(b15) if b15 else 0})\n")
        return []

    trades = []
    pole_engine.reset_cooldowns()  # fresh per coin

    # Walk: for each 15m bar i (after warmup), call detect with bars_15m[:i+1]
    warmup = 100
    last_signal_bar = -100
    cooldown_bars = 4  # enforce 1h cooldown between SAME-coin entries

    for i in range(warmup, len(b15)):
        if i - last_signal_bar < cooldown_bars:
            continue
        # Build aligned 1h/4h windows up to this point in time
        cur_t = b15[i]['t']
        b1h_view = [b for b in b1h if b['t'] <= cur_t]
        b4h_view = [b for b in b4h if b['t'] <= cur_t]
        if len(b1h_view) < 50 or len(b4h_view) < 30:
            continue

        b15_view = b15[:i + 1]
        sig = pole_engine.detect(coin, b15_view, b1h_view, b4h_view, now_ts_ms=cur_t)
        if not sig:
            continue

        # Simulate forward
        result = simulate_trade(sig, b15, i)
        result['coin'] = coin
        result['signal_t'] = cur_t
        result['side'] = sig['side']
        result['entry'] = sig['entry']
        result['sl'] = sig['sl']
        result['tp'] = sig['tp']
        result['rr'] = sig['rr']
        result['swept_kind'] = sig['swept_pole']['kind']
        result['swept_tf'] = sig['swept_pole']['tf']
        result['target_kind'] = sig['target_pole']['kind']
        result['confluences'] = sig['confluences']
        trades.append(result)
        last_signal_bar = i

        if verbose:
            t = datetime.fromtimestamp(cur_t / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
            sys.stderr.write(f"    {t} {coin} {sig['side']} swept={sig['swept_pole']['kind']} "
                             f"target={sig['target_pole']['kind']} rr={sig['rr']:.2f} "
                             f"=> {result['outcome']} {result['ret_pct']*100:+.2f}%\n")

    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=60)
    ap.add_argument('--coins', type=str, default=','.join(DEFAULT_COINS))
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(',') if c.strip()]
    sys.stderr.write(f"Backtesting {len(coins)} coins over {args.days}d...\n")

    all_trades = []
    for c in coins:
        trades = backtest_coin(c, args.days, verbose=args.verbose)
        all_trades.extend(trades)
        sys.stderr.write(f"  {c}: {len(trades)} trades\n")

    # Aggregate
    n = len(all_trades)
    if n == 0:
        print("NO TRADES")
        return

    wins = [t for t in all_trades if t['ret_pct'] > 0]
    losses = [t for t in all_trades if t['ret_pct'] <= 0]
    wr = len(wins) / n * 100
    avg_w = sum(t['ret_pct'] for t in wins) / len(wins) * 100 if wins else 0
    avg_l = sum(t['ret_pct'] for t in losses) / len(losses) * 100 if losses else 0
    ev = sum(t['ret_pct'] for t in all_trades) / n * 100
    total_pct = sum(t['ret_pct'] for t in all_trades) * 100

    # Outcome breakdown
    by_outcome = {}
    for t in all_trades:
        by_outcome.setdefault(t['outcome'], []).append(t)

    print(f"\n=== POLE ENGINE BACKTEST — {args.days}d, {len(coins)} coins ===")
    print(f"Trades: {n}")
    print(f"WR:     {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"Avg W:  {avg_w:+.2f}%   Avg L: {avg_l:+.2f}%")
    print(f"EV:     {ev:+.3f}% per trade")
    print(f"Total:  {total_pct:+.2f}%")
    print(f"\nBy outcome:")
    for o, ts in sorted(by_outcome.items(), key=lambda kv: -len(kv[1])):
        ow = sum(1 for t in ts if t['ret_pct'] > 0)
        print(f"  {o:8s} n={len(ts):3d}  WR={ow/len(ts)*100:5.1f}%  "
              f"avg={sum(t['ret_pct'] for t in ts)/len(ts)*100:+.2f}%")

    print(f"\nBy swept-pole kind:")
    by_kind = {}
    for t in all_trades:
        by_kind.setdefault(t['swept_kind'], []).append(t)
    for k, ts in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        ow = sum(1 for t in ts if t['ret_pct'] > 0)
        print(f"  {k:8s} n={len(ts):3d}  WR={ow/len(ts)*100:5.1f}%  "
              f"avg={sum(t['ret_pct'] for t in ts)/len(ts)*100:+.2f}%")

    print(f"\nBy timeframe:")
    by_tf = {}
    for t in all_trades:
        by_tf.setdefault(t['swept_tf'], []).append(t)
    for tf, ts in sorted(by_tf.items(), key=lambda kv: -len(kv[1])):
        ow = sum(1 for t in ts if t['ret_pct'] > 0)
        print(f"  {tf:4s} n={len(ts):3d}  WR={ow/len(ts)*100:5.1f}%  "
              f"avg={sum(t['ret_pct'] for t in ts)/len(ts)*100:+.2f}%")

    print(f"\nEngine stats: {pole_engine.status()}")

    # Dump raw
    with open('/tmp/pole_backtest.json', 'w') as f:
        json.dump(all_trades, f, indent=2)
    print(f"\nRaw trades: /tmp/pole_backtest.json")


if __name__ == '__main__':
    main()
