"""Plain-english knowledge base.

Each KB entry = "we learned this about this coin/pattern". Written by
the synthesizer after every post-mortem. Read by the entry gate before
every new trade.

Pattern key format:
    "{coin}:{side}"                                  — coin+side baseline
    "{coin}:{side}:regime={regime}"                  — regime-specific
    "{coin}:{side}:session={session}"                — session-specific
    "{coin}:{side}:engine={engine}"                  — engine-specific
    "{coin}:{side}:funding={pos|neg|flat}"           — funding-state specific

When the same pattern_key is written again, reinforced_count increments
and weight increases (saturating). The summary gets the most recent
framing but evidence accumulates.
"""
import os
import time
import json
import threading

from . import db

_LOCK = threading.Lock()

# Weight saturates at this value — prevents a single heavily-repeated pattern
# from drowning all other evidence.
MAX_WEIGHT = 5.0
# Decay half-life: weights halve every N days of inactivity
HALF_LIFE_DAYS = 21.0


def _now():
    return time.time()


def _decayed_weight(current_weight, last_updated_ts, now=None):
    """Apply time-decay since last_updated. weight *= 0.5^(days/half_life)."""
    if now is None: now = _now()
    days = max(0.0, (now - last_updated_ts) / 86400.0)
    if days < 0.1: return current_weight
    factor = 0.5 ** (days / HALF_LIFE_DAYS)
    return current_weight * factor


def write_entry(coin, side, pattern_key, summary, evidence=None, log_id=None):
    """Insert or reinforce a KB entry. Idempotent on (coin, side, pattern_key)."""
    try:
        with _LOCK, db._conn() as c:
            existing = c.execute(
                'SELECT id, reinforced_count, weight, updated_at FROM kb_entries '
                'WHERE coin=? AND side=? AND pattern_key=?',
                (coin, side, pattern_key)
            ).fetchone()
            now = _now()
            if existing:
                new_weight = min(MAX_WEIGHT, _decayed_weight(existing['weight'], existing['updated_at'], now) + 1.0)
                c.execute('''
                    UPDATE kb_entries SET
                        updated_at = ?,
                        summary = ?,
                        evidence_json = ?,
                        reinforced_count = reinforced_count + 1,
                        weight = ?,
                        last_log_id = COALESCE(?, last_log_id)
                    WHERE id = ?
                ''', (now, summary, json.dumps(evidence or {}, default=str),
                      new_weight, log_id, existing['id']))
            else:
                c.execute('''
                    INSERT INTO kb_entries(created_at, updated_at, coin, side, pattern_key,
                                           summary, evidence_json, reinforced_count, weight, last_log_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1.0, ?)
                ''', (now, now, coin, side, pattern_key, summary,
                      json.dumps(evidence or {}, default=str), log_id))
            c.commit()
            return True
    except Exception as e:
        print(f'[postmortem.kb] write_entry err: {e}', flush=True)
        return False


def read_relevant(coin, side, extra_pattern_keys=None, max_entries=8):
    """Return KB entries relevant to this coin/side, ranked by decayed weight.

    extra_pattern_keys: list of extra keys to match exactly (e.g.
    "BTC:BUY:regime=squeeze", "BTC:BUY:session=asian"). Adds to the
    coin+side baseline which is always included.

    Returns list of dicts ordered by effective weight descending.
    """
    try:
        now = _now()
        keys = set(extra_pattern_keys or [])
        keys.add(f'{coin}:{side}')
        with db._conn() as c:
            rows = c.execute(
                'SELECT * FROM kb_entries WHERE coin=? AND side=? ORDER BY updated_at DESC LIMIT 100',
                (coin, side)
            ).fetchall()
        if not rows:
            return []
        scored = []
        for r in rows:
            d = dict(r)
            # Apply decay
            d['effective_weight'] = _decayed_weight(d['weight'], d['updated_at'], now)
            # Boost if pattern_key matches any requested extra key
            if d['pattern_key'] in keys:
                d['effective_weight'] *= 2.0
            # Parse evidence for caller convenience
            try:
                d['evidence'] = json.loads(d.get('evidence_json') or '{}')
            except Exception:
                d['evidence'] = {}
            scored.append(d)
        scored.sort(key=lambda x: x['effective_weight'], reverse=True)
        # Drop entries with weight effectively zero (saves prompt tokens)
        scored = [x for x in scored if x['effective_weight'] >= 0.1]
        return scored[:max_entries]
    except Exception as e:
        print(f'[postmortem.kb] read_relevant err: {e}', flush=True)
        return []


def format_for_prompt(entries, max_chars=1200):
    """Compact KB entries into a Claude-readable block. Respects char budget."""
    if not entries:
        return '(no relevant KB entries yet)'
    out = []
    used = 0
    for e in entries:
        line = (f'- [{e["pattern_key"]}, ×{e["reinforced_count"]}, w={e["effective_weight"]:.1f}] '
                f'{e["summary"][:240]}')
        if used + len(line) > max_chars:
            break
        out.append(line)
        used += len(line) + 1
    return '\n'.join(out)


def list_entries(coin=None, side=None, limit=100):
    """Dashboard helper."""
    try:
        with db._conn() as c:
            sql = 'SELECT * FROM kb_entries'
            where = []; args = []
            if coin: where.append('coin=?'); args.append(coin)
            if side: where.append('side=?'); args.append(side)
            if where: sql += ' WHERE ' + ' AND '.join(where)
            sql += ' ORDER BY updated_at DESC LIMIT ?'
            args.append(limit)
            rows = c.execute(sql, args).fetchall()
            now = _now()
            out = []
            for r in rows:
                d = dict(r)
                d['effective_weight'] = _decayed_weight(d['weight'], d['updated_at'], now)
                out.append(d)
            return out
    except Exception as e:
        print(f'[postmortem.kb] list_entries err: {e}', flush=True)
        return []


def delete_entry(entry_id):
    try:
        with _LOCK, db._conn() as c:
            c.execute('DELETE FROM kb_entries WHERE id=?', (entry_id,))
            c.commit()
            return True
    except Exception as e:
        print(f'[postmortem.kb] delete_entry err: {e}', flush=True)
        return False


def reset_coin(coin):
    try:
        with _LOCK, db._conn() as c:
            c.execute('DELETE FROM kb_entries WHERE coin=?', (coin,))
            c.commit()
            return True
    except Exception as e:
        return False
