"""
okx_fetch.py — Drop-in replacement for HL candle REST fetches.

Why OKX: HL's CloudFront aggressively rate-limits shared-IP cloud hosts
(documented 429 cascades from Render). Binance and Bybit either geo-block
US IPs or block CloudFront-fronted regions. OKX's public market data API
is global, requires no auth, and has no IP-range restrictions.

Price parity: BTC/ETH/SOL on OKX vs HL trade within ~1bp (arb keeps them
tight). Well below our 0.3% SL buffer. Long-tail alts may be slightly
wider but still within tolerance for 15m strategy.

Public API (drop-in compatible with precog.py's _snap_fetch):
    fetch_klines(hl_coin, interval, n_bars) -> list[(t_ms, o, h, l, c, v)]

OKX endpoint:
    GET https://www.okx.com/api/v5/market/candles
    params: instId=BTC-USDT-SWAP, bar=15m, limit=300
    response.data: [[ts_ms, o, h, l, c, vol, volCcy, volCcyQuote, confirm], ...]
    NOTE: response is newest-first; we reverse before returning.
"""

import json
import time
import urllib.request
import urllib.parse

OKX_REST = "https://www.okx.com/api/v5/market/candles"
OKX_HISTORY_REST = "https://www.okx.com/api/v5/market/history-candles"

# HL coin name → OKX instrument ID (perpetual swap, USDT-margined).
# Most coins follow the pattern <COIN>-USDT-SWAP. Special cases below.
#
# k-prefix (HL's 1000x convention) maps to OKX's raw symbol — OKX uses
# `ctVal` to encode the multiplier in the instrument metadata, but the
# candle CLOSE price is the raw spot-equivalent. For our pattern-based
# strategy (RSI / pivots / ATR ratios) the multiplier is irrelevant — only
# the price RATIOS matter, and those are identical regardless of multiplier.
HL_TO_OKX = {
    # k-prefix HL coins → raw OKX symbol
    'kPEPE':   'PEPE-USDT-SWAP',
    'kSHIB':   'SHIB-USDT-SWAP',
    'kBONK':   'BONK-USDT-SWAP',
    'kFLOKI':  'FLOKI-USDT-SWAP',
    'kDOGS':   'DOGS-USDT-SWAP',
    'kCAT':    'CAT-USDT-SWAP',
    # Renames / rebrands
    'MATIC':   'POL-USDT-SWAP',     # Polygon rebrand (2024)
    'FTM':     'S-USDT-SWAP',       # Sonic rebrand
    'RNDR':    'RENDER-USDT-SWAP',  # Render token rebrand
}

# Coins not listed on OKX perps. Return [] for these = no signal generated.
# Better than 429-spamming HL endlessly trying.
NOT_ON_OKX = {
    'PURR',     # HL native
    'kLUNC',    # delisted
    'XMR',      # privacy coin, removed from many exchanges
    'MKR',      # not on OKX perps
    'RUNE',     # not on OKX perps
    'VET',      # not on OKX perps
    'KAS',      # not on OKX perps
    'BAL',      # not on OKX perps
    'EOS',      # not on OKX perps
    'VVV',      # not listed
}

# HL timeframe → OKX timeframe. OKX uses lowercase m for minutes,
# uppercase H for hours, D/W/M for days/weeks/months.
_TF_MAP = {
    '1m':  '1m',
    '3m':  '3m',
    '5m':  '5m',
    '15m': '15m',
    '30m': '30m',
    '1h':  '1H',
    '2h':  '2H',
    '4h':  '4H',
    '1d':  '1D',
}


def hl_to_okx_inst(hl_coin):
    """Map HL coin → OKX instrument ID, or None if not available."""
    if hl_coin in NOT_ON_OKX:
        return None
    if hl_coin in HL_TO_OKX:
        return HL_TO_OKX[hl_coin]
    return f"{hl_coin}-USDT-SWAP"


def fetch_klines(hl_coin, interval, n_bars):
    """Drop-in replacement for HL's info.candles_snapshot().

    Returns list of dicts in HL's exact shape:
        [{'t': open_ms, 'o': open, 'h': high, 'l': low,
          'c': close, 'v': volume}, ...]

    Chronologically ordered (oldest first), matching HL's behavior.

    Returns [] on:
        - Coin not on OKX (HL-native, delisted)
        - Network error
        - Empty/malformed response
    """
    inst = hl_to_okx_inst(hl_coin)
    if inst is None:
        return []

    okx_bar = _TF_MAP.get(interval, interval)

    # OKX limit cap is 300 for /candles, 100 for /history-candles.
    limit = max(1, min(int(n_bars), 300))

    params = urllib.parse.urlencode({
        'instId': inst,
        'bar':    okx_bar,
        'limit':  limit,
    })
    url = f"{OKX_REST}?{params}"

    try:
        req = urllib.request.Request(url, headers={
            'Accept':     'application/json',
            'User-Agent': 'precog-hl/1.0',
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            payload = json.loads(r.read())
    except Exception:
        return []

    if not isinstance(payload, dict) or payload.get('code') != '0':
        return []
    rows = payload.get('data') or []
    if not isinstance(rows, list):
        return []

    out = []
    for k in rows:
        try:
            out.append({
                't': int(k[0]),
                'o': float(k[1]),
                'h': float(k[2]),
                'l': float(k[3]),
                'c': float(k[4]),
                'v': float(k[5]),
            })
        except (IndexError, TypeError, ValueError):
            continue

    # OKX returns newest-first; reverse to chronological (matches HL).
    out.reverse()
    return out


# Lightweight throttle — OKX public limit is 20 req / 2s per IP.
# Our snapshot build hits ~75 coins so we'd burst at 75 req / few seconds.
# 0.05s gap = 20 req/s = right at the limit. Use 0.1s to be safe.
_LAST_CALL = [0.0]
_MIN_GAP_SEC = 0.1


def throttle():
    """Per-call gap to stay under OKX public rate limit.
    Drop-in compatible with precog.py's _hl_throttle signature."""
    now = time.time()
    gap = now - _LAST_CALL[0]
    if gap < _MIN_GAP_SEC:
        time.sleep(_MIN_GAP_SEC - gap)
    _LAST_CALL[0] = time.time()


# Smoke test
if __name__ == '__main__':
    import sys
    coin = sys.argv[1] if len(sys.argv) > 1 else 'BTC'
    tf   = sys.argv[2] if len(sys.argv) > 2 else '15m'
    nb   = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    print(f"Fetching {nb} {tf} bars for {coin} (OKX inst: {hl_to_okx_inst(coin)})…")
    bars = fetch_klines(coin, tf, nb)
    print(f"Got {len(bars)} bars")
    for b in bars[-3:]:
        ts = time.strftime('%Y-%m-%d %H:%M', time.gmtime(b['t']/1000))
        print(f"  {ts}  o={b['o']:>10}  h={b['h']:>10}  l={b['l']:>10}  c={b['c']:>10}  v={b['v']:.1f}")
