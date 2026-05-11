"""Council-mandated unit tests. Each lever has at least one assertion."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from router import route


def W(side, price, usd=1_000_000, persistence_sec=300):
    return {'side': side, 'price': price, 'usd': usd, 'persistence_sec': persistence_sec}


def assert_eq(actual, expected, msg=''):
    if actual != expected:
        print(f'  FAIL {msg}: got {actual!r}, expected {expected!r}')
        return False
    print(f'  pass {msg}')
    return True


def test_no_walls_allows():
    """Empty wall context → ALLOW."""
    d = route('BTC', 'BUY', 100.0, walls=[])
    return assert_eq(d['action'], 'ALLOW', 'no walls → ALLOW')


def test_side_aware_only_same_side_blocks():
    """Council lever 1: BUY trade. Bid-wall below (support) alone should NOT block.
       Even close, even entrenched. Bid below is on our side. Old btc_macro blocked both."""
    walls = [W('bid', 99.7, usd=5_000_000, persistence_sec=900)]  # entrenched bid below
    d = route('BTC', 'BUY', 100.0, walls=walls,
              recent_px_trajectory=[(0, 100.05), (60, 100.0)])  # retreating from above
    # Bid below is opposite-side for BUY → SL anchor candidate, NOT a blocker.
    # Within sl_anchor_proximity_pct=0.01, triggers MODIFY (SL tightening). Not BLOCK.
    if d['action'] == 'BLOCK':
        print(f"  FAIL: blocked by opposite-side wall (action={d['action']} reason={d['reason']})")
        return False
    print(f"  pass: opposite-side wall did NOT block (action={d['action']} reason={d['reason']})")
    return True


def test_side_aware_sell_not_blocked_by_ask_above():
    """SELL trade with ask above (support for our direction!) and no bid below → ALLOW.
       Old behavior would have blocked symmetrically."""
    walls = [W('ask', 100.3, persistence_sec=900)]  # ask above = supports our SELL
    d = route('BTC', 'SELL', 100.0, walls=walls)
    # The ask above is on the SL-anchor side for SELL → would tighten SL only.
    # Within sl_anchor_proximity_pct=0.01, this triggers MODIFY (SL anchor).
    return assert_eq(d['action'], 'MODIFY', 'SELL w/ ask above only → MODIFY (SL anchor)')


def test_approach_aware_no_block_if_retreating():
    """Council lever 2: same wall, same proximity, but price moving AWAY from wall.
       Should NOT block (we're already past or moving away)."""
    walls = [W('ask', 100.2, persistence_sec=900)]
    # BUY trade. Price was 100.5 a minute ago, now 100.0 → moving DOWN, away from ask
    d = route('BTC', 'BUY', 100.0, walls=walls,
              recent_px_trajectory=[(0, 100.5), (60, 100.0)],
              require_approach_to_block=True)
    # Wall 0.2% away (within block), entrenched (900s ≥600), but not approaching.
    # Expected: NOT 'BLOCK'. Falls through to soft warn or pass-through.
    if d['action'] == 'BLOCK':
        print(f"  FAIL approach-aware: blocked despite retreating ({d['reason']})")
        return False
    print(f"  pass approach-aware: action={d['action']} reason={d['reason']}")
    return True


def test_persistence_aware_transient_ignored():
    """Council lever 3: 30s-old wall (below min_persistence_sec=120) should be IGNORED."""
    walls = [W('ask', 100.1, persistence_sec=30, usd=10_000_000)]  # huge but transient
    d = route('BTC', 'BUY', 100.0, walls=walls,
              recent_px_trajectory=[(0, 99.95), (60, 100.0)])
    return assert_eq(d['action'], 'ALLOW', 'transient wall (30s) → ALLOW')


def test_size_modulating_sandwich():
    """Council lever 4: walls both sides + price NOT approaching either → MODIFY w/ size_mult<1.

    Note: if price IS approaching the same-side wall AND it's entrenched, the
    correct behavior is BLOCK (don't enter into approaching resistance). Sandwich
    routing only applies when no clear directional pressure exists."""
    walls = [
        W('ask', 100.2, usd=5_000_000, persistence_sec=900),
        W('bid',  99.8, usd=8_000_000, persistence_sec=900),
    ]
    # Price flat / oscillating — no approach signal in either direction
    d = route('BTC', 'BUY', 100.0, walls=walls,
              recent_px_trajectory=[(0, 100.0), (60, 100.0)])  # flat
    ok1 = assert_eq(d['action'], 'MODIFY', 'sandwich (non-approaching) → MODIFY')
    ok2 = d.get('size_mult', 1.0) < 1.0
    print(f"  {'pass' if ok2 else 'FAIL'} size_mult={d.get('size_mult')} <1.0 in sandwich")
    return ok1 and ok2


def test_entrenched_blocks_sandwich():
    """When in a sandwich BUT approaching entrenched same-side wall, BLOCK still applies.
       Sandwich routing is for the un-pressured case; entrenched-and-approaching wins."""
    walls = [
        W('ask', 100.2, usd=5_000_000, persistence_sec=900),
        W('bid',  99.8, usd=8_000_000, persistence_sec=900),
    ]
    # Price climbing toward ask
    d = route('BTC', 'BUY', 100.0, walls=walls,
              recent_px_trajectory=[(0, 99.9), (60, 100.0)])
    return assert_eq(d['action'], 'BLOCK', 'sandwich + approaching entrenched ask → BLOCK')


def test_far_wall_becomes_tp_target():
    """Same-side wall outside block range but inside target range → MODIFY w/ TP at wall."""
    walls = [W('ask', 101.5, usd=3_000_000, persistence_sec=900)]  # 1.5% above
    d = route('BTC', 'BUY', 100.0, walls=walls)
    ok1 = assert_eq(d['action'], 'MODIFY', 'far same-side wall → MODIFY tp')
    ok2 = d.get('suggested_tp_px') is not None
    print(f"  {'pass' if ok2 else 'FAIL'} suggested_tp_px set ({d.get('suggested_tp_px')})")
    return ok1 and ok2


def test_min_usd_filter():
    """Walls below min_wall_usd should be IGNORED."""
    walls = [W('ask', 100.1, usd=100_000, persistence_sec=900)]  # 100k below 500k threshold
    d = route('BTC', 'BUY', 100.0, walls=walls)
    return assert_eq(d['action'], 'ALLOW', 'small wall ($100k) → ALLOW')


tests = [
    test_no_walls_allows,
    test_side_aware_only_same_side_blocks,
    test_entrenched_blocks_sandwich,
    test_side_aware_sell_not_blocked_by_ask_above,
    test_approach_aware_no_block_if_retreating,
    test_persistence_aware_transient_ignored,
    test_size_modulating_sandwich,
    test_far_wall_becomes_tp_target,
    test_min_usd_filter,
]
results = []
for t in tests:
    print(f'\n=== {t.__name__} ===')
    try:
        results.append(t())
    except Exception as e:
        print(f'  EXCEPTION: {type(e).__name__}: {e}')
        results.append(False)

passed = sum(results)
total = len(results)
print(f'\n{"="*40}')
print(f'  {passed}/{total} tests passed')
sys.exit(0 if passed == total else 1)
