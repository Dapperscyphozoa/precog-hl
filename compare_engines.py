#!/usr/bin/env python3
"""
compare_engines.py — Stack V9 + V10 lifetime stats side-by-side.

V9 (srv-d7u59hl0lvsc73enssrg, branch pole-engine, ENGINE_PREFIX=V9): wall-bounce engine, live trading.
V10 (srv-d7uirbv7f7vs73cmrai0, branch v10-framework): SMC framework + walls, paper trading.

Pulls each service's most recent log slice via Render API, parses lifetime
counters from the latest tick line of each, prints the comparison.

V10 also splits by path (4h vs 1h) and wall (present/absent) when available.
"""
import json, sys, urllib.request, re

RENDER_TOKEN = "rnd_GbOYfugIiAl0ihJR2O2wOjYNpWUz"
OWNER_ID = "tea-d6ufmnea2pns739be9gg"
HL_API = "https://api.hyperliquid.xyz/info"
HL_WALLET = "0x3eDaD0649Db466E6E7B9a0Caa3E5d6ddc71B5ffE"


def hl_post(body):
    req = urllib.request.Request(HL_API, data=json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def v9_actual_pnl():
    """V9 actual realized PnL — currently NOT recoverable via cloid prefix.

    HL hashes client-supplied cloid into a 16-byte hex hash on-chain. The original
    `V9{coin}...` string V9 sends is keccak-hashed before storage in user fills.
    To get V9's true closed-trade PnL, options are:
      1. SSH into V9 service and read /var/data/pole_state_v9.json
      2. Add a /stats HTTP endpoint to V9 (small code change to V9)
      3. Add CLOSE-event logging in V9's reconcile() so log parsing captures PnL
    """
    return {"trades": 0, "note": "V9 PnL not recoverable via HL cloid (HL hashes cloids); see compare_engines.py source for options"}


ENGINES = [
    {"name": "V9 (wall bounce, live)",  "id": "srv-d7u59hl0lvsc73enssrg"},
    {"name": "V10 (framework, paper)", "id": "srv-d7uirbv7f7vs73cmrai0"},
]


def fetch_logs(svc_id, n=300):
    url = (f"https://api.render.com/v1/logs?ownerId={OWNER_ID}"
           f"&resource={svc_id}&limit={n}&direction=backward")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {RENDER_TOKEN}"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def latest_match(logs, regex):
    rx = re.compile(regex)
    for l in reversed(logs):
        m = rx.search(l.get('message', ''))
        if m:
            return m, l['timestamp'][11:19]
    return None, None


def parse_engine(svc_id):
    raw = fetch_logs(svc_id, 300)
    logs = sorted(raw.get('logs', []), key=lambda l: l.get('timestamp', ''))
    if not logs:
        return None

    out = {'last_log_t': logs[-1]['timestamp'][11:19] if logs else '-'}

    # Tick number / balance — both engines log "Bal:$X.XX Pos:N Pend:M"
    m, ts = latest_match(logs, r'Bal:\$([\d.]+)\s+Pos:(\d+)\s+Pend:(\d+)')
    if m:
        out['bal'] = float(m.group(1))
        out['pos'] = int(m.group(2))
        out['pend'] = int(m.group(3))
        out['snapshot_t'] = ts

    # V9 tick line: "Bal:$X Pos:N Pend:M Trig:T | BO_armed:A BO_fired:F Bounce:B Spoof:S FundKill:K Foreign:Fn"
    m, _ = latest_match(logs,
        r'Bal:\$([\d.]+)\s+Pos:(\d+)\s+Pend:(\d+)\s+Trig:(\d+)\s+\|\s+BO_armed:(\d+)\s+BO_fired:(\d+)\s+Bounce:(\d+)\s+Spoof:(\d+)\s+FundKill:(\d+)\s+Foreign:(\d+)')
    if m:
        out['bal'] = float(m.group(1))
        out['pos'] = int(m.group(2))
        out['pend'] = int(m.group(3))
        out['v9_trig'] = int(m.group(4))
        out['v9_bo_armed'] = int(m.group(5))
        out['v9_bo_fired'] = int(m.group(6))
        out['v9_bounce'] = int(m.group(7))
        out['v9_spoof'] = int(m.group(8))
        out['v9_fundkill'] = int(m.group(9))
        out['v9_foreign'] = int(m.group(10))
        out['fires_total'] = out['v9_bo_fired'] + out['v9_bounce']

    # V10 tick line: "Total fires:N (4h:N 1h:N) Qualified:N Wall+:N Wall-:N"
    m, _ = latest_match(logs,
        r'Total fires:(\d+)(?:\s+\(4h:(\d+)\s+1h:(\d+)\))?\s+Qualified:(\d+)\s+(?:WallBlocked|Wall\+):(\d+)\s*(?:Wall-:(\d+))?')
    if m:
        out['fires_total'] = int(m.group(1))
        if m.group(2) is not None: out['fires_4h'] = int(m.group(2))
        if m.group(3) is not None: out['fires_1h'] = int(m.group(3))
        out['qualified'] = int(m.group(4))
        out['wall_present_or_blocked'] = int(m.group(5))
        if m.group(6) is not None: out['wall_absent'] = int(m.group(6))

    # V10 tracker: closed=N (W:A+B+C/L:D/EXP:E/EOW:F) WR=X% PnL%=Y
    m, _ = latest_match(logs,
        r'Tracker:\s+closed=(\d+)\s+\(W:(\d+)\+(\d+)(?:\+(\d+))?/L:(\d+)/EXP:(\d+)/EOW:(\d+)\)\s+WR=(\d+)%\s+PnL%=([+-]?\d+\.\d+)')
    if m:
        out['tracker'] = {
            'closed': int(m.group(1)),
            'tp1_tp2': int(m.group(2)),
            'tp1_be':  int(m.group(3)),
            'tp1_betimeout': int(m.group(4)) if m.group(4) else 0,
            'losses_sl': int(m.group(5)),
            'expired':  int(m.group(6)),
            'eow':      int(m.group(7)),
            'wr_pct':   int(m.group(8)),
            'pnl_pct':  float(m.group(9)),
        }

    # V9 lifetime line: "Lifetime: trades=N WR=X% pf=Y net=+Z%"
    m, _ = latest_match(logs, r'Lifetime:\s+trades=(\d+)\s+WR=(\d+)%\s+pf=([\d.]+)\s+net=([+-]?[\d.]+%?)')
    if m:
        out['v9_lifetime'] = {
            'trades': int(m.group(1)),
            'wr_pct': int(m.group(2)),
            'pf':     float(m.group(3)),
            'net':    m.group(4),
        }

    # V9 alt naming: "Total: trades=N wins=W losses=L WR=X%"
    m, _ = latest_match(logs, r'Total:\s+trades=(\d+)\s+wins=(\d+)\s+losses=(\d+)\s+WR=(\d+)%')
    if m and 'v9_lifetime' not in out:
        out['v9_lifetime'] = {
            'trades': int(m.group(1)),
            'wins':   int(m.group(2)),
            'losses': int(m.group(3)),
            'wr_pct': int(m.group(4)),
        }

    return out


def main():
    print(f"{'METRIC':30s}  {'V9 (wall bounce, LIVE)':>26s}  {'V10 (framework, PAPER)':>26s}")
    print("-" * 90)

    results = {}
    for e in ENGINES:
        r = parse_engine(e['id'])
        results[e['name']] = r or {}

    v9 = results.get("V9 (wall bounce, live)", {})
    v10 = results.get("V10 (framework, paper)", {})

    def row(label, v9_val, v10_val):
        print(f"{label:30s}  {str(v9_val):>26s}  {str(v10_val):>26s}")

    row("Last log",     v9.get('last_log_t','-'), v10.get('last_log_t','-'))
    row("Balance ($)",  f"{v9.get('bal',0):.2f}", f"{v10.get('bal',0):.2f}")
    row("Open pos",     v9.get('pos','-'), v10.get('pos','-'))
    row("Pending",      v9.get('pend','-'), v10.get('pend','-'))
    print()
    row("Lifetime fires (total)", v9.get('fires_total','-'), v10.get('fires_total','-'))
    row("  V9 BO_fired (breakouts)", v9.get('v9_bo_fired','-'), '-')
    row("  V9 Bounce (wall bounces)", v9.get('v9_bounce','-'), '-')
    row("  V9 BO_armed (waiting)", v9.get('v9_bo_armed','-'), '-')
    row("  V9 Trig (active triggers)", v9.get('v9_trig','-'), '-')
    row("  V9 Spoof / FundKill", f"{v9.get('v9_spoof','-')} / {v9.get('v9_fundkill','-')}", '-')
    row("  V10 Path A (4h)", '-', v10.get('fires_4h','-'))
    row("  V10 Path B (1h)", '-', v10.get('fires_1h','-'))
    row("Qualified setups (V10)", '-', v10.get('qualified','-'))
    row("Wall present (V10)",  '-', v10.get('wall_present_or_blocked','-'))
    row("Wall absent (V10)",   '-', v10.get('wall_absent','-'))
    row("Foreign positions (V9)", v9.get('v9_foreign','-'), '-')

    # V9 lifetime parse
    v9l = v9.get('v9_lifetime', {})
    if v9l:
        print()
        row("V9 lifetime trades", v9l.get('trades','-'), '-')
        row("V9 lifetime WR%",    v9l.get('wr_pct','-'), '-')
        if 'pf' in v9l: row("V9 lifetime PF", v9l['pf'], '-')
        if 'net' in v9l: row("V9 lifetime net", v9l['net'], '-')

    # V10 paper tracker
    t = v10.get('tracker', {})
    if t:
        print()
        row("V10 paper closed", '-', t['closed'])
        row("V10 wins (TP2/BE/BEtimeout)", '-', f"{t['tp1_tp2']}/{t['tp1_be']}/{t['tp1_betimeout']}")
        row("V10 losses (SL)", '-', t['losses_sl'])
        row("V10 expired/EOW", '-', f"{t['expired']}/{t['eow']}")
        row("V10 WR%", '-', t['wr_pct'])
        row("V10 PnL%", '-', f"{t['pnl_pct']:+.2f}")

    # V9 ACTUAL realized PnL from HL exchange (V9-prefixed cloids only, last 7d)
    print("\n=== V9 actual realized PnL (HL exchange, V9-prefix cloids, last 7d) ===")
    pnl = v9_actual_pnl()
    if 'error' in pnl:
        print(f"  err: {pnl['error']}")
    elif pnl.get('trades', 0) == 0:
        print(f"  {pnl.get('note', 'no closed trades')}")
    else:
        print(f"  Closed trades: {pnl['trades']}")
        print(f"  Wins / Losses: {pnl['wins']} / {pnl['losses']}")
        print(f"  WR%:           {pnl['wr_pct']}")
        print(f"  Net PnL:       ${pnl['net_pnl_usd']:+.2f}")
        print(f"  Avg win:       ${pnl['avg_win_usd']:+.2f}")
        print(f"  Avg loss:      ${pnl['avg_loss_usd']:+.2f}")


if __name__ == '__main__':
    main()
