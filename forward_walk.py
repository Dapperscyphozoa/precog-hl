"""Forward-walk validation: log every closed trade to disk with full context.
Weekly cron re-runs OOS tests against live trade log to detect edge decay.
Triple-check: includes all params needed to reconstruct OOS comparison."""
import time, json, os, threading

LOG_PATH = '/var/data/fwv_trades.jsonl'
LOCK = threading.Lock()

def record_trade(coin, tier, side, entry, exit_price, pnl_pct, 
                 entry_ts, exit_ts, signal_engine, config_snapshot, 
                 leverage, notional_pct, size_mult, equity_before, equity_after):
    """Append single trade to forward-walk log."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        record = {
            'coin': coin, 'tier': tier, 'side': side,
            'entry': entry, 'exit': exit_price, 'pnl_pct': pnl_pct,
            'entry_ts': entry_ts, 'exit_ts': exit_ts,
            'hold_sec': exit_ts - entry_ts if exit_ts > entry_ts else 0,
            'signal_engine': signal_engine,
            'config': config_snapshot,  # TP, SL, sigs, flt, RH, RL
            'leverage': leverage, 'notional_pct': notional_pct,
            'size_mult': size_mult,
            'equity_before': equity_before, 'equity_after': equity_after,
            'equity_delta_pct': (equity_after - equity_before) / max(equity_before, 1e-9),
            'logged_at': time.time(),
        }
        with LOCK:
            with open(LOG_PATH, 'a') as f:
                f.write(json.dumps(record) + '\n')
    except Exception as e:
        pass  # never fail caller for logging

def load_trades(hours_back=168):
    """Load last N hours of trades."""
    if not os.path.exists(LOG_PATH): return []
    cutoff = time.time() - hours_back * 3600
    trades = []
    try:
        with LOCK:
            for line in open(LOG_PATH):
                try:
                    r = json.loads(line)
                    if r.get('logged_at', 0) >= cutoff:
                        trades.append(r)
                except: pass
    except: pass
    return trades

def tier_performance(hours_back=168):
    """Aggregate tier stats over window."""
    trades = load_trades(hours_back)
    by_tier = {}
    for t in trades:
        tier = t.get('tier', 'UNKNOWN')
        by_tier.setdefault(tier, []).append(t)
    out = {}
    for tier, ts in by_tier.items():
        n = len(ts); wins = sum(1 for t in ts if t.get('pnl_pct', 0) > 0)
        total_pnl = sum(t.get('equity_delta_pct', 0) for t in ts)
        wr = wins/n*100 if n else 0
        avg_hold = sum(t.get('hold_sec', 0) for t in ts)/max(n,1) / 60  # minutes
        out[tier] = {
            'trades': n, 'wr_pct': round(wr, 1),
            'total_equity_pnl_pct': round(total_pnl*100, 2),
            'avg_hold_min': round(avg_hold, 1),
        }
    return out

def coin_performance(hours_back=168, min_trades=3):
    """Per-coin stats — detect cold coins for auto-pause."""
    trades = load_trades(hours_back)
    by_coin = {}
    for t in trades:
        by_coin.setdefault(t.get('coin'), []).append(t)
    out = {}
    for coin, ts in by_coin.items():
        if len(ts) < min_trades: continue
        wins = sum(1 for t in ts if t.get('pnl_pct', 0) > 0)
        total = sum(t.get('equity_delta_pct', 0) for t in ts)
        out[coin] = {
            'trades': len(ts), 
            'wr_pct': round(wins/len(ts)*100, 1),
            'pnl_pct': round(total*100, 2),
        }
    return out
