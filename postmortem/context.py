"""Unified market context: news + macro + calendar.

Entry gate + trade finder read from here. Results cached 60s.
Safe to call thousands of times per minute.
"""
import os
import time
import threading

from . import news, macro, calendar as cal

_CACHE = {}            # coin -> (ctx_dict, expires_at)
_GLOBAL = None
_GLOBAL_TS = 0.0
_LOCK = threading.Lock()
TTL = int(os.environ.get('POSTMORTEM_CTX_TTL', '60'))


def global_context():
    """Context not specific to any coin — macro + calendar only."""
    global _GLOBAL, _GLOBAL_TS
    now = time.time()
    with _LOCK:
        if _GLOBAL is not None and (now - _GLOBAL_TS) < TTL:
            return _GLOBAL
    snap = {
        'ts': now,
        'macro': macro.fetch_all(),
        'calendar_2h': cal.upcoming(window_sec=7200, impact_min='high',
                                    currencies=['USD','EUR','GBP','JPY','CNY']),
        'calendar_8h': cal.upcoming(window_sec=28800, impact_min='high',
                                    currencies=['USD','EUR','GBP','JPY','CNY']),
        'news_latest': news.fetch_all()[:10],
    }
    with _LOCK:
        _GLOBAL = snap
        _GLOBAL_TS = now
    return snap


def for_coin(coin, window_sec=3600):
    """Coin-specific context: coin news + global macro/calendar."""
    now = time.time()
    ckey = coin.upper() if coin else '_none_'
    with _LOCK:
        cached = _CACHE.get(ckey)
        if cached and cached[1] > now:
            return cached[0]

    g = global_context()
    ctx = {
        'ts': now,
        'coin': coin,
        'news_for_coin': news.recent_for_coin(coin, window_sec=window_sec, max_items=6),
        'macro': g['macro'],
        'calendar_2h': g['calendar_2h'],
        'calendar_8h': g['calendar_8h'],
    }
    with _LOCK:
        _CACHE[ckey] = (ctx, now + TTL)
    return ctx


def format_for_prompt(coin_ctx, max_total_chars=2800):
    """Render a unified block for Claude prompts."""
    parts = []

    parts.append('=== MACRO SNAPSHOT ===')
    parts.append(macro.format_for_prompt(coin_ctx.get('macro')))

    parts.append('\n=== ECON CALENDAR — next 2h (high impact) ===')
    parts.append(cal.format_for_prompt(coin_ctx.get('calendar_2h'), window_sec=7200))

    if coin_ctx.get('calendar_8h'):
        # Only show 8h if 2h was empty, to keep prompt tight
        if not coin_ctx.get('calendar_2h'):
            parts.append('\n=== NEXT 8h (high impact) ===')
            parts.append(cal.format_for_prompt(coin_ctx.get('calendar_8h'), window_sec=28800))

    parts.append(f'\n=== NEWS — last 60min for {coin_ctx.get("coin","ALL")} + macro ===')
    parts.append(news.format_for_prompt(coin_ctx.get('news_for_coin', []),
                                        max_chars=1000))

    out = '\n'.join(parts)
    if len(out) > max_total_chars:
        out = out[:max_total_chars] + '\n...(truncated)'
    return out


def health():
    return {
        'news': news.snapshot(),
        'macro': macro.snapshot_health(),
        'calendar': cal.snapshot_health(),
        'global_cache_age_sec': int(time.time() - _GLOBAL_TS) if _GLOBAL_TS else None,
        'coin_cache_size': len(_CACHE),
    }


def invalidate():
    global _GLOBAL, _GLOBAL_TS
    with _LOCK:
        _GLOBAL = None
        _GLOBAL_TS = 0.0
        _CACHE.clear()
