"""News ingestion for entry gate + trade finder.

Sources (all free, no paid API required):
  - RSS:        CoinDesk, CoinTelegraph, The Block, Decrypt, ZeroHedge,
                Reuters biz, FT markets, WSJ markets, Bloomberg crypto
  - CryptoPanic: 100 req/day free tier
  - Reddit:     public .json endpoints, no auth needed (60 req/min)

Output schema per item:
  {
    'ts': float,          # unix seconds
    'source': str,        # 'rss:coindesk' etc
    'title': str,
    'url': str,
    'body': str,          # excerpt, up to ~500 chars
    'coins_mentioned': list[str],  # ['BTC', 'ETH'] heuristic extraction
    'sentiment': 'bullish' | 'bearish' | 'neutral',  # filled later by classifier
    'impact': 'high' | 'med' | 'low',
  }

Cached in memory (180s default TTL) to absorb repeated reads per tick.
"""
import os
import re
import time
import json
import threading
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

_CACHE = None
_CACHE_TS = 0.0
_LOCK = threading.Lock()
TTL = int(os.environ.get('POSTMORTEM_NEWS_TTL', '180'))
MAX_ITEMS = int(os.environ.get('POSTMORTEM_NEWS_MAX', '40'))
FETCH_TIMEOUT = 8
USER_AGENT = 'precog-postmortem/1.0 (news-ingest)'

# RSS sources — free, no auth
RSS_FEEDS = [
    ('rss:coindesk',       'https://www.coindesk.com/arc/outboundfeeds/rss/'),
    ('rss:cointelegraph',  'https://cointelegraph.com/rss'),
    ('rss:decrypt',        'https://decrypt.co/feed'),
    ('rss:theblock',       'https://www.theblock.co/rss.xml'),
    ('rss:zerohedge',      'https://feeds.feedburner.com/zerohedge/feed'),
    ('rss:reuters_biz',    'https://www.reutersagency.com/feed/?best-topics=business-finance'),
    ('rss:bloomberg_xbt',  'https://www.bloomberg.com/feed/podcast/bloomberg-crypto.xml'),
]

CRYPTOPANIC_TOKEN = os.environ.get('CRYPTOPANIC_TOKEN', '')  # optional

# Simple coin-symbol mention extractor (no NLP needed at ingest time)
_COIN_SYMBOLS = ['BTC','ETH','SOL','XRP','DOGE','ADA','AVAX','LINK','DOT','MATIC','POL',
                 'UNI','LTC','BCH','ATOM','NEAR','APT','SUI','ARB','OP','INJ','TIA',
                 'SEI','PYTH','JUP','JTO','FTM','FIL','AAVE','MKR','COMP','SNX','LDO',
                 'RUNE','CAKE','CRV','BNB','TRX','TON','PEPE','SHIB','BONK','WIF',
                 'FARTCOIN','MOODENG','APE','UMA','POLYX','HYPE','BLUR','AIXBT','MORPHO',
                 'ENS','SUSHI','PUMP','LIT','MAGIC','PENDLE','TRB','SPX','VVV','AR']
_COIN_NAMES = {
    'bitcoin':'BTC','ethereum':'ETH','solana':'SOL','ripple':'XRP','dogecoin':'DOGE',
    'cardano':'ADA','avalanche':'AVAX','chainlink':'LINK','polkadot':'DOT','polygon':'POL',
    'uniswap':'UNI','litecoin':'LTC','cosmos':'ATOM','near':'NEAR','aptos':'APT','sui':'SUI',
    'arbitrum':'ARB','optimism':'OP','injective':'INJ','celestia':'TIA','binance':'BNB',
}

_SYM_RE = re.compile(r'\b(' + '|'.join(_COIN_SYMBOLS) + r')\b')
_NAME_RE = re.compile(r'\b(' + '|'.join(_COIN_NAMES.keys()) + r')\b', re.IGNORECASE)


def _extract_coins(text):
    if not text: return []
    coins = set(_SYM_RE.findall(text.upper()))
    for m in _NAME_RE.findall(text or ''):
        coins.add(_COIN_NAMES[m.lower()])
    return sorted(coins)


def _http_get(url, timeout=FETCH_TIMEOUT):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# Lightweight RSS parser — no feedparser dependency to keep install small
_ITEM_RE = re.compile(r'<item[\s>](.*?)</item>', re.DOTALL | re.IGNORECASE)
_TITLE_RE = re.compile(r'<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', re.DOTALL | re.IGNORECASE)
_LINK_RE = re.compile(r'<link[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>', re.DOTALL | re.IGNORECASE)
_DESC_RE = re.compile(r'<description[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>', re.DOTALL | re.IGNORECASE)
_DATE_RE = re.compile(r'<pubDate[^>]*>(.*?)</pubDate>', re.DOTALL | re.IGNORECASE)
_HTML_RE = re.compile(r'<[^>]+>')


def _strip_html(s):
    if not s: return ''
    s = _HTML_RE.sub('', s)
    return re.sub(r'\s+', ' ', s).strip()


def _parse_rfc822_date(s):
    try:
        import email.utils as eu
        t = eu.parsedate_tz(s)
        if t:
            return eu.mktime_tz(t)
    except Exception:
        pass
    return time.time()


def _fetch_rss(source, url):
    out = []
    try:
        raw = _http_get(url)
        text = raw.decode('utf-8', errors='ignore')
        items = list(_ITEM_RE.finditer(text))[:25]
        for item_m in items:
            block = item_m.group(1)
            title_m = _TITLE_RE.search(block)
            link_m = _LINK_RE.search(block)
            desc_m = _DESC_RE.search(block)
            date_m = _DATE_RE.search(block)
            title = _strip_html(title_m.group(1)) if title_m else ''
            link = _strip_html(link_m.group(1)) if link_m else ''
            desc = _strip_html(desc_m.group(1)) if desc_m else ''
            ts = _parse_rfc822_date(date_m.group(1)) if date_m else time.time()
            # Only include items from last 6h
            if ts < time.time() - 6 * 3600:
                continue
            out.append({
                'ts': ts, 'source': source, 'title': title[:220],
                'url': link, 'body': desc[:500],
                'coins_mentioned': _extract_coins(title + ' ' + desc),
                'sentiment': 'neutral',  # filled later
                'impact': 'low',
            })
    except Exception as e:
        pass
    return out


def _fetch_cryptopanic():
    if not CRYPTOPANIC_TOKEN:
        return []
    out = []
    try:
        url = f'https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_TOKEN}&kind=news&public=true'
        raw = _http_get(url, timeout=10)
        data = json.loads(raw.decode('utf-8', errors='ignore'))
        for r in (data.get('results') or [])[:30]:
            ts_str = r.get('published_at') or r.get('created_at') or ''
            try:
                import email.utils as eu
                ts = time.mktime(time.strptime(ts_str[:19], '%Y-%m-%dT%H:%M:%S'))
            except Exception:
                ts = time.time()
            if ts < time.time() - 6 * 3600:
                continue
            title = r.get('title', '')
            currencies = r.get('currencies', []) or []
            coins = [c.get('code', '').upper() for c in currencies if c.get('code')]
            out.append({
                'ts': ts, 'source': 'cryptopanic',
                'title': title[:220],
                'url': r.get('url', ''),
                'body': title[:500],
                'coins_mentioned': coins,
                'sentiment': 'neutral',
                'impact': 'med' if r.get('kind') == 'news' else 'low',
            })
    except Exception:
        pass
    return out


def _fetch_reddit_sub(sub):
    out = []
    try:
        url = f'https://www.reddit.com/r/{sub}/hot.json?limit=20'
        raw = _http_get(url, timeout=8)
        data = json.loads(raw.decode('utf-8', errors='ignore'))
        for c in (data.get('data', {}).get('children') or []):
            d = c.get('data', {})
            ts = d.get('created_utc', time.time())
            if ts < time.time() - 6 * 3600:
                continue
            title = d.get('title', '')
            body = d.get('selftext', '')[:400]
            out.append({
                'ts': ts, 'source': f'reddit:{sub}',
                'title': title[:220],
                'url': f"https://www.reddit.com{d.get('permalink', '')}",
                'body': body,
                'coins_mentioned': _extract_coins(title + ' ' + body),
                'sentiment': 'neutral',
                'impact': 'low',
            })
    except Exception:
        pass
    return out


def fetch_all(force=False):
    """Parallel fetch all sources. Cached TTL seconds."""
    global _CACHE, _CACHE_TS
    now = time.time()
    with _LOCK:
        if not force and _CACHE is not None and (now - _CACHE_TS) < TTL:
            return _CACHE

    items = []
    tasks = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for src, url in RSS_FEEDS:
            tasks.append(ex.submit(_fetch_rss, src, url))
        tasks.append(ex.submit(_fetch_cryptopanic))
        for sub in ('cryptocurrency', 'bitcoin', 'ethereum'):
            tasks.append(ex.submit(_fetch_reddit_sub, sub))
        for t in tasks:
            try:
                items.extend(t.result(timeout=FETCH_TIMEOUT + 4) or [])
            except Exception:
                pass

    # Dedupe by (title first 80 chars lowercased)
    seen = set(); deduped = []
    for it in items:
        k = (it.get('title', '')[:80]).lower().strip()
        if not k or k in seen: continue
        seen.add(k); deduped.append(it)

    # Newest first, clamp to max
    deduped.sort(key=lambda x: x['ts'], reverse=True)
    deduped = deduped[:MAX_ITEMS]

    with _LOCK:
        _CACHE = deduped
        _CACHE_TS = now
    return deduped


def recent_for_coin(coin, window_sec=3600, max_items=6):
    """Return items mentioning this coin or 'macro' within window."""
    items = fetch_all()
    now = time.time()
    out = []
    coin_u = (coin or '').upper()
    for it in items:
        if now - it['ts'] > window_sec: continue
        mentions = set(it.get('coins_mentioned') or [])
        # Always include very recent high-impact macro news (no coin tag)
        is_macro_fresh = (not mentions and (now - it['ts']) < 1800
                          and any(k in it['title'].lower()
                                  for k in ['fed','cpi','ppi','fomc','nfp','inflation','rate',
                                            'powell','treasury','gdp','jobs','unemployment',
                                            'war','strike','sanction','election']))
        if coin_u in mentions or is_macro_fresh:
            out.append(it)
        if len(out) >= max_items: break
    return out


def format_for_prompt(items, max_chars=1000):
    if not items:
        return '(no recent news)'
    lines = []; used = 0
    for it in items:
        age_m = int((time.time() - it['ts']) / 60)
        mentions = ','.join(it.get('coins_mentioned') or []) or '-'
        line = f'- [{it["source"]}, {age_m}m ago, {mentions}] {it["title"][:180]}'
        if used + len(line) > max_chars: break
        lines.append(line); used += len(line) + 1
    return '\n'.join(lines)


def snapshot():
    """Dashboard helper."""
    return {
        'cache_age_sec': int(time.time() - _CACHE_TS) if _CACHE_TS else None,
        'item_count': len(_CACHE) if _CACHE else 0,
        'sources_enabled': len(RSS_FEEDS) + (1 if CRYPTOPANIC_TOKEN else 0) + 3,
        'cryptopanic_enabled': bool(CRYPTOPANIC_TOKEN),
    }
