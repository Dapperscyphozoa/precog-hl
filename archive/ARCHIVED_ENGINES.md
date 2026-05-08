# Archived Engines

## LSR (Liquidity Sweep Reversal) — archived 2026-05-08
- Strategy: sweep + reject (liquidity sweep reversal)
- Status: never produced live signals on PRECOG infrastructure
- Listed in /all_systems with `live=False, error='no_signal'` indefinitely
- Removed from precog.py SYSTEMS dict + ORDER list
- Removed from landing.html in earlier commit (ce48d22)

## BRK (OB Break + Retest Continuation) — archived 2026-05-08
- Strategy: orderblock break + retest continuation
- Backtest claim: 60% WR / PF 2.13
- Status: never produced live signals on PRECOG infrastructure
- Listed in /all_systems with `live=False, error='no_signal'` indefinitely
- Removed from precog.py SYSTEMS dict + ORDER list
- Removed from landing.html in earlier commit (ce48d22)

To revive either engine: restore the dict entry and ORDER list slot in
the all_systems() route in precog.py, then plug in the engine
implementation that writes its dashboard state via dashboard_push.
