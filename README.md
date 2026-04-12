# PreCog Live Deployment

## Files added (precog-hl branch)
- `precog.py` - main engine (192 lines)
- `requirements.txt` - hyperliquid-python-sdk + eth-account
- `render-precog.yaml` - Render service spec

## Render Setup (Standard tier)
1. Render dashboard → New → Background Worker
2. Connect repo: `Dapperscyphozoa/cyber-psycho`, branch: `precog-hl`
3. Runtime: Python 3
4. Build: `pip install -r requirements.txt`
5. Start: `python3 precog.py`
6. Plan: Standard
7. Persistent disk: 1GB at `/var/data`
8. Env vars (already exist on CP service, copy them):
   - HYPERLIQUID_ACCOUNT
   - HL_PRIVATE_KEY
9. Deploy

## Strategy Locked
- Structural-gate v6.3 Lite
- 19 coins, 15m timeframe
- 15x leverage, 15% current equity per leg
- Max 5 legs per cluster
- Funding-aware exit at 30% PnL drag
- 80.2% WR / max streak 5 (60d HL backtest)

## Live monitoring
- Render service logs = full activity stream
- HL exchange UI = real-time position view
- State file: `/var/data/precog_state.json`

## Kill switch
Stop Render service from dashboard. Open positions remain on HL — close manually if desired.

## Old CP crypto trading
CP node service still runs (handles MT4). To stop CP from also trading crypto, comment out `setInterval(hlTick, 30*60*1000)` in `index.js` line ~131. PreCog runs independently.
