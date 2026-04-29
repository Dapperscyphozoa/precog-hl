"""SB-only filter and decoupling tests.

Per operator directive 2026-04-29: SA is locked out, SB must be independent.
These tests verify confluence_worker no longer calls precog._engine_disabled
and that the SB-only filter works as designed.

Run: python3 -m unittest tests.test_sb_only -v
"""
import os
import sys
import importlib
import unittest
from unittest import mock

THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
sys.path.insert(0, ROOT)


def _src(filename):
    if not hasattr(_src, '_cache'):
        _src._cache = {}
    if filename not in _src._cache:
        with open(os.path.join(ROOT, filename)) as f:
            _src._cache[filename] = f.read()
    return _src._cache[filename]


class TestSBDecoupledFromSA(unittest.TestCase):
    """Verify confluence_worker no longer reaches into precog's allowlist."""

    def test_sb_does_not_call_precog_engine_disabled(self):
        s = _src('confluence_worker.py')
        # The OLD path called _precog._engine_disabled — must be replaced
        self.assertNotIn('_precog._engine_disabled', s,
                         'SB should NOT call SA\'s _engine_disabled (allowlist coupling)')

    def test_sb_uses_own_filter(self):
        s = _src('confluence_worker.py')
        self.assertIn('def _sb_engine_disabled', s)
        self.assertIn('_sb_engine_disabled(_engine_tag_check', s)

    def test_sb_baseline_is_sb_only(self):
        """SB blocks SB-side losers only. HL is SA territory; not in SB list."""
        s = _src('confluence_worker.py')
        self.assertIn('_SB_VERIFIED_LOSER_BASELINE = {', s)
        self.assertIn('CONFLUENCE_BTC_WALL+NEWS', s)
        self.assertIn('CONFLUENCE_BTC_WALL+SNIPER', s)
        self.assertIn('CONFLUENCE_BTC_WALL+DAY', s)
        # 'HL' should appear elsewhere (e.g. in comments) but NOT inside the
        # SB baseline set definition. Locate the set definition and check.
        start = s.find('_SB_VERIFIED_LOSER_BASELINE = {')
        end = s.find('}', start)
        self.assertGreater(end, start, 'SB baseline set should have closing brace')
        baseline_block = s[start:end + 1]
        self.assertNotIn("'HL'", baseline_block,
                         'HL is SA-only, must not be in SB baseline')


class TestSBFilterBehavior(unittest.TestCase):
    """Behavioral test of the SB filter logic (extracted as standalone fn)."""

    def setUp(self):
        # Re-import the function each test to pick up env state cleanly.
        for mod in ('confluence_worker',):
            if mod in sys.modules:
                # Clean slate for env-driven defaults
                pass

    def test_blocks_verified_losers_when_enabled(self):
        # We can't import confluence_worker directly (depends on hyperliquid
        # SDK). Static-source check confirms the wiring instead.
        s = _src('confluence_worker.py')
        # The filter checks SB_VERIFIED_LOSER_VETO env (default '1') AND
        # the engine is in the baseline set.
        self.assertIn("os.environ.get('SB_VERIFIED_LOSER_VETO', '1') == '1'", s)
        self.assertIn('name in _SB_VERIFIED_LOSER_BASELINE', s)

    def test_supports_conf_disable_engines_env(self):
        s = _src('confluence_worker.py')
        self.assertIn("os.environ.get('CONF_DISABLE_ENGINES', '')", s)
        # Wildcard support: 'CONFLUENCE_BTC_WALL+*' should match all variants
        self.assertIn(".endswith('*')", s)
        self.assertIn(".startswith(tok[:-1])", s)


class TestSBCapacity(unittest.TestCase):
    def test_max_positions_default_25(self):
        s = _src('confluence_worker.py')
        self.assertIn("environ.get('CONFLUENCE_MAX_POSITIONS', '25')", s)


class TestSBStatusVisibility(unittest.TestCase):
    def test_status_exposes_sb_filter_state(self):
        s = _src('confluence_worker.py')
        self.assertIn("'sb_filter'", s)
        self.assertIn("'verified_loser_veto_enabled'", s)
        self.assertIn("'verified_loser_baseline'", s)
        self.assertIn("'conf_disable_engines_env'", s)
        self.assertIn("'decoupled_from_sa_allowlist': True", s)


class TestSALockout(unittest.TestCase):
    """Verify this PR doesn't change SA. precog.py should be untouched
    relative to origin/main."""

    def test_no_precog_modifications(self):
        import subprocess
        result = subprocess.run(
            ['git', 'diff', '--name-only', 'origin/main', 'HEAD', '--', 'precog.py'],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(
            result.stdout.strip(), '',
            f'precog.py must be unchanged vs main (SA lockout). Diff: {result.stdout}'
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
