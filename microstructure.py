"""Microstructure annotator — post-hoc classification of entry conditions.

Runs on every closed trade. Pulls the 15m candles around entry and classifies
the entry context into:
- momentum_ignition: price velocity > 1.5× median at entry bar + continued in direction
- absorption: high volume but price held flat after entry
- fake_breakout: price broke signal level, retraced >50% within 3 bars
- clean: standard entry, no special microstructure signature

Logs to /tmp/ms_annotations.json (local to container, survives restarts on standard plan).
Also writes to postmortem KB for long-term storage.

Trigger discussion hook: when N_CLOSED >= 200, print flag to logs.
"""
import json, time, os, urllib.request, threading
from collections import defaultdict

ANNOTATIONS_PATH = os.environ.get('MS_ANNOTATIONS_PATH', '/app/ms_annotations.json')
TRIGGER_THRESHOLD = 200
_LOCK = threading.Lock()
_LOG_PREFIX = '[microstructure]'


def _load_annotations():
    try:
        if os.path.exists(ANNOTATIONS_PATH):
            with open(ANNOTATIONS_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {'annotations': [], 'trigger_fired': False}


def _save_annotations(data):
    try:
        tmp = ANNOTATIONS_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, default=str)
        os.replace(tmp, ANNOTATIONS_PATH)
    except Exception as e:
        print(f"{_LOG_PREFIX} save err: {e}", flush=True)


def _fetch_bars_around(coin, entry_ts_ms, interval='15m', bars_before=10, bars_after=6):
    """Pull bars centered on entry. Returns list of [t,o,h,l,c,v]."""
    ms_per_bar = {'15m': 900_000, '5m': 300_000, '1m': 60_000}[interval]
    start = entry_ts_ms - bars_before * ms_per_bar
    end = entry_ts_ms + bars_after * ms_per_bar
    body = json.dumps({
        'type': 'candleSnapshot',
        'req': {'coin': coin, 'interval': interval, 'startTime': start, 'endTime': end}
    }).encode()
    req = urllib.request.Request('https://api.hyperliquid.xyz/info',
        data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        return [(int(b['t']), float(b['o']), float(b['h']), float(b['l']),
                 float(b['c']), float(b['v'])) for b in data]
    except Exception as e:
        return []


def _classify(entry_bars, signal_bars, side, entry_price, sl_price, tp_price):
    """Return {momentum_ignition, absorption, fake_breakout, label, notes}."""
    if len(signal_bars) < 4 or len(entry_bars) < 10:
        return {'label': 'insufficient_data'}

    # Find the signal bar (bar whose close generated the signal, i.e. the bar
    # just before entry_bars[0]). entry_bars[0] is the first bar AFTER entry.
    # signal_bars = bars_before entry.
    signal_bar = signal_bars[-1]
    sig_t, sig_o, sig_h, sig_l, sig_c, sig_v = signal_bar

    # Median volume over the 10 bars before signal
    vols = [b[5] for b in signal_bars]
    median_vol = sorted(vols)[len(vols)//2]
    vol_ratio = sig_v / median_vol if median_vol > 0 else 1.0

    # Median range (high-low)
    ranges = [b[2] - b[3] for b in signal_bars if b[2] > b[3]]
    median_range = sorted(ranges)[len(ranges)//2] if ranges else 0
    sig_range = sig_h - sig_l
    range_ratio = sig_range / median_range if median_range > 0 else 1.0

    # Velocity = abs(close-open) / range, in signal direction
    sig_body = sig_c - sig_o
    velocity = abs(sig_body) / sig_range if sig_range > 0 else 0
    direction_match = (side == 'BUY' and sig_body > 0) or (side == 'SELL' and sig_body < 0)

    # Post-entry price action (first 3 bars AFTER entry)
    post = entry_bars[:3]
    if not post:
        return {'label': 'insufficient_data'}

    # Did it continue in signal direction in the first bar?
    first_post = post[0]
    continuation = (side == 'BUY' and first_post[4] > entry_price) or \
                   (side == 'SELL' and first_post[4] < entry_price)

    # Did it retrace >50% of the signal bar extension within 3 bars?
    if side == 'BUY':
        max_extension = max(b[2] for b in post) - sig_c  # high-minus-sig-close
        retracement = sig_c - min(b[3] for b in post)
    else:
        max_extension = sig_c - min(b[3] for b in post)
        retracement = max(b[2] for b in post) - sig_c

    if max_extension > 0:
        retrace_pct = retracement / max_extension
    else:
        retrace_pct = 1.0  # No extension at all

    # Flat price action — range of post-entry bars compared to signal bar
    post_range = max(b[2] for b in post) - min(b[3] for b in post)
    post_range_ratio = post_range / sig_range if sig_range > 0 else 1.0

    # Classification rules
    notes = []
    label = 'clean'
    momentum_ignition = False
    absorption = False
    fake_breakout = False

    # Momentum ignition: high vol + high velocity + continuation
    if vol_ratio > 1.5 and velocity > 0.5 and continuation:
        momentum_ignition = True
        label = 'momentum_ignition'
        notes.append(f'vol {vol_ratio:.2f}x velocity {velocity:.2f}')

    # Fake breakout: signal bar had range but price retraced >50% within 3 bars
    if range_ratio > 1.3 and retrace_pct > 0.5 and not continuation:
        fake_breakout = True
        label = 'fake_breakout'
        notes.append(f'range {range_ratio:.2f}x retrace {retrace_pct:.0%}')

    # Absorption: high volume but post-entry range is small (price held flat)
    if vol_ratio > 1.5 and post_range_ratio < 0.7:
        absorption = True
        if label == 'clean':
            label = 'absorption'
        else:
            label = label + '+absorption'
        notes.append(f'vol {vol_ratio:.2f}x post_range {post_range_ratio:.2f}x')

    return {
        'label': label,
        'momentum_ignition': momentum_ignition,
        'absorption': absorption,
        'fake_breakout': fake_breakout,
        'vol_ratio': round(vol_ratio, 2),
        'range_ratio': round(range_ratio, 2),
        'velocity': round(velocity, 2),
        'continuation': continuation,
        'retrace_pct': round(retrace_pct, 2),
        'post_range_ratio': round(post_range_ratio, 2),
        'notes': '; '.join(notes) if notes else 'standard',
    }


def annotate_close(coin, side, entry_price, sl_price, tp_price, entry_ts, pnl_pct,
                   engine=None, regime=None, conf=None, wilson_lb=None):
    """Main hook called from record_close. Non-blocking — spawns thread.

    entry_ts: unix seconds (or ms — we'll detect)
    """
    def _do():
        try:
            ts_ms = int(entry_ts * 1000) if entry_ts < 1e12 else int(entry_ts)
            bars = _fetch_bars_around(coin, ts_ms)
            if not bars or len(bars) < 12:
                return  # silent skip
            # Split into before/after entry
            signal_bars = [b for b in bars if b[0] < ts_ms]
            entry_bars = [b for b in bars if b[0] >= ts_ms]
            if len(signal_bars) < 4 or len(entry_bars) < 3:
                return

            cls = _classify(signal_bars, entry_bars, side, entry_price, sl_price, tp_price)
            cls.update({
                'coin': coin,
                'side': side,
                'entry': entry_price,
                'pnl_pct': round(float(pnl_pct), 3),
                'win': pnl_pct > 0,
                'engine': engine,
                'regime': regime,
                'conf': conf,
                'wilson_lb': wilson_lb,
                'entry_ts': ts_ms,
                'recorded_ts': int(time.time()),
            })

            with _LOCK:
                data = _load_annotations()
                data['annotations'].append(cls)
                # Cap at last 2000 to avoid unbounded growth
                if len(data['annotations']) > 2000:
                    data['annotations'] = data['annotations'][-2000:]
                n = len(data['annotations'])

                # Trigger discussion flag
                if n >= TRIGGER_THRESHOLD and not data.get('trigger_fired'):
                    data['trigger_fired'] = True
                    data['trigger_ts'] = int(time.time())
                    print(f"{_LOG_PREFIX} ★★★ TRIGGER: {n} classified closes accumulated. "
                          f"Ready for microstructure discussion. ★★★", flush=True)

                _save_annotations(data)

            print(f"{_LOG_PREFIX} {coin} {side} pnl={pnl_pct:.2f}% "
                  f"label={cls['label']} [{cls['notes']}] (n_total={n})",
                  flush=True)
        except Exception as e:
            print(f"{_LOG_PREFIX} annotate err {coin}: {e}", flush=True)

    t = threading.Thread(target=_do, daemon=True)
    t.start()


def get_stats():
    """Return summary of accumulated annotations. Used by /microstructure endpoint."""
    with _LOCK:
        data = _load_annotations()
    anns = data.get('annotations', [])
    if not anns:
        return {'total': 0, 'trigger_threshold': TRIGGER_THRESHOLD}

    # WR by microstructure label
    by_label = defaultdict(lambda: {'w': 0, 'l': 0, 'pnl_sum': 0.0})
    by_engine_and_label = defaultdict(lambda: {'w': 0, 'l': 0})
    by_regime_and_label = defaultdict(lambda: {'w': 0, 'l': 0})

    for a in anns:
        label = a.get('label', 'unknown')
        if a.get('win'):
            by_label[label]['w'] += 1
        else:
            by_label[label]['l'] += 1
        by_label[label]['pnl_sum'] += a.get('pnl_pct', 0)

        eng = a.get('engine') or 'unknown'
        reg = a.get('regime') or 'unknown'
        by_engine_and_label[f'{eng}/{label}']['w' if a.get('win') else 'l'] += 1
        by_regime_and_label[f'{reg}/{label}']['w' if a.get('win') else 'l'] += 1

    label_stats = {}
    for label, v in by_label.items():
        n = v['w'] + v['l']
        label_stats[label] = {
            'n': n,
            'wr': round(v['w'] / n, 3) if n else 0,
            'pnl_avg': round(v['pnl_sum'] / n, 3) if n else 0,
        }

    return {
        'total': len(anns),
        'trigger_threshold': TRIGGER_THRESHOLD,
        'trigger_fired': data.get('trigger_fired', False),
        'trigger_ts': data.get('trigger_ts'),
        'by_label': label_stats,
        'by_engine_and_label': {k: v for k, v in by_engine_and_label.items() if sum(v.values()) >= 3},
        'by_regime_and_label': {k: v for k, v in by_regime_and_label.items() if sum(v.values()) >= 3},
    }
