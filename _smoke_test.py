"""Smoke test — imports every module fresh and runs each one's safe init.

Catches NameError, ImportError, syntax bugs that only surface at runtime —
the class of bug that killed confluence_worker for 2h on 2026-04-27 (_os
referenced before its alias was defined).

Usage:
    python _smoke_test.py            # exits 0 on success, 1 on failure
    python _smoke_test.py --verbose  # show each module pass/fail

Run pre-deploy. Cheap to run (~2s total). Catches the bugs that take
hours to diagnose in production.
"""
import importlib
import sys
import traceback


# Modules that should always be importable. Order: leaf → composite.
# Add new modules here when shipped.
CORE_MODULES = [
    'okx_fetch',
    'btc_correlation',
    'btc_macro',
    'cvd_ws',
    'oi_tracker',
    'orderbook_ws',
    'liquidation_ws',
    'whale_filter',
    'spoof_detection',
    'wall_absorption',
    'wall_bounce',
    'wall_exhaustion',
    'funding_engine',
    'funding_filter',
    'funding_accrual',
    'regime_detector',
    'mtf_context',
    'percoin_configs',
    'shadow_trades',
    'trade_ledger',
    'confluence_engine',
    # 'precog' and 'confluence_worker' have side effects on import
    # (start threads, open ports). Don't smoke-test those — verify
    # they at least PARSE via ast.
]

PARSE_ONLY = ['precog', 'confluence_worker']


def main():
    verbose = '--verbose' in sys.argv or '-v' in sys.argv
    failures = []

    for mod in CORE_MODULES:
        try:
            # Force fresh import in case of stale cache
            if mod in sys.modules:
                del sys.modules[mod]
            importlib.import_module(mod)
            if verbose:
                print(f"  ✓ {mod}")
        except Exception as e:
            failures.append((mod, type(e).__name__, str(e)))
            print(f"  ✗ {mod}: {type(e).__name__}: {e}", file=sys.stderr)
            if verbose:
                traceback.print_exc()

    # Parse-only check for side-effecting modules
    import ast
    for mod in PARSE_ONLY:
        path = f"{mod}.py"
        try:
            with open(path, 'r') as f:
                ast.parse(f.read())
            if verbose:
                print(f"  ✓ {mod} (parse-only)")
        except FileNotFoundError:
            failures.append((mod, 'FileNotFoundError', path))
            print(f"  ✗ {mod}: file not found at {path}", file=sys.stderr)
        except SyntaxError as e:
            failures.append((mod, 'SyntaxError', str(e)))
            print(f"  ✗ {mod}: SyntaxError: {e}", file=sys.stderr)

    # Specific spot-checks for known fragile bits
    spot_checks = []

    # 1. confluence_engine module-level constants must be defined
    try:
        import confluence_engine as ce
        for name in ['CONF_MIN_SYS', 'CONF_MIN_DOMAINS', 'COIN_COOLDOWN_S',
                     'COIN_COOLDOWN_FAST_S', 'EVENT_ALONE_ALLOWED', 'SYSTEM_DOMAIN']:
            if not hasattr(ce, name):
                spot_checks.append(f"confluence_engine missing: {name}")
        if 'BTC_WALL' not in ce.SYSTEM_DOMAIN:
            spot_checks.append("confluence_engine.SYSTEM_DOMAIN missing BTC_WALL")
    except Exception as e:
        spot_checks.append(f"confluence_engine spot-check failed: {e}")

    # 2. trade_ledger has the new aggregation helpers
    try:
        import trade_ledger as tl
        for name in ['system_aggregate', 'engine_rolling_wr', 'coin_engine_rolling_wr',
                     'recent_consecutive_losses']:
            if not hasattr(tl, name):
                spot_checks.append(f"trade_ledger missing: {name}")
    except Exception as e:
        spot_checks.append(f"trade_ledger spot-check failed: {e}")

    # 3. btc_macro has the public API
    try:
        import btc_macro as bm
        for name in ['near_resistance', 'near_support', 'wall_broken',
                     'wall_rejected', 'near_wall_summary', 'status']:
            if not hasattr(bm, name):
                spot_checks.append(f"btc_macro missing: {name}")
    except Exception as e:
        spot_checks.append(f"btc_macro spot-check failed: {e}")

    if spot_checks:
        for msg in spot_checks:
            print(f"  ✗ {msg}", file=sys.stderr)
        failures.extend([('spot_check', 'AssertionError', m) for m in spot_checks])

    if failures:
        print(f"\nSMOKE TEST FAILED: {len(failures)} issue(s)", file=sys.stderr)
        for mod, exc, msg in failures:
            print(f"  - {mod}: {exc}: {msg}", file=sys.stderr)
        return 1

    print(f"SMOKE TEST OK: {len(CORE_MODULES)} modules imported, "
          f"{len(PARSE_ONLY)} parsed, all spot-checks pass")
    return 0


if __name__ == '__main__':
    sys.exit(main())
