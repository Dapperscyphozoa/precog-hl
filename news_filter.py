"""News + macro-event filter. Polls RSS every 10min, writes regime.json.
Keyword-driven, no LLM. Monday catalysts: FOMC/CPI/Fed/China/Hormuz/geopolitical.
"""
import json, threading, time, urllib.request, re, os
from xml.etree import ElementTree as ET

STATE_FILE = '/var/data/regime.json' if os.path.isdir('/var/data') else '/tmp/regime.json'
POLL_SEC = 600

FEEDS = [
    ('coindesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/'),
    ('cointelegraph', 'https://cointelegraph.com/rss'),
    ('theblock', 'https://www.theblock.co/rss.xml'),
    ('decrypt', 'https://decrypt.co/feed'),
]

# pattern → (magnitude 1-5, blackout_min, direction: 1 bull / -1 bear / 0 unclear)
KEYWORDS = {
    r'\bFOMC\b': (5, 60, 0),
    r'\bCPI\b': (5, 45, 0),
    r'\brate\s*(decision|cut|hike)\b': (5, 60, 0),
    r'\bPowell\b': (4, 30, 0),
    r'\bnfp\b|\bnonfarm\b': (4, 30, 0),
    r'\bGDP\b': (3, 20, 0),
    r'\bHormuz\b|Straits?\s+of\s+Hormuz': (5, 60, -1),
    r'\bsanctions?\b': (4, 30, -1),
    r'\binvasion\b|\bwar\b.*(break|declare)': (5, 120, -1),
    r'\bChina\b.*(treasur|dump|sell)': (5, 60, -1),
    r'\bIran\b.*(strike|attack|missile)': (5, 90, -1),
    r'\bhack(ed|ing)?\b|exploit(ed)?': (4, 30, -1),
    r'\bSEC\b.*(suit|charge|reject|approve)': (4, 30, 0),
    r'\bETF\b.*(approve|reject|flow|inflow|outflow)': (3, 15, 0),
    r'liquidation.{0,20}(cascade|massive|billion)': (4, 20, -1),
    r'rally|surge|all.?time.?high|ATH\b': (2, 0, 1),
    r'adoption|institutional.{0,30}buy': (2, 0, 1),
}

_STATE = {'blackout': False, 'blackout_until': 0, 'direction_bias': 0,
          'risk_mult': 1.0, 'last_events': [], 'last_poll': 0}
_LOCK = threading.Lock()
_SEEN = set()
_RUN = False

def _fetch(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 PreCog/1.0'})
        return urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        return None

def _parse(raw):
    if not raw: return []
    try: root = ET.fromstring(raw)
    except ET.ParseError: return []
    out = []
    for item in root.iter('item'):
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        guid = (item.findtext('guid') or link or title)
        out.append({'title': title, 'guid': guid})
    return out

def _score(title):
    if not title: return None
    hits = []
    for pat, (mag, bmin, dh) in KEYWORDS.items():
        if re.search(pat, title, re.IGNORECASE):
            hits.append((mag, bmin, dh))
    if not hits: return None
    mag = max(h[0] for h in hits)
    bmin = max(h[1] for h in hits)
    dirs = [h[2] for h in hits if h[2] != 0]
    direction = 0 if not dirs else (sum(dirs) / len(dirs))
    return {'magnitude': mag, 'blackout_min': bmin, 'direction': direction}

def _update(events):
    now = time.time()
    max_bout = 0; max_mag = 0; dsum = 0; dcnt = 0
    for e in events:
        s = e.get('score')
        if not s: continue
        if s['magnitude'] >= 4:
            end = now + s['blackout_min'] * 60
            if end > max_bout: max_bout = end
        if s['magnitude'] > max_mag: max_mag = s['magnitude']
        if s['direction'] != 0:
            dsum += s['direction']; dcnt += 1
    with _LOCK:
        _STATE['blackout'] = now < max_bout
        _STATE['blackout_until'] = max_bout
        _STATE['direction_bias'] = (dsum / dcnt) if dcnt else 0
        if _STATE['blackout']: _STATE['risk_mult'] = 0.0
        elif max_mag >= 4 and _STATE['direction_bias'] == 0: _STATE['risk_mult'] = 0.3
        elif max_mag >= 3 and abs(_STATE['direction_bias']) >= 0.5: _STATE['risk_mult'] = 1.3
        else: _STATE['risk_mult'] = 1.0
        _STATE['last_events'] = [{'title': e['title'][:100], 'src': e['src'],
                                   'mag': e['score']['magnitude'],
                                   'dir': e['score']['direction']}
                                  for e in events if e.get('score')][:20]
        _STATE['last_poll'] = now
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w') as f: json.dump(_STATE, f, indent=2)
    except Exception: pass

def _poll():
    events = []
    for src, url in FEEDS:
        for item in _parse(_fetch(url)):
            if item['guid'] in _SEEN: continue
            _SEEN.add(item['guid'])
            sc = _score(item['title'])
            if sc:
                item['score'] = sc; item['src'] = src
                events.append(item)
    if len(_SEEN) > 10000: _SEEN.clear()
    _update(events)
    if events:
        print(f"[news] {len(events)} events | mult={_STATE['risk_mult']} blackout={_STATE['blackout']}", flush=True)

def _runner():
    while _RUN:
        try: _poll()
        except Exception as e: print(f"[news] {e}", flush=True)
        time.sleep(POLL_SEC)

def start():
    global _RUN
    if _RUN: return
    _RUN = True
    threading.Thread(target=_runner, daemon=True, name='news').start()
    print("[news] started (10m poll)", flush=True)

def get_risk_mult():
    with _LOCK: return _STATE['risk_mult']

def get_state():
    with _LOCK: return dict(_STATE)

def is_blackout():
    with _LOCK: return _STATE['blackout']
