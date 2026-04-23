"""Red team defenses — cheap structural safeguards.

Three low-complexity, non-empirical safeguards that address confirmed
failure modes from adversarial analysis:

1. CHOP_COOLDOWN_MULT: 2× signal cooldown when regime=chop (prevents Zone C
   signal clustering decay)
2. regime_staleness_ok(): verify BTC 1h candle is fresh (< 65 min old) before
   trusting regime classification (prevents Zone 8 stale regime bug)
3. funding_exit_check(): audit-only log of whether funding-aware exit logic
   is wired (exposes Zone 5 silent drift)

All callable from precog.py signal path. None alter signal semantics beyond
cooldown extension. Non-blocking; fail-open (default to "allow").
"""
import time, os, urllib.request, json, threading

_LOG_PREFIX = '[red_team]'
_FUNDING_AUDIT_DONE = False


def chop_cooldown_multiplier(regime):
    """Return cooldown multiplier given current regime.

    chop: 2.0 (extend cooldown to prevent Zone C clustering)
    all other: 1.0 (baseline)
    """
    if regime == 'chop':
        return 2.0
    return 1.0


def extended_cooldown(base_cd_ms, regime):
    """Convenience: return extended cooldown value."""
    return int(base_cd_ms * chop_cooldown_multiplier(regime))


def regime_staleness_ok(max_age_sec=3900):
    """Verify BTC 1h candle freshness. Fail-open on error (don't block trading).

    Returns True if fresh or unknown, False only if confirmed stale.
    Staleness threshold: 65 min (3900s). A 1h candle updates every 60 min;
    allow 5 min buffer.
    """
    try:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - 5 * 3600_000
        body = json.dumps({
            'type': 'candleSnapshot',
            'req': {'coin': 'BTC', 'interval': '1h',
                    'startTime': start_ms, 'endTime': end_ms}
        }).encode()
        req = urllib.request.Request('https://api.hyperliquid.xyz/info',
            data=body, method='POST',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=4) as r:
            bars = json.loads(r.read())
        if not bars:
            return True  # fail-open
        latest_ts = max(int(b['t']) for b in bars)
        age_sec = (end_ms - latest_ts) / 1000
        if age_sec > max_age_sec:
            print(f"{_LOG_PREFIX} BTC 1h stale: age={age_sec:.0f}s > {max_age_sec}s", flush=True)
            return False
        return True
    except Exception:
        return True  # fail-open on network errors


def funding_exit_audit():
    """Audit-only log of funding-aware exit wiring. Runs once at startup.

    Checks if the codebase has the funding-monitor daemon thread registered.
    Does NOT modify behavior; just logs presence/absence to expose silent drift.
    """
    global _FUNDING_AUDIT_DONE
    if _FUNDING_AUDIT_DONE: return
    _FUNDING_AUDIT_DONE = True

    def _do():
        try:
            # Check for funding-related modules in runtime
            checks = {}
            try:
                import funding_filter
                checks['funding_filter_module'] = True
            except Exception:
                checks['funding_filter_module'] = False
            try:
                import funding_arb
                checks['funding_arb_module'] = True
            except Exception:
                checks['funding_arb_module'] = False

            # Check for any thread with 'funding' in its name
            funding_threads = [t.name for t in threading.enumerate() if 'funding' in t.name.lower()]
            checks['funding_daemon_threads'] = funding_threads or ['NONE']

            # Check env toggle
            checks['FUNDING_EXIT_ENABLED'] = os.environ.get('FUNDING_EXIT_ENABLED', '<unset>')
            checks['FUNDING_CUT_PNL_RATIO'] = os.environ.get('FUNDING_CUT_PNL_RATIO', '<unset>')

            print(f"{_LOG_PREFIX} funding_exit_audit: {checks}", flush=True)
        except Exception as e:
            print(f"{_LOG_PREFIX} funding audit err: {e}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


def status():
    """Return red team defense state for /red_team endpoint."""
    return {
        'chop_cooldown_mult': 2.0,
        'regime_staleness_threshold_sec': 3900,
        'funding_exit_audit_fired': _FUNDING_AUDIT_DONE,
        'defenses_active': {
            'chop_cooldown_extension': 'live (2x CD_MS when regime=chop)',
            'regime_staleness_check': 'live (BTC 1h freshness gate)',
            'funding_exit_audit': 'startup log only (see server logs for details)',
        },
        'failure_modes_addressed': {
            'zone_7_signal_clustering': 'chop_cooldown_mult',
            'zone_8_stale_regime_data': 'regime_staleness_ok()',
            'zone_5_funding_bleed': 'funding_exit_audit() (exposure only)',
        },
        'failure_modes_not_addressed': [
            'zone_1_regime_transition_pain (needs asymmetric hysteresis — deferred)',
            'zone_2_hl_api_failure (needs circuit breaker — deferred)',
            'zone_3_cascade_chasing (needs anti-chasing filter — deferred)',
            'zone_4_news_shock (needs swan-BLACK pause — deferred)',
            'zone_6_thin_book_slip (needs liq-weighted routing — deferred to $20k+ scale)',
        ],
    }
