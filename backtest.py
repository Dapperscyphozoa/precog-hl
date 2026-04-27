"""Historical backtest harness for confluence_engine.

Runs the live engine logic against past bars to project per-coin WR/PnL
without waiting for forward shadow-trades to mature.

Caveats:
- Orthogonal systems (LIQ/CVD/OI/SPOOF/WHALE/WALL_ABS/FUND_ARB/NEWS)
  need live state — they fail-soft to None during backtest. Result:
  backtest tests SNIPER/DAY/SWING/FUNDING price-action stack only.
- Fills are perfect (no slippage simulated). Real WR will be 5-10pp lower.
- TP/SL resolution uses bar high/low — assumes worst-case sequencing
  (SL hits first if both touched in same bar).

Usage:
  bt = backtest_coin('BTC', n_bars=300)
  → {n_signals, wins, losses, wr_pct, mean_pnl_pct, ...}

  bt = backtest_universe(['BTC','ETH','SOL'], n_bars=300)
  → {coin: stats}
"""
import time
from collections import defaultdict


COOLDOWN_BARS = 4   # 1h cooldown between same-coin signals (matches live dedupe)
LOOKAHEAD_BARS = 96  # 24h max hold per simulated trade
WARMUP_BARS = 100    # eval_coin needs 100+ bars of history


def _simulate_outcome(bars, entry_idx, side, entry_price, tp_pct, sl_pct):
    """Walk forward from entry_idx to find TP or SL hit.

    Conservative ordering: if both TP and SL touched in same bar, assume
    SL hit first (worst case).

    Returns (outcome, exit_idx, pnl_pct, mfe_pct, mae_pct).
    outcome: 'tp' | 'sl' | 'timeout'
    """
    if entry_price <= 0:
        return ('timeout', entry_idx, 0.0, 0.0, 0.0)

    if side == 'BUY':
        tp_price = entry_price * (1 + tp_pct)
        sl_price = entry_price * (1 - sl_pct)
    else:
        tp_price = entry_price * (1 - tp_pct)
        sl_price = entry_price * (1 + sl_pct)

    mfe = 0.0
    mae = 0.0

    end_idx = min(entry_idx + LOOKAHEAD_BARS, len(bars) - 1)
    for j in range(entry_idx + 1, end_idx + 1):
        b = bars[j]
        try:
            high = float(b['h'])
            low = float(b['l'])
        except (KeyError, TypeError, ValueError):
            continue

        # Update MFE/MAE
        if side == 'BUY':
            cur_mfe = (high - entry_price) / entry_price
            cur_mae = (low - entry_price) / entry_price
        else:
            cur_mfe = (entry_price - low) / entry_price
            cur_mae = (entry_price - high) / entry_price
        if cur_mfe > mfe:
            mfe = cur_mfe
        if cur_mae < mae:
            mae = cur_mae

        # Check SL first (conservative)
        if side == 'BUY':
            if low <= sl_price:
                return ('sl', j, -sl_pct, mfe, mae)
            if high >= tp_price:
                return ('tp', j, tp_pct, mfe, mae)
        else:
            if high >= sl_price:
                return ('sl', j, -sl_pct, mfe, mae)
            if low <= tp_price:
                return ('tp', j, tp_pct, mfe, mae)

    # No TP/SL hit within window
    last_close = float(bars[end_idx]['c']) if bars[end_idx].get('c') else entry_price
    if side == 'BUY':
        timeout_pnl = (last_close - entry_price) / entry_price
    else:
        timeout_pnl = (entry_price - last_close) / entry_price
    return ('timeout', end_idx, timeout_pnl, mfe, mae)


def backtest_coin(coin, n_bars=300):
    """Backtest confluence_engine.eval_coin against historical bars.

    Returns dict with per-coin stats:
      n_signals, wins, losses, timeouts, wr_pct, mean_pnl_pct,
      mean_mfe_pct, mean_mae_pct, by_systems (dict of combo→stats)
    """
    try:
        import okx_fetch
        import confluence_engine as ce
    except ImportError as e:
        return {'err': f'import: {e}'}

    bars_to_request = min(int(n_bars), 300)
    try:
        bars = okx_fetch.fetch_klines(coin, '15m', bars_to_request)
    except Exception as e:
        return {'err': f'fetch: {type(e).__name__}: {e}'}

    if not bars or len(bars) < WARMUP_BARS + 50:
        return {'err': f'insufficient bars (got {len(bars) if bars else 0}, need {WARMUP_BARS + 50})'}

    n_signals = 0
    n_wins = 0
    n_losses = 0
    n_timeouts = 0
    pnl_pcts = []
    mfes = []
    maes = []
    by_systems = defaultdict(lambda: {'n': 0, 'wins': 0, 'losses': 0, 'pnl_sum': 0.0})

    last_signal_idx = -COOLDOWN_BARS  # cooldown gate

    # Walk forward — simulate "now" at each bar
    for i in range(WARMUP_BARS, len(bars) - 50):
        # Cooldown: don't fire if recent signal on same coin
        if i - last_signal_idx < COOLDOWN_BARS:
            continue

        # Slice bars up to current point (inclusive) — engine sees historical view
        window = bars[: i + 1]
        try:
            sig = ce.eval_coin(coin, window, now_ts=int(bars[i].get('t', time.time() * 1000)) // 1000)
        except Exception:
            continue

        if not sig:
            continue
        if sig.get('coin') != coin:
            continue

        # Simulate entry at next bar's open
        if i + 1 >= len(bars):
            break
        entry_bar = bars[i + 1]
        try:
            entry_price = float(entry_bar.get('o', 0))
        except (TypeError, ValueError):
            continue
        if entry_price <= 0:
            continue

        side = sig.get('side')
        tp_pct = float(sig.get('tp_pct', 0.04) or 0.04)
        sl_pct = float(sig.get('sl_pct', 0.015) or 0.015)
        systems = '+'.join(sorted(sig.get('systems') or []))

        outcome, exit_idx, pnl, mfe, mae = _simulate_outcome(
            bars, i + 1, side, entry_price, tp_pct, sl_pct
        )

        n_signals += 1
        pnl_pcts.append(pnl)
        mfes.append(mfe)
        maes.append(mae)

        bs = by_systems[systems]
        bs['n'] += 1
        bs['pnl_sum'] += pnl

        if outcome == 'tp':
            n_wins += 1
            bs['wins'] += 1
        elif outcome == 'sl':
            n_losses += 1
            bs['losses'] += 1
        else:
            n_timeouts += 1

        last_signal_idx = i

    # Aggregate
    decided = n_wins + n_losses
    wr = (n_wins / decided * 100) if decided else None
    mean_pnl = (sum(pnl_pcts) / len(pnl_pcts)) if pnl_pcts else 0.0
    mean_mfe = (sum(mfes) / len(mfes)) if mfes else 0.0
    mean_mae = (sum(maes) / len(maes)) if maes else 0.0

    return {
        'coin': coin,
        'bars_total': len(bars),
        'n_signals': n_signals,
        'wins': n_wins,
        'losses': n_losses,
        'timeouts': n_timeouts,
        'wr_pct': round(wr, 1) if wr is not None else None,
        'mean_pnl_pct': round(mean_pnl * 100, 3),
        'mean_mfe_pct': round(mean_mfe * 100, 3),
        'mean_mae_pct': round(mean_mae * 100, 3),
        'sum_pnl_pct': round(sum(pnl_pcts) * 100, 3) if pnl_pcts else 0.0,
        'by_systems': {
            k: {
                'n': v['n'],
                'wins': v['wins'],
                'losses': v['losses'],
                'wr_pct': round(v['wins'] / (v['wins'] + v['losses']) * 100, 1) if (v['wins'] + v['losses']) else None,
                'mean_pnl_pct': round(v['pnl_sum'] / v['n'] * 100, 3) if v['n'] else 0.0,
            }
            for k, v in by_systems.items()
        },
    }


def backtest_universe(coins, n_bars=300):
    """Run backtest_coin across a list of coins. Returns dict[coin] -> stats."""
    out = {}
    for coin in coins:
        try:
            out[coin] = backtest_coin(coin, n_bars=n_bars)
        except Exception as e:
            out[coin] = {'err': f'{type(e).__name__}: {e}'}
    return out


def rank_promotion_candidates(results, min_n=8, min_wr=60.0):
    """Filter universe results to coins meeting promotion criteria.
    Returns list sorted by mean_pnl_pct descending.
    """
    candidates = []
    for coin, stats in results.items():
        if stats.get('err'):
            continue
        n = stats.get('n_signals', 0)
        wr = stats.get('wr_pct')
        if n >= min_n and wr is not None and wr >= min_wr:
            candidates.append({
                'coin': coin,
                'n': n,
                'wr_pct': wr,
                'mean_pnl_pct': stats.get('mean_pnl_pct', 0),
                'sum_pnl_pct': stats.get('sum_pnl_pct', 0),
                'mean_mfe_pct': stats.get('mean_mfe_pct', 0),
                'mean_mae_pct': stats.get('mean_mae_pct', 0),
            })
    candidates.sort(key=lambda c: -c['mean_pnl_pct'])
    return candidates
