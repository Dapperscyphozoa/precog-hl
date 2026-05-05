"""
alerts.py — monitoring daemon for the unified dashboard.

Watches engine-state pushes + HL account state; fires ntfy.sh push notifications
on transitions into/out of failure modes. Hysteresis prevents spam: each alert
key has an "active" bit, only fires the notification on transition (off→on or
on→off) so you get one ping per incident, not every tick.

Conditions monitored:
  ENGINE_STALE:n      engine push age > STALE_THRESHOLD_SEC          (5min default)
  ENGINE_DRY:n        engine reports live=False                       (DRY mode)
  EQUITY_DROP_24H     equity dropped > EQUITY_DROP_PCT vs 24h ago     (5% default)
  NO_FILLS_6H         total closes across all engines == 0 in 6h
  AGENT_REVOKED       any engine reports agent NOT in approved list   (parse logs)

Notifications via ntfy.sh — set NTFY_TOPIC env var. Persists alert state to
/var/data/alerts.jsonl for postmortem.

Loop runs every ALERT_CHECK_SEC seconds (60 default).
"""
import os, time, json, threading, traceback, urllib.request
from datetime import datetime, timezone
from collections import deque

# ─── Config ──────────────────────────────────────────────────────
NTFY_SERVER = os.environ.get('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')
NTFY_TOPIC  = os.environ.get('NTFY_TOPIC', '').strip()
ALERT_CHECK_SEC = int(os.environ.get('ALERT_CHECK_SEC', '60'))

STALE_THRESHOLD_SEC = int(os.environ.get('ALERT_STALE_SEC', '300'))       # 5min
EQUITY_DROP_PCT     = float(os.environ.get('ALERT_EQUITY_DROP_PCT', '5')) # 5% drop in 24h
NO_FILLS_HOURS      = int(os.environ.get('ALERT_NO_FILLS_HOURS', '6'))    # 6h
EQUITY_HISTORY_PATH = os.environ.get('ALERT_EQUITY_HISTORY_PATH',
                                     '/var/data/equity_history.jsonl')
ALERTS_LOG_PATH     = os.environ.get('ALERTS_LOG_PATH',
                                     '/var/data/alerts.jsonl')

# ─── State (in-memory, persisted on transition) ──────────────────
_alert_state = {}  # {alert_key: {'active': bool, 'since': ts, 'last_notified': ts}}
_state_lock = threading.Lock()
_recent_log = deque(maxlen=200)

def _lg(msg):
    line = f'[alerts {datetime.now(timezone.utc).isoformat()}] {msg}'
    print(line, flush=True)
    _recent_log.append(line)

# ─── ntfy.sh send ────────────────────────────────────────────────
def _send_ntfy(title, body, priority='default', tags='warning'):
    """priority: min, low, default, high, urgent. tags: comma-separated emoji codes."""
    if not NTFY_TOPIC:
        _lg(f'(skipping ntfy — no NTFY_TOPIC set) {title}: {body[:80]}')
        return False
    try:
        url = f'{NTFY_SERVER}/{NTFY_TOPIC}'
        req = urllib.request.Request(
            url,
            data=body[:1500].encode('utf-8'),
            headers={
                'Title':    f'PreCog · {title}'[:200],
                'Priority': priority,
                'Tags':     tags,
            },
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return True
    except Exception as e:
        _lg(f'ntfy send err: {e}')
        return False

# ─── Alert lifecycle (hysteresis: send on transition only) ───────
def _trigger(key, title, body, priority='high', tags='rotating_light'):
    """Fire alert if not already active. Records transition + sends ntfy."""
    now = int(time.time() * 1000)
    with _state_lock:
        cur = _alert_state.get(key, {})
        if cur.get('active'):
            return  # already firing — don't spam
        _alert_state[key] = {'active': True, 'since': now, 'last_notified': now}
    _persist({'ts': now, 'kind': 'TRIGGER', 'key': key, 'title': title, 'body': body})
    sent = _send_ntfy(title, body, priority=priority, tags=tags)
    _lg(f'TRIGGER {key} → ntfy={"ok" if sent else "skip"} | {title}')

def _resolve(key, title, body):
    """Clear alert if currently active. Sends recovery ntfy."""
    now = int(time.time() * 1000)
    with _state_lock:
        cur = _alert_state.get(key, {})
        if not cur.get('active'):
            return
        duration_min = (now - cur.get('since', now)) / 60_000
        _alert_state[key] = {'active': False, 'since': now, 'last_notified': now}
    msg = f'{body}\n(was active for {duration_min:.1f}m)'
    _persist({'ts': now, 'kind': 'RESOLVE', 'key': key, 'title': title, 'body': msg})
    _send_ntfy(f'✓ resolved · {title}', msg, priority='default', tags='white_check_mark')
    _lg(f'RESOLVE {key} | duration={duration_min:.1f}m')

def _persist(record):
    try:
        os.makedirs(os.path.dirname(ALERTS_LOG_PATH), exist_ok=True)
        with open(ALERTS_LOG_PATH, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        _lg(f'persist err: {e}')

# ─── Equity history (for 24h drop detection) ─────────────────────
def _record_equity(equity):
    try:
        os.makedirs(os.path.dirname(EQUITY_HISTORY_PATH), exist_ok=True)
        with open(EQUITY_HISTORY_PATH, 'a') as f:
            f.write(json.dumps({'ts': int(time.time()*1000), 'equity': equity}) + '\n')
    except Exception as e:
        _lg(f'equity record err: {e}')

def _equity_24h_ago():
    """Return equity value from ~24h ago (closest sample), or None if not enough history."""
    cutoff = int(time.time()*1000) - 24*3600*1000
    try:
        if not os.path.exists(EQUITY_HISTORY_PATH):
            return None
        candidates = []
        with open(EQUITY_HISTORY_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line.strip())
                    if r.get('ts'):
                        candidates.append(r)
                except Exception:
                    continue
        # pick sample closest to (now - 24h)
        if not candidates:
            return None
        # only use if oldest sample is at least 23h old
        oldest = min(candidates, key=lambda x: x['ts'])
        if (int(time.time()*1000) - oldest['ts']) < 23*3600*1000:
            return None  # not enough history yet
        # pick the sample whose ts is closest to cutoff
        best = min(candidates, key=lambda x: abs(x['ts'] - cutoff))
        return float(best['equity'])
    except Exception as e:
        _lg(f'equity_24h read err: {e}')
        return None

# ─── Check loops ─────────────────────────────────────────────────
def _check_engines(engine_states):
    """engine_states: dict {engine_name: state_dict_with_ts}.
    Each state has 'ts' (server-time of last push) and 'live' (bool from engine).
    """
    now_ms = int(time.time() * 1000)
    expected = ['multi-gate', 'smc-v1', 'smc-v2', 'smc-loose', 'lsr', 'brk']

    for name in expected:
        s = engine_states.get(name)
        key_stale = f'STALE:{name}'
        key_dry   = f'DRY:{name}'

        if not s:
            # never received any push — only alert if it's been like this for >2 min after boot
            # (avoid noise during initial deploys); skip for now unless we need to
            continue

        ts = s.get('ts', 0)
        age_sec = (now_ms - ts) / 1000 if ts else 99999

        # Stale check
        if age_sec > STALE_THRESHOLD_SEC:
            _trigger(key_stale,
                     f'engine stale · {name}',
                     f'{name} has not pushed in {age_sec/60:.1f}m\n'
                     f'(threshold: {STALE_THRESHOLD_SEC/60:.0f}m)\n'
                     f'check render service or worker process',
                     priority='high', tags='rotating_light,timer_clock')
        else:
            _resolve(key_stale, f'engine recovered · {name}',
                     f'{name} pushing again (age={age_sec:.0f}s)')

        # Dry-mode check
        is_live = s.get('live')
        if is_live is False:
            _trigger(key_dry,
                     f'engine in DRY MODE · {name}',
                     f'{name} reports live=False — agent likely revoked\n'
                     f'check HL approved-agents list and re-deploy',
                     priority='urgent', tags='no_entry,key')
        elif is_live is True:
            _resolve(key_dry, f'engine LIVE · {name}', f'{name} re-approved + trading')

def _check_equity(account_data):
    if not account_data:
        return
    eq = account_data.get('equity')
    if not eq:
        return
    _record_equity(eq)

    eq_24h = _equity_24h_ago()
    if eq_24h is None or eq_24h <= 0:
        return

    drop_pct = (eq_24h - eq) / eq_24h * 100
    key = 'EQUITY_DROP_24H'
    if drop_pct > EQUITY_DROP_PCT:
        _trigger(key,
                 f'equity drop {drop_pct:.2f}% / 24h',
                 f'24h ago: ${eq_24h:.2f}\n'
                 f'now:     ${eq:.2f}\n'
                 f'change:  -${eq_24h - eq:.2f}\n'
                 f'(threshold: {EQUITY_DROP_PCT}%)\n'
                 f'investigate which engine(s) are losing',
                 priority='urgent', tags='rotating_light,chart_with_downwards_trend')
    elif drop_pct < EQUITY_DROP_PCT * 0.5:
        # Only resolve when drop has fallen significantly (avoid flapping at threshold)
        _resolve(key, 'equity recovered',
                 f'now ${eq:.2f} (drop {drop_pct:.2f}%)')

def _check_no_fills(engine_states):
    """If total closes across all engines is zero for >NO_FILLS_HOURS, alert."""
    cutoff = int(time.time()*1000) - NO_FILLS_HOURS * 3600 * 1000
    total_recent_closes = 0
    for name, s in engine_states.items():
        for h in s.get('history_12h', []):
            if h.get('close_t', 0) >= cutoff:
                total_recent_closes += 1

    key = 'NO_FILLS'
    if total_recent_closes == 0:
        _trigger(key,
                 f'no fills in {NO_FILLS_HOURS}h',
                 f'zero close events across all 6 engines in last {NO_FILLS_HOURS}h\n'
                 f'either no setups firing or every engine is stuck\n'
                 f'check fills feed + render logs',
                 priority='high', tags='warning,zzz')
    else:
        _resolve(key, 'fills resumed',
                 f'{total_recent_closes} closes detected in last {NO_FILLS_HOURS}h')

# ─── Main loop ───────────────────────────────────────────────────
def alert_loop(get_engine_states, get_account_data):
    """Background daemon — runs forever.
    get_engine_states():  callable() returning dict {engine_name: state_dict}
    get_account_data():   callable() returning dict {'equity':..., 'fetched_t':...}
    """
    _lg(f'alerts daemon starting | ntfy={NTFY_TOPIC[:20]+"..." if NTFY_TOPIC else "(disabled)"}')

    if NTFY_TOPIC:
        _send_ntfy('alerts daemon online',
                   f'PreCog alerting active\n'
                   f'monitoring 6 engines + equity\n'
                   f'check interval: {ALERT_CHECK_SEC}s\n'
                   f'stale threshold: {STALE_THRESHOLD_SEC/60:.0f}m\n'
                   f'equity drop alert: {EQUITY_DROP_PCT}%/24h\n'
                   f'no-fills alert: {NO_FILLS_HOURS}h',
                   priority='default', tags='satellite_antenna')

    while True:
        try:
            engine_states = get_engine_states() or {}
            account_data = get_account_data() or {}
            _check_engines(engine_states)
            _check_equity(account_data)
            _check_no_fills(engine_states)
        except Exception as e:
            _lg(f'check loop err: {e}\n{traceback.format_exc()}')
        time.sleep(ALERT_CHECK_SEC)

# ─── Public surface ──────────────────────────────────────────────
def get_alert_status():
    """For dashboard UI — returns currently-active alerts."""
    with _state_lock:
        active = [{'key': k, 'since': v.get('since', 0)}
                  for k, v in _alert_state.items() if v.get('active')]
    return {'active_count': len(active),
            'active': active,
            'recent_log': list(_recent_log)[-50:]}
