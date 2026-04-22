"""Macro snapshot — NO YAHOO.

Sources (all free, crypto-native or user-controlled):
  - CoinGecko       — BTC.D, ETH.D, total mcap, 24h change (30 req/min free)
  - Binance futures — BTC/ETH/SOL funding + mark (public, no key)
  - Bybit public    — BTC/ETH funding for cross-venue check
  - OKX public      — BTC/ETH funding
  - Deribit         — BTC/ETH options: DVOL index (crypto VIX-equivalent)
  - CoinGlass       — BTC/ETH long/short ratio (public endpoint)
  - TV webhook cache — DXY, SPX, VIX, GOLD, OIL pushed by user's TradingView
                       alerts. Zero reliance on Yahoo or scraping.

Cached 60s. Parallel fetch. All sources fail-independent.
"""
import os
import time
import json
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import tv_cache

_CACHE = None
_CACHE_TS = 0.0
_LOCK = threading.Lock()
TTL = int(os.environ.get('POSTMORTEM_MACRO_TTL', '60'))
TIMEOUT = 6
UA = 'Mozilla/5.0 (precog-postmortem)'


def _http(url, timeout=TIMEOUT, headers=None):
    h = {'User-Agent': UA, 'Accept': 'application/json'}
    if headers: h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ─────────────────────────────────────────────────────
# Crypto dominance + macro cap (CoinGecko)
# ─────────────────────────────────────────────────────
def _fetch_coingecko_global():
    try:
        raw = _http('https://api.coingecko.com/api/v3/global', timeout=8)
        d = json.loads(raw.decode('utf-8', errors='ignore'))
        data = d.get('data', {})
        mcp = data.get('market_cap_percentage') or {}
        return {
            'btc_dominance':    round(mcp.get('btc'), 2) if mcp.get('btc') else None,
            'eth_dominance':    round(mcp.get('eth'), 2) if mcp.get('eth') else None,
            'total_mcap_usd':   int(data.get('total_market_cap', {}).get('usd', 0)) or None,
            'mcap_chg_24h_pct': round(data.get('market_cap_change_percentage_24h_usd', 0), 2),
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────
# Cross-venue perp funding (Binance / Bybit / OKX)
# ─────────────────────────────────────────────────────
def _fetch_binance_funding(symbol='BTCUSDT'):
    try:
        raw = _http(f'https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}', timeout=6)
        d = json.loads(raw.decode('utf-8', errors='ignore'))
        return {
            'funding_bps': round(float(d.get('lastFundingRate', 0)) * 10000, 3),
            'mark': float(d.get('markPrice', 0)),
        }
    except Exception:
        return None


def _fetch_bybit_funding(symbol='BTCUSDT'):
    try:
        raw = _http(f'https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}', timeout=6)
        d = json.loads(raw.decode('utf-8', errors='ignore'))
        items = (d.get('result') or {}).get('list') or []
        if not items: return None
        t = items[0]
        return {
            'funding_bps': round(float(t.get('fundingRate', 0)) * 10000, 3),
            'mark': float(t.get('markPrice', 0)),
        }
    except Exception:
        return None


def _fetch_okx_funding(instId='BTC-USDT-SWAP'):
    try:
        raw = _http(f'https://www.okx.com/api/v5/public/funding-rate?instId={instId}', timeout=6)
        d = json.loads(raw.decode('utf-8', errors='ignore'))
        items = d.get('data') or []
        if not items: return None
        t = items[0]
        return {
            'funding_bps': round(float(t.get('fundingRate', 0)) * 10000, 3),
        }
    except Exception:
        return None


def _fetch_funding_triangle():
    """Cross-venue funding divergence for BTC and ETH."""
    out = {}
    for coin, bn, bb, ok in [
        ('BTC', 'BTCUSDT', 'BTCUSDT', 'BTC-USDT-SWAP'),
        ('ETH', 'ETHUSDT', 'ETHUSDT', 'ETH-USDT-SWAP'),
    ]:
        row = {}
        try: row['binance'] = _fetch_binance_funding(bn)
        except Exception: row['binance'] = None
        try: row['bybit']   = _fetch_bybit_funding(bb)
        except Exception: row['bybit']   = None
        try: row['okx']     = _fetch_okx_funding(ok)
        except Exception: row['okx']     = None
        fundings = [r['funding_bps'] for r in (row['binance'], row['bybit'], row['okx']) if r and 'funding_bps' in r]
        if len(fundings) >= 2:
            mn = min(fundings); mx = max(fundings)
            row['range_bps'] = round(mx - mn, 2)
            row['divergent'] = bool((mx - mn) > 5.0)
        out[coin] = row
    return out


# ─────────────────────────────────────────────────────
# Deribit options — BTC/ETH DVOL (crypto VIX)
# ─────────────────────────────────────────────────────
def _fetch_deribit_iv(currency='BTC'):
    try:
        raw = _http(f'https://www.deribit.com/api/v2/public/get_index_price?index_name={currency.lower()}_usd', timeout=6)
        spot = (json.loads(raw.decode())).get('result', {}).get('index_price')
        raw2 = _http(f'https://www.deribit.com/api/v2/public/get_volatility_index_data?currency={currency}&start_timestamp={int((time.time()-7200)*1000)}&end_timestamp={int(time.time()*1000)}&resolution=3600', timeout=6)
        d2 = json.loads(raw2.decode())
        dvol = None
        try:
            data_pts = (d2.get('result') or {}).get('data') or []
            if data_pts: dvol = round(float(data_pts[-1][4]), 2)
        except Exception:
            pass
        return {'spot': spot, 'dvol': dvol}
    except Exception:
        return None


# ─────────────────────────────────────────────────────
# CoinGlass L/S ratio (public)
# ─────────────────────────────────────────────────────
def _fetch_coinglass_ls(symbol='BTC'):
    try:
        url = f'https://open-api-v4.coinglass.com/api/futures/global-long-short-account-ratio/history?exchanges=Binance&symbol={symbol}&interval=h1&limit=2'
        raw = _http(url, timeout=6, headers={'accept': 'application/json'})
        d = json.loads(raw.decode())
        items = (d.get('data') or [])
        if not items: return None
        latest = items[-1]
        return {
            'global_long_pct':  round(float(latest.get('longAccount', 0)) * 100, 2),
            'global_short_pct': round(float(latest.get('shortAccount', 0)) * 100, 2),
            'ls_ratio':         round(float(latest.get('longShortRatio', 0)), 3),
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────
# TV webhook cache — DXY / SPX / VIX / GOLD / OIL / US10Y
# User sets up TradingView alerts that POST /postmortem/tv/macro
# ─────────────────────────────────────────────────────
def _fetch_tv_symbols():
    out = {}
    for sym in ('DXY', 'SPX', 'VIX', 'GOLD', 'OIL', 'US10Y', 'TNX', 'BTC_D'):
        v = tv_cache.read(sym)
        if v: out[sym.lower()] = v
    return out


# ─────────────────────────────────────────────────────
# Main aggregator
# ─────────────────────────────────────────────────────
def fetch_all(force=False):
    global _CACHE, _CACHE_TS
    now = time.time()
    with _LOCK:
        if not force and _CACHE is not None and (now - _CACHE_TS) < TTL:
            return _CACHE

    snap = {'ts': now, 'sources_used': []}
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut_cg   = ex.submit(_fetch_coingecko_global)
        fut_fund = ex.submit(_fetch_funding_triangle)
        fut_btc_iv = ex.submit(_fetch_deribit_iv, 'BTC')
        fut_eth_iv = ex.submit(_fetch_deribit_iv, 'ETH')
        fut_ls   = ex.submit(_fetch_coinglass_ls, 'BTC')
        fut_tv   = ex.submit(_fetch_tv_symbols)

        try: snap['crypto_global'] = fut_cg.result(timeout=TIMEOUT+4)
        except Exception: snap['crypto_global'] = None
        try: snap['funding_cross_venue'] = fut_fund.result(timeout=TIMEOUT+4)
        except Exception: snap['funding_cross_venue'] = None
        try: snap['btc_options'] = fut_btc_iv.result(timeout=TIMEOUT+4)
        except Exception: snap['btc_options'] = None
        try: snap['eth_options'] = fut_eth_iv.result(timeout=TIMEOUT+4)
        except Exception: snap['eth_options'] = None
        try: snap['btc_longshort'] = fut_ls.result(timeout=TIMEOUT+4)
        except Exception: snap['btc_longshort'] = None
        try: snap['tv_macro'] = fut_tv.result(timeout=TIMEOUT+4)
        except Exception: snap['tv_macro'] = None

    for k, v in list(snap.items()):
        if v and k not in ('ts', 'sources_used'):
            snap['sources_used'].append(k)

    with _LOCK:
        _CACHE = snap
        _CACHE_TS = now
    return snap


def format_for_prompt(snap=None):
    if snap is None: snap = fetch_all()
    if not snap: return '(macro unavailable)'
    lines = []

    cg = snap.get('crypto_global')
    if cg:
        def fmt(v, suf=''):
            return f'{v}{suf}' if v is not None else '—'
        lines.append(f'  BTC.D {fmt(cg.get("btc_dominance"),"%")}  ETH.D {fmt(cg.get("eth_dominance"),"%")}  '
                     f'total mcap 24h Δ {fmt(cg.get("mcap_chg_24h_pct"),"%")}')

    fund = snap.get('funding_cross_venue') or {}
    for coin in ('BTC', 'ETH'):
        row = fund.get(coin)
        if not row: continue
        parts = [f'  {coin} funding:']
        for venue in ('binance', 'bybit', 'okx'):
            r = row.get(venue)
            if r and 'funding_bps' in r:
                parts.append(f'{venue}={r["funding_bps"]:+.2f}bps')
        if row.get('divergent'):
            parts.append(f'DIVERGENT range={row.get("range_bps")}bps')
        lines.append(' '.join(parts))

    btc_o = snap.get('btc_options')
    eth_o = snap.get('eth_options')
    opt_parts = []
    if btc_o and btc_o.get('dvol') is not None:
        opt_parts.append(f'BTC DVOL {btc_o["dvol"]}')
    if eth_o and eth_o.get('dvol') is not None:
        opt_parts.append(f'ETH DVOL {eth_o["dvol"]}')
    if opt_parts:
        lines.append('  Options: ' + '  '.join(opt_parts))

    ls = snap.get('btc_longshort')
    if ls:
        lines.append(f'  BTC L/S  long {ls.get("global_long_pct","—")}% short {ls.get("global_short_pct","—")}% (ratio {ls.get("ls_ratio","—")})')

    tv = snap.get('tv_macro') or {}
    if tv:
        tv_parts = []
        for sym in ('spx', 'vix', 'dxy', 'gold', 'oil', 'us10y', 'tnx', 'btc_d'):
            d = tv.get(sym)
            if not d: continue
            px = d.get('price')
            chg = d.get('chg_pct_session')
            age_min = int((time.time() - d.get('ts', 0)) / 60) if d.get('ts') else None
            if age_min is not None and age_min > 120:
                continue
            seg = f'{sym.upper()} {px}'
            if chg is not None:
                seg += f' ({chg:+.2f}%)'
            if age_min is not None:
                seg += f' [{age_min}m old]'
            tv_parts.append(seg)
        if tv_parts:
            lines.append('  TV macro: ' + '  '.join(tv_parts))
        else:
            lines.append('  TV macro: (no recent pushes — configure TradingView alerts → POST /postmortem/tv/macro)')
    else:
        lines.append('  TV macro: (not configured — see endpoint /postmortem/tv/macro)')

    return '\n'.join(lines) if lines else '(macro unavailable)'


def snapshot_health():
    snap = _CACHE or {}
    return {
        'cache_age_sec': int(time.time() - _CACHE_TS) if _CACHE_TS else None,
        'sources_used': snap.get('sources_used', []),
        'tv_symbols_cached': list((snap.get('tv_macro') or {}).keys()),
    }
