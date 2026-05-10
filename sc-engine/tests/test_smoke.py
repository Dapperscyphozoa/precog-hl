"""End-to-end smoke test using synthetic OHLC data.

Validates that the full pipeline (data → primitives → confluence →
backtest engine → metrics) runs without errors on representative
trending/ranging price data. Does NOT validate that signals are
+EV — that's the user's job once real data lands.

Run: python3 -m unittest tests.test_smoke -v
"""
import os, sys, math
import unittest
import numpy as np
import pandas as pd

THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
sys.path.insert(0, ROOT)


def make_synthetic_ohlc(n_bars: int = 1000, freq: str = '15min',
                        trend: float = 0.0, vol: float = 0.5,
                        seed: int = 42) -> pd.DataFrame:
    """Geometric brownian motion with drift. Realistic OHLC distribution."""
    rng = np.random.default_rng(seed)
    dt = 1 / (60 * 24)  # 15min as fraction of day
    drift = trend
    sigma = vol
    price = 2000.0  # start at gold-like price
    rows = []
    ts0 = pd.Timestamp('2024-01-01', tz='UTC')
    for i in range(n_bars):
        # Per-bar log return
        r = drift * dt + sigma * math.sqrt(dt) * rng.standard_normal()
        new_price = price * math.exp(r)
        # Make OHLC from intra-bar volatility (~half the inter-bar)
        intra = sigma * math.sqrt(dt) * 0.5
        o = price
        c = new_price
        wick_up = abs(rng.standard_normal()) * intra * price
        wick_dn = abs(rng.standard_normal()) * intra * price
        h = max(o, c) + wick_up
        l = min(o, c) - wick_dn
        rows.append({'Open': o, 'High': h, 'Low': l, 'Close': c, 'Volume': 1000})
        price = new_price
    df = pd.DataFrame(rows, index=pd.date_range(ts0, periods=n_bars, freq=freq))
    return df


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample 15min OHLC up to higher TFs."""
    o = df['Open'].resample(rule).first()
    h = df['High'].resample(rule).max()
    l = df['Low'].resample(rule).min()
    c = df['Close'].resample(rule).last()
    v = df['Volume'].resample(rule).sum() if 'Volume' in df.columns else None
    out = pd.DataFrame({'Open': o, 'High': h, 'Low': l, 'Close': c}).dropna()
    if v is not None:
        out['Volume'] = v.dropna()
    return out
class TestPrimitives(unittest.TestCase):
    def setUp(self):
        self.df = make_synthetic_ohlc(2000, freq='15min', vol=0.4)

    def test_pivots_detected(self):
        from primitives import detect_pivots
        pivots = detect_pivots(self.df, lookback=5)
        self.assertGreater(len(pivots), 5,
                           'should find multiple pivots in 2000 bars of synthetic data')
        kinds = set(p.kind for p in pivots)
        self.assertTrue(kinds.issubset({'HH', 'HL', 'LH', 'LL'}))

    def test_structure_state_no_lookahead(self):
        from primitives import structure_at
        # State at bar 1500 should not include pivots from bar > 1500
        st = structure_at(self.df, 1500, lookback=5)
        for p in st.pivots:
            self.assertLessEqual(p.idx + 5, 1500)
        self.assertIn(st.trend, ('up', 'down', 'range'))

    def test_zones_detected(self):
        from primitives import detect_order_blocks
        zones = detect_order_blocks(self.df, impulse_atr_mult=1.0)
        # Some zones should be found in 2000 bars
        self.assertGreater(len(zones), 0)
        for z in zones:
            self.assertIn(z.side, ('bullish', 'bearish'))
            self.assertGreater(z.high, z.low)

    def test_sweeps_detected(self):
        from primitives import detect_sweeps
        sweeps = detect_sweeps(self.df, pivot_lookback=5, min_wick_ratio=0.5)
        # Sweeps may or may not exist on random data; just verify it runs
        for s in sweeps:
            self.assertIn(s.side, ('buy_side', 'sell_side'))

    def test_fvgs_detected(self):
        from primitives import detect_fvgs
        fvgs = detect_fvgs(self.df)
        for f in fvgs:
            self.assertIn(f.side, ('bullish', 'bearish'))
            self.assertGreaterEqual(f.high, f.low)

    def test_mss_runs(self):
        from primitives.mss import mss_at
        # Just verify it doesn't crash on a range of bars
        for i in [100, 500, 1000, 1500]:
            res = mss_at(self.df, i)
            self.assertTrue(res is None or res.direction in ('up', 'down'))


class TestConfluence(unittest.TestCase):
    def test_signal_generation_no_crash(self):
        from primitives.confluence import generate_signal
        ltf = make_synthetic_ohlc(2000, '15min', vol=0.4)
        mtf = resample(ltf, '1h')
        htf = resample(ltf, '1D')
        # Walk forward and check no exception
        signals = []
        for i in range(50, 200):
            sig = generate_signal('TEST', htf, mtf, ltf, i)
            if sig is not None:
                signals.append(sig)
        # Verify signal shape if any
        for s in signals:
            self.assertIn(s.direction, ('long', 'short'))
            self.assertGreater(s.tp, 0)
            self.assertGreater(s.sl, 0)


class TestBacktest(unittest.TestCase):
    def test_backtest_runs_end_to_end(self):
        from backtest.engine import run_backtest
        ltf = make_synthetic_ohlc(2000, '15min', vol=0.4)
        mtf = resample(ltf, '1h')
        htf = resample(ltf, '1D')
        result = run_backtest('TEST', htf, mtf, ltf,
                              max_bars_held=100, cooldown_bars=3)
        m = result.metrics()
        self.assertIn('n', m)
        # Metrics should have expected keys
        for k in ('wr_pct', 'pf', 'sharpe', 'mdd_R', 'expectancy_R', 'total_R'):
            self.assertIn(k, m)


class TestDataLoader(unittest.TestCase):
    def test_csv_roundtrip(self):
        from data import loader
        df = make_synthetic_ohlc(100, '1h')
        loader.save_csv(df, 'TEST=X', '1h')
        df2 = loader.load_csv('TEST=X', '1h')
        self.assertIsNotNone(df2)
        self.assertEqual(len(df), len(df2))
        # cleanup
        os.remove(loader._csv_path('TEST=X', '1h'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
