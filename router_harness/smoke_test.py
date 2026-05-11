"""End-to-end smoke: synthetic signals + synthetic walls → harness → output.
Validates the wiring before we deploy the recorder to production.
"""
import os, sys, tempfile, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from router_harness.router import route
from router_harness.harness import run_backtest, compare_variants

# Synthetic data: 2 hours ago, several BTC signals at various entry prices
NOW = time.time()
TWO_H_AGO = NOW - 7200

# Walls: BTC has been sandwiched the whole window
walls_jsonl = '/tmp/test_walls.jsonl'
signals_jsonl = '/tmp/test_signals.jsonl'

with open(walls_jsonl, 'w') as f:
    for t_offset in range(0, 7200, 30):  # one snapshot every 30s
        ts = TWO_H_AGO + t_offset
        rec = {
            'ts': ts, 'coin': 'BTC', 'mid': 80500.0,
            'walls': [
                {'side': 'ask', 'price': 80650, 'usd': 25_000_000, 'persistence_sec': 900},
                {'side': 'bid', 'price': 80350, 'usd': 38_000_000, 'persistence_sec': 1200},
            ]
        }
        f.write(json.dumps(rec) + '\n')

# Signals at various entry prices around BTC
with open(signals_jsonl, 'w') as f:
    cases = [
        # (offset_sec, side, entry_px, engine, expected_router_action)
        (1800, 'BUY',  80520, 'BB_REJ',   'should hit sandwich-MODIFY'),
        (2400, 'SELL', 80480, 'PIVOT',    'should hit sandwich-MODIFY'),
        (3000, 'BUY',  80640, 'LIQ_CSCD', 'too close to ask wall above'),
        (3600, 'SELL', 80360, 'BB_REJ',   'too close to bid wall below'),
    ]
    for off, side, px, eng, _ in cases:
        rec = {'ts': TWO_H_AGO + off, 'coin': 'BTC', 'side': side,
               'engine': eng, 'entry_px': px}
        f.write(json.dumps(rec) + '\n')

# Run the comparator
print('=== running 3-variant comparison on synthetic data ===\n')
result = compare_variants(signals_jsonl, walls_jsonl)
for variant, stats in result.items():
    print(f'--- {variant} ---')
    print(f'  n_signals_scored: {stats["n_signals_scored"]}')
    print(f'  actions: {stats["actions"]}')
    print(f'  120m: {stats["horizons"]["120m"]}')
    print()

# Cleanup
os.remove(walls_jsonl)
os.remove(signals_jsonl)
print('smoke test complete')
