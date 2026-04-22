"""Economic calendar.

Free: ForexFactory weekly XML feed — parsed locally.

Output: upcoming high-impact events within a time window.
Cached 15min.
"""
import os
import re
import time
import threading
import urllib.request
from datetime import datetime, timezone

_CACHE = None
_CACHE_TS = 0.0
_LOCK = threading.Lock()
TTL = int(os.environ.get('POSTMORTEM_CAL_TTL', '900'))
TIMEOUT = 8
USER_AGENT = 'Mozilla/5.0 (precog-postmortem)'

# ForexFactory's weekly XML feed
FF_URL = 'https://nfs.faireconomy.media/ff_calendar_thisweek.xml'

# Also fetch next week when we're on Thursday+ (rolling)
FF_URL_NEXT = 'https://nfs.faireconomy.media/ff_calendar_nextweek.xml'


def _http(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/xml'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


_EVENT_RE = re.compile(r'<event>(.*?)</event>', re.DOTALL)
_TAG_RE = lambda t: re.compile(rf'<{t}[^>]*>(.*?)</{t}>', re.DOTALL)


def _parse_ff(xml_text):
    events = []
    for m in _EVENT_RE.finditer(xml_text):
        blk = m.group(1)
        def tag(t):
            mm = _TAG_RE(t).search(blk)
            return mm.group(1).strip() if mm else ''
        title = tag('title')
        country = tag('country')
        date = tag('date')       # e.g. "12-10-2025"
        t_ = tag('time')          # e.g. "8:30am"
        impact = tag('impact')    # "High" / "Medium" / "Low"
        if not (title and date): continue
        try:
            # ff_calendar uses format "MM-DD-YYYY" and time "H:MMam/pm" (US Eastern)
            if t_.lower() in ('all day', 'tentative', ''):
                dt_str = f'{date} 12:00pm'
            else:
                dt_str = f'{date} {t_}'
            # Parse as Eastern Time (approximation: use UTC-5 / UTC-4 by date)
            dt = datetime.strptime(dt_str, '%m-%d-%Y %I:%M%p')
            # Rough ET → UTC: add 4-5 hrs. Use DST-aware heuristic: add 4 if Mar-Nov else 5.
            offset = 4 if 3 <= dt.month <= 11 else 5
            ts = dt.replace(tzinfo=timezone.utc).timestamp() + offset * 3600
        except Exception:
            continue
        events.append({
            'ts': ts,
            'title': title[:160],
            'country': country,
            'impact': impact.lower(),
        })
    return events


def fetch_all(force=False):
    global _CACHE, _CACHE_TS
    now = time.time()
    with _LOCK:
        if not force and _CACHE is not None and (now - _CACHE_TS) < TTL:
            return _CACHE
    events = []
    for url in (FF_URL, FF_URL_NEXT):
        try:
            raw = _http(url)
            events.extend(_parse_ff(raw.decode('utf-8', errors='ignore')))
        except Exception:
            pass
    events.sort(key=lambda e: e['ts'])
    with _LOCK:
        _CACHE = events
        _CACHE_TS = now
    return events


def upcoming(window_sec=7200, impact_min='high', currencies=None):
    """Return events starting within `window_sec` filtered by impact/country.

    impact_min: 'high', 'med', or 'low' — includes that tier and higher.
    currencies: list like ['USD','EUR']; None = all.
    """
    events = fetch_all()
    now = time.time()
    order = {'high': 2, 'medium': 1, 'med': 1, 'low': 0}
    min_rank = order.get(impact_min.lower(), 2)
    out = []
    for e in events:
        if e['ts'] < now - 300: continue   # drop past events (5min grace)
        if e['ts'] > now + window_sec: break
        if order.get(e['impact'], 0) < min_rank: continue
        if currencies and e['country'].upper() not in {c.upper() for c in currencies}: continue
        out.append({**e, 'minutes_until': int((e['ts'] - now) / 60)})
    return out


def format_for_prompt(events=None, window_sec=7200):
    if events is None:
        events = upcoming(window_sec=window_sec, impact_min='high', currencies=['USD','EUR','GBP'])
    if not events:
        return '(no high-impact events in next 2h)'
    lines = []
    for e in events[:8]:
        lines.append(f'  - {e["minutes_until"]:>4}min | {e["country"]:3} | {e["impact"].upper():6} | {e["title"]}')
    return '\n'.join(lines)


def snapshot_health():
    return {
        'cache_age_sec': int(time.time() - _CACHE_TS) if _CACHE_TS else None,
        'total_events': len(_CACHE) if _CACHE else 0,
    }
