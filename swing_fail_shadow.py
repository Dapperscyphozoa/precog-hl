"""SWING_FAIL shadow runner — live signal generation, NO live orders.

Loops every SCAN_INTERVAL_S seconds:
  1. Fetch 4h bars for each coin in UNIVERSE
  2. Run swing_fail_engine.detect() on the latest closed bar
  3. Log new signals to /var/data/swing_fail_shadow.jsonl
  4. Advance pending signals — check if TP/SL hit using fresh bars
  5. Apply friction (0.25% RT) + funding accumulation on close
  6. Persist state across restarts via the jsonl

Status visible at /swing_fail_status (Flask endpoint added by precog.py).

Universe: 30 coins from the validated backtest (env override:
SFP_UNIVERSE=BTC,ETH,...).

Mode: shadow_only. NEVER places live orders. Signals are logged for
post-hoc comparison against backtest expectations.
"""
import os
import json
import time
import threading
import urllib.request
from collections import defaultdict

import swing_fail_engine as sfe

LOG_PATH        = os.environ.get('SFP_SHADOW_LOG', '/var/data/swing_fail_shadow.jsonl')
SCAN_INTERVAL_S = int(os.environ.get('SFP_SCAN_INTERVAL_S', '900'))   # 15min
BARS_TO_FETCH   = int(os.environ.get('SFP_BARS_FETCH', '50'))         # need 21+ for lookback=20
HL_INFO_URL     = 'https://api.hyperliquid.xyz/info'

# ─── COIN UNIVERSE TIERS (from validated 50-day backtest) ─────────────
#
# TIER A — VALIDATED WINNERS on HL (run live in shadow + future live):
#   ENS, MEW, LTC, MINA, SOL, UNI, INJ, WIF, HBAR, STX, JUP, BNB, ADA,
#   LINK, NEAR (15 coins, all on HL, all backtest +EV)
#
# TIER B — UNTESTED on HL (run in shadow under SFP_DISCOVERY=1 for
# data collection — these are HL coins NOT in the 50d backtest, edge
# unknown):
#   AAVE, ALT, ANIME, APE, APEX, ASTER, BANANA, BIGTIME, BLAST, CAKE,
#   CC, CRV, DYM, ETC, ETHFI, FTT, GRASS, HMSTR, ICP, IP, JTO, KAS,
#   LAYER, LDO, LINEA, MANTA, MAV, MEGA, MERL, MOVE, NEO, NOT, ORDI,
#   POL, POLYX, PROVE, RSR, SAND, SKR, SNX, STABLE, STRK, SUPER, SUSHI,
#   TAO, TNSR, TRB, TURBO, UMA, USTC, VVV, W, WCT, XLM, XRP, ZEN, ZK,
#   ZRO, kFLOKI (~59 coins)
#
# TIER C — VALIDATED LOSERS on HL (NEVER fire — backtest -EV consistently):
#   SEI, PUMP, TRX, WLFI, OP (5 coins)
#
# TIER D — MARGINAL on HL (small backtest sample, monitor only):
#   S (single backtest trade, near-flat)
#
# Tested in backtest but NOT on HL (informational only — can't trade):
#   BTC, ETH, AVAX, ATOM, FET, TIA, DOGE
#
# Default = TIER A only. Set SFP_DISCOVERY=1 to add TIER B for data
# collection. TIER C is hardcoded BLOCKED regardless of universe env.

TIER_A_WINNERS = [
    'ENS','MEW','LTC','MINA','SOL','UNI','INJ','WIF','HBAR','STX',
    'JUP','BNB','ADA','LINK','NEAR',
]
TIER_B_UNTESTED = [
    'AAVE','ALT','ANIME','APE','APEX','ASTER','BANANA','BIGTIME','BLAST',
    'CAKE','CC','CRV','DYM','ETC','ETHFI','FTT','GRASS','HMSTR','ICP',
    'IP','JTO','KAS','LAYER','LDO','LINEA','MANTA','MAV','MEGA','MERL',
    'MOVE','NEO','NOT','ORDI','POL','POLYX','PROVE','RSR','SAND','SKR',
    'SNX','STABLE','STRK','SUPER','SUSHI','TAO','TNSR','TRB','TURBO',
    'UMA','USTC','VVV','W','WCT','XLM','XRP','ZEN','ZK','ZRO','kFLOKI',
]
TIER_C_LOSERS = ['SEI','PUMP','TRX','WLFI','OP']  # hardcoded block

# Build universe: A always; +B if discovery; -C always
_discovery = os.environ.get('SFP_DISCOVERY', '0') == '1'
_default_universe = list(TIER_A_WINNERS)
if _discovery:
    _default_universe += TIER_B_UNTESTED
UNIVERSE = [c.strip().upper() for c in
            os.environ.get('SFP_UNIVERSE', ','.join(_default_universe)).split(',')
            if c.strip() and c.strip().upper() not in TIER_C_LOSERS]

# ─── REGIME GATE ──────────────────────────────────────────────────────
# Backtest showed SFP +1.03% gross EV in bear-calm but -0.28% in Q4 2024
# bull regime. Mirror failure mode. So: only fire SFP when regime is
# bear-calm or chop. Skip bull regimes entirely.
# Tunable: SFP_REGIME_GATE=0 disables, SFP_ALLOWED_REGIMES overrides list.
_REGIME_GATE_ENABLED = os.environ.get('SFP_REGIME_GATE', '1') == '1'
_ALLOWED_REGIMES = set(
    r.strip().lower() for r in
    os.environ.get('SFP_ALLOWED_REGIMES', 'chop,bear-calm,bear-storm').split(',')
    if r.strip()
)

_LOCK = threading.Lock()
_STATE = {
    'pending': [],          # signals awaiting TP/SL/timeout resolution
    'resolved': [],         # closed signals with PnL
    'last_scan_ts': 0,
    'scans_total': 0,
    'signals_total': 0,
    'tp_hits': 0,
    'sl_hits': 0,
    'timeouts': 0,
    'fetch_errors': 0,
    'started_ts': 0,
}
_RUNNING = False


def _log(msg):
    print(f'[swing_fail_shadow] {msg}', flush=True)


def _append_jsonl(record):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        _log(f'jsonl write err: {e}')


def _load_state():
    if not os.path.exists(LOG_PATH):
        return
    try:
        with open(LOG_PATH) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get('event') == 'signal':
                    _STATE['pending'].append(rec)
                    _STATE['signals_total'] += 1
                elif rec.get('event') == 'resolved':
                    _STATE['resolved'].append(rec)
                    out = rec.get('outcome')
                    if out == 'tp': _STATE['tp_hits'] += 1
                    elif out == 'sl': _STATE['sl_hits'] += 1
                    elif out == 'timeout': _STATE['timeouts'] += 1
                    # Drop matching pending if present
                    tid = rec.get('signal_id')
                    _STATE['pending'] = [p for p in _STATE['pending']
                                         if p.get('signal_id') != tid]
        _log(f'loaded state: pending={len(_STATE["pending"])} '
             f'resolved={len(_STATE["resolved"])}')
    except Exception as e:
        _log(f'load state err: {e}')


def _fetch_bars_4h(coin, n_bars=50):
    """Fetch 4h candles from HL via candleSnapshot.

    Returns:
      list of bars on success
      None on network error (counted in fetch_errors_by_coin)
      []   on empty/non-list response (likely coin unsupported on HL)
    """
    end_ms = int(time.time() * 1000)
    ms_per_bar = 4 * 3600 * 1000
    start_ms = end_ms - n_bars * ms_per_bar
    body = json.dumps({
        'type': 'candleSnapshot',
        'req': {'coin': coin, 'interval': '4h',
                'startTime': start_ms, 'endTime': end_ms}
    }).encode()
    req = urllib.request.Request(
        HL_INFO_URL, data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except Exception as e:
        _STATE['fetch_errors'] += 1
        # Per-coin diagnostic: track WHICH coins fail and why
        with _LOCK:
            _STATE.setdefault('fetch_errors_by_coin', {})
            _STATE['fetch_errors_by_coin'][coin] = (
                _STATE['fetch_errors_by_coin'].get(coin, 0) + 1
            )
        # Log first time per coin to avoid spam, then silent
        if _STATE['fetch_errors_by_coin'][coin] == 1:
            _log(f'fetch err {coin}: {type(e).__name__}: {e}')
        return None
    if not isinstance(data, list):
        # Non-list (e.g., {} or {"error": ...}) — likely unsupported coin
        with _LOCK:
            _STATE.setdefault('empty_responses_by_coin', {})
            _STATE['empty_responses_by_coin'][coin] = (
                _STATE['empty_responses_by_coin'].get(coin, 0) + 1
            )
        if _STATE['empty_responses_by_coin'][coin] == 1:
            _log(f'fetch non-list {coin}: type={type(data).__name__} '
                 f'preview={str(data)[:100]}')
        return []
    if not data:
        with _LOCK:
            _STATE.setdefault('empty_list_by_coin', {})
            _STATE['empty_list_by_coin'][coin] = (
                _STATE['empty_list_by_coin'].get(coin, 0) + 1
            )
        if _STATE['empty_list_by_coin'][coin] == 1:
            _log(f'fetch empty-list {coin}: HL returned [] — coin not on HL?')
        return []
    bars = []
    for k in data:
        try:
            bars.append({
                't': int(k['t']), 'o': float(k['o']), 'h': float(k['h']),
                'l': float(k['l']), 'c': float(k['c']), 'v': float(k.get('v', 0)),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return bars


def _new_signal_id(coin, bar_t):
    return f'{coin}_{bar_t}'


def _current_regime():
    """Cached BTC regime read. None on failure → fail-soft to ALLOW."""
    try:
        import regime_detector as _rd
        return (_rd.get_regime() or '').lower() or None
    except Exception:
        return None


def _scan_once():
    """One full scan cycle. New signals get logged; pending get advanced."""
    now_ts = int(time.time())
    new_signals = 0

    # Regime gate — skip new fires entirely if regime not in allowed set.
    # Pending signals still get advanced (we don't abandon mid-trade).
    regime_blocked = False
    cur_regime = _current_regime()
    if _REGIME_GATE_ENABLED and cur_regime is not None:
        if cur_regime not in _ALLOWED_REGIMES:
            regime_blocked = True
    with _LOCK:
        _STATE['last_regime'] = cur_regime
        _STATE['regime_blocked'] = regime_blocked

    for coin in UNIVERSE:
        bars = _fetch_bars_4h(coin, BARS_TO_FETCH)
        if not bars or len(bars) < 22:
            continue

        # Use the LAST CLOSED bar as candidate. HL returns the open bar
        # too — drop the last if it's still open (current 4h slot).
        # Heuristic: if last bar's t > now - 4h, it's the open bar.
        ms_per_bar = 4 * 3600 * 1000
        last_open_ms = (now_ts // (4 * 3600)) * (4 * 3600) * 1000
        if bars[-1]['t'] >= last_open_ms:
            bars = bars[:-1]
        if len(bars) < 22:
            continue

        candidate = bars[-1]
        sig_id = _new_signal_id(coin, candidate['t'])

        # Skip if we've already logged this signal
        with _LOCK:
            seen_ids = {p.get('signal_id') for p in _STATE['pending']}
            seen_ids |= {r.get('signal_id') for r in _STATE['resolved']}
        if sig_id in seen_ids:
            # Still need to advance pending for this coin if any
            _advance_pending(coin, bars)
            continue

        sig = sfe.detect(bars)
        if sig:
            if regime_blocked:
                # Log the would-have-fired event but don't add to pending
                with _LOCK:
                    _STATE.setdefault('regime_skipped', 0)
                    _STATE['regime_skipped'] += 1
                _log(f'REGIME-SKIP {coin} {sig["side"]} (regime={cur_regime} '
                     f'not in {sorted(_ALLOWED_REGIMES)})')
            else:
                sig['coin'] = coin
                sig['signal_id'] = sig_id
                sig['signal_bar_t'] = candidate['t']
                sig['scan_ts'] = now_ts
                sig['regime_at_entry'] = cur_regime
                sig['tier'] = ('A' if coin in TIER_A_WINNERS
                               else 'B' if coin in TIER_B_UNTESTED
                               else 'D')
                sig['event'] = 'signal'
                with _LOCK:
                    _STATE['pending'].append(sig)
                    _STATE['signals_total'] += 1
                _append_jsonl(sig)
                new_signals += 1
                _log(f'NEW {coin} {sig["side"]} {sig["pattern"]} tier={sig["tier"]} '
                     f'entry={sig["entry_price"]:.6f} swing={sig["swing_level"]:.6f} '
                     f'wick/body={sig["wick_body_ratio"]:.2f}x')

        # Advance any pending signals on this coin (always — even if regime blocked,
        # don't abandon mid-trade entries)
        _advance_pending(coin, bars)

    with _LOCK:
        _STATE['last_scan_ts'] = now_ts
        _STATE['scans_total'] += 1
    return new_signals


def _advance_pending(coin, bars_4h):
    """Check pending signals on this coin against new bars."""
    if not bars_4h:
        return
    with _LOCK:
        pending_for_coin = [p for p in _STATE['pending'] if p.get('coin') == coin]

    for sig in pending_for_coin:
        entry_t = sig.get('signal_bar_t', 0)
        # Bars after entry
        post_bars = [b for b in bars_4h if b['t'] > entry_t]
        if not post_bars:
            continue

        # Use simulate_trade — but slice bars starting AT entry bar (so
        # index 0 = entry, index 1+ = post)
        sim_bars = [{'t': entry_t, 'o': sig['entry_price'],
                     'h': sig['entry_price'], 'l': sig['entry_price'],
                     'c': sig['entry_price']}] + post_bars

        # Cap at 24 bars (96h) for max hold
        n_lookahead = min(24, len(sim_bars))
        outcome, exit_idx, gross, net, mfe, mae = sfe.simulate_trade(
            sim_bars, sig, n_lookahead_bars=n_lookahead
        )

        if outcome == 'timeout' and len(post_bars) < 24:
            # Not yet at 96h — leave pending
            continue

        # Resolved
        resolved = {
            'event': 'resolved',
            'signal_id': sig.get('signal_id'),
            'coin': coin,
            'side': sig.get('side'),
            'pattern': sig.get('pattern'),
            'entry_price': sig.get('entry_price'),
            'entry_bar_t': entry_t,
            'exit_bar_t': post_bars[exit_idx - 1]['t'] if exit_idx > 0 else entry_t,
            'outcome': outcome,
            'gross_pnl_pct': gross,
            'net_pnl_pct': net,
            'mfe_pct': mfe,
            'mae_pct': mae,
            'resolved_ts': int(time.time()),
        }
        _append_jsonl(resolved)
        with _LOCK:
            _STATE['resolved'].append(resolved)
            _STATE['pending'] = [p for p in _STATE['pending']
                                 if p.get('signal_id') != sig.get('signal_id')]
            if outcome == 'tp': _STATE['tp_hits'] += 1
            elif outcome == 'sl': _STATE['sl_hits'] += 1
            else: _STATE['timeouts'] += 1
        _log(f'RESOLVED {coin} {outcome} gross={gross*100:+.2f}% net={net*100:+.2f}%')


def _loop():
    global _RUNNING
    _RUNNING = True
    _STATE['started_ts'] = int(time.time())
    _log(f'started: universe={len(UNIVERSE)} coins, scan_interval={SCAN_INTERVAL_S}s')
    _load_state()
    while True:
        try:
            n = _scan_once()
            if n > 0:
                _log(f'scan complete: {n} new signals')
        except Exception as e:
            _log(f'scan error: {type(e).__name__}: {e}')
        time.sleep(SCAN_INTERVAL_S)


def start():
    """Spawn the shadow runner thread. Idempotent."""
    if _RUNNING:
        _log('already running')
        return
    t = threading.Thread(target=_loop, name='swing-fail-shadow', daemon=True)
    t.start()
    _log('thread launched')


def status():
    """For /swing_fail_status endpoint."""
    with _LOCK:
        resolved = list(_STATE['resolved'])
        pending = list(_STATE['pending'])
        scans = _STATE['scans_total']
        last_ts = _STATE['last_scan_ts']
        tp = _STATE['tp_hits']
        sl = _STATE['sl_hits']
        to = _STATE['timeouts']
        sigs = _STATE['signals_total']
        errs = _STATE['fetch_errors']
        started = _STATE['started_ts']
        regime = _STATE.get('last_regime')
        regime_blocked = _STATE.get('regime_blocked', False)
        regime_skipped = _STATE.get('regime_skipped', 0)

    n_resolved = len(resolved)
    decided = tp + sl
    wr = (tp / decided * 100) if decided else None
    gross_sum = sum(r.get('gross_pnl_pct', 0) for r in resolved)
    net_sum = sum(r.get('net_pnl_pct', 0) for r in resolved)
    gross_mean = (gross_sum / n_resolved * 100) if n_resolved else 0
    net_mean = (net_sum / n_resolved * 100) if n_resolved else 0

    by_coin = defaultdict(lambda: {'n': 0, 'tp': 0, 'sl': 0, 'to': 0,
                                    'gross_sum': 0, 'net_sum': 0})
    for r in resolved:
        c = r.get('coin', '?')
        b = by_coin[c]
        b['n'] += 1
        out = r.get('outcome')
        if out == 'tp': b['tp'] += 1
        elif out == 'sl': b['sl'] += 1
        else: b['to'] += 1
        b['gross_sum'] += r.get('gross_pnl_pct', 0)
        b['net_sum'] += r.get('net_pnl_pct', 0)

    # Per-tier breakdown
    by_tier = defaultdict(lambda: {'n': 0, 'tp': 0, 'sl': 0, 'gross_sum': 0, 'net_sum': 0})
    for r in resolved:
        c = r.get('coin', '?')
        tier = ('A' if c in TIER_A_WINNERS
                else 'B' if c in TIER_B_UNTESTED
                else 'D')
        b = by_tier[tier]
        b['n'] += 1
        out = r.get('outcome')
        if out == 'tp': b['tp'] += 1
        elif out == 'sl': b['sl'] += 1
        b['gross_sum'] += r.get('gross_pnl_pct', 0)
        b['net_sum'] += r.get('net_pnl_pct', 0)

    return {
        'mode': 'shadow_only',
        'engine': 'SWING_FAIL_4H',
        'config': sfe.status(),
        'universe_size': len(UNIVERSE),
        'universe': UNIVERSE,
        'tier_a_winners': TIER_A_WINNERS,
        'tier_b_untested_count': len(TIER_B_UNTESTED),
        'tier_c_blocked': TIER_C_LOSERS,
        'discovery_mode': _discovery,
        'regime_gate_enabled': _REGIME_GATE_ENABLED,
        'allowed_regimes': sorted(_ALLOWED_REGIMES),
        'current_regime': regime,
        'regime_blocked_now': regime_blocked,
        'regime_skipped_total': regime_skipped,
        'scans_total': scans,
        'last_scan_ts': last_ts,
        'last_scan_age_sec': int(time.time() - last_ts) if last_ts else None,
        'started_ts': started,
        'uptime_sec': int(time.time() - started) if started else 0,
        'fetch_errors': errs,
        'fetch_errors_by_coin': dict(_STATE.get('fetch_errors_by_coin', {})),
        'empty_responses_by_coin': dict(_STATE.get('empty_responses_by_coin', {})),
        'empty_list_by_coin': dict(_STATE.get('empty_list_by_coin', {})),
        'signals_total': sigs,
        'pending': len(pending),
        'resolved': n_resolved,
        'tp_hits': tp,
        'sl_hits': sl,
        'timeouts': to,
        'wr_pct': round(wr, 1) if wr is not None else None,
        'gross_mean_pct': round(gross_mean, 3),
        'net_mean_pct': round(net_mean, 3),
        'by_tier': {
            t: {
                'n': v['n'], 'tp': v['tp'], 'sl': v['sl'],
                'wr_pct': round(v['tp'] / (v['tp'] + v['sl']) * 100, 1)
                          if (v['tp'] + v['sl']) else None,
                'gross_mean_pct': round(v['gross_sum'] / v['n'] * 100, 3) if v['n'] else 0,
                'net_mean_pct': round(v['net_sum'] / v['n'] * 100, 3) if v['n'] else 0,
            }
            for t, v in by_tier.items()
        },
        'by_coin': {
            c: {
                'n': v['n'], 'tp': v['tp'], 'sl': v['sl'], 'to': v['to'],
                'wr_pct': round(v['tp'] / (v['tp'] + v['sl']) * 100, 1)
                          if (v['tp'] + v['sl']) else None,
                'gross_mean_pct': round(v['gross_sum'] / v['n'] * 100, 3) if v['n'] else 0,
                'net_mean_pct': round(v['net_sum'] / v['n'] * 100, 3) if v['n'] else 0,
            }
            for c, v in by_coin.items()
        },
    }
