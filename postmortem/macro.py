"""Macro snapshot.

Free sources:
  Yahoo Finance v8 chart endpoint — DXY, SPX, VIX, gold, TLT
  CoinGecko public API (30 req/min) — BTC dominance, total market cap

Cached 60s. Parallel fetch.
"""
import os
import time
import json
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_CACHE = None
_CACHE_TS = 0.0
_LOCK = threading.Lock()
TTL = int(os.environ.get('POSTMORTEM_MACRO_TTL', '60'))
TIMEOUT = 6
USER_AGENT = 'Mozilla/5.0 (precog-postmortem)'

# Yahoo tickers we track
YAHOO_TICKERS = {
    'DXY':   '%5EDXY',      # Dollar Index (Yahoo requires ^DXY url-encoded)
    'SPX':   '%5EGSPC',     # S&P 500
    'VIX':   '%5EVIX',      # Volatility index
    'GOLD':  'GC%3DF',      # Gold futures
    'TLT':   'TLT',         # 20yr bonds ETF (rate proxy)
    'OIL':   'CL%3DF',      # Crude oil
}


def _http(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_yahoo(label, sym):
    """Return {price, chg_24h_pct, chg_7d_pct} or None."""
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1h&range=7d'
        raw = _http(url)
        d = json.loads(raw.decode('utf-8', errors='ignore'))
        result = (d.get('chart') or {}).get('result') or []
        if not result: return None
        r0 = result[0]
        meta = r0.get('meta', {})
        price = meta.get('regularMarketPrice') or meta.get('previousClose')
        closes = (r0.get('indicators', {}).get('quote') or [{}])[0].get('close') or []
        closes = [c for c in closes if c is not None]
        if not closes or price is None: return {'price': price}
        # 24h ≈ last 24 bars (1h interval); 7d = full range
        c_start = closes[0]
        c_24h_ago = closes[-25] if len(closes) >= 25 else closes[0]
        chg_24h = ((price - c_24h_ago) / c_24h_ago * 100) if c_24h_ago else 0.0
        chg_7d = ((price - c_start) / c_start * 100) if c_start else 0.0
        return {
            'price': round(price, 4),
            'chg_24h_pct': round(chg_24h, 2),
            'chg_7d_pct': round(chg_7d, 2),
        }
    except Exception:
        return None


def _fetch_coingecko_global():
    """BTC.D and total crypto mkt cap."""
    try:
        raw = _http('https://api.coingecko.com/api/v3/global', timeout=8)
        d = json.loads(raw.decode('utf-8', errors='ignore'))
        data = d.get('data', {})
        btc_d = (data.get('market_cap_percentage') or {}).get('btc')
        eth_d = (data.get('market_cap_percentage') or {}).get('eth')
        total = data.get('total_market_cap', {}).get('usd')
        chg_24h = data.get('market_cap_change_percentage_24h_usd')
        return {
            'btc_dominance': round(btc_d, 2) if btc_d else None,
            'eth_dominance': round(eth_d, 2) if eth_d else None,
            'total_mcap_usd': int(total) if total else None,
            'mcap_chg_24h_pct': round(chg_24h, 2) if chg_24h else None,
        }
    except Exception:
        return None


def _fetch_binance_funding_btc():
    """BTC perp funding on Binance — cross-venue sanity check vs HL."""
    try:
        raw = _http('https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT', timeout=6)
        d = json.loads(raw.decode('utf-8', errors='ignore'))
        return {
            'binance_btc_funding_bps': round(float(d.get('lastFundingRate', 0)) * 10000, 2),
            'binance_btc_mark': float(d.get('markPrice', 0)),
        }
    except Exception:
        return None


def fetch_all(force=False):
    global _CACHE, _CACHE_TS
    now = time.time()
    with _LOCK:
        if not force and _CACHE is not None and (now - _CACHE_TS) < TTL:
            return _CACHE

    snap = {'ts': now}
    with ThreadPoolExecutor(max_workers=6) as ex:
        yf = {label: ex.submit(_fetch_yahoo, label, sym) for label, sym in YAHOO_TICKERS.items()}
        cg = ex.submit(_fetch_coingecko_global)
        bn = ex.submit(_fetch_binance_funding_btc)
        for label, fut in yf.items():
            try: snap[label.lower()] = fut.result(timeout=TIMEOUT + 4)
            except Exception: snap[label.lower()] = None
        try: snap['crypto_global'] = cg.result(timeout=TIMEOUT + 4)
        except Exception: snap['crypto_global'] = None
        try: snap['binance_btc'] = bn.result(timeout=TIMEOUT + 4)
        except Exception: snap['binance_btc'] = None

    with _LOCK:
        _CACHE = snap
        _CACHE_TS = now
    return snap


def format_for_prompt(snap=None):
    if snap is None: snap = fetch_all()
    lines = []
    def v(d, k):
        if d is None: return '—'
        x = d.get(k)
        return f'{x}' if x is not None else '—'

    for lbl in ('spx', 'vix', 'dxy', 'gold', 'oil', 'tlt'):
        d = snap.get(lbl)
        if d:
            lines.append(f'  {lbl.upper():5} {v(d,"price"):>10} | 24h {v(d,"chg_24h_pct"):>6}% | 7d {v(d,"chg_7d_pct"):>6}%')
    cg = snap.get('crypto_global')
    if cg:
        lines.append(f'  BTC.D {v(cg,"btc_dominance"):>10}% | ETH.D {v(cg,"eth_dominance"):>6}% | mcap_24h {v(cg,"mcap_chg_24h_pct"):>6}%')
    bn = snap.get('binance_btc')
    if bn:
        lines.append(f'  BINANCE BTC funding {v(bn,"binance_btc_funding_bps")}bps | mark {v(bn,"binance_btc_mark")}')
    return '\n'.join(lines) if lines else '(macro unavailable)'


def snapshot_health():
    snap = _CACHE or {}
    return {
        'cache_age_sec': int(time.time() - _CACHE_TS) if _CACHE_TS else None,
        'fetched_keys': [k for k, v in snap.items() if v is not None and k != 'ts'],
    }
