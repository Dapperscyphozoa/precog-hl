"""SQLite persistence for post-mortem tuning engine.

Tables:
    signal_params   -- live per-coin per-component tuned parameters
    component_vetos -- hard vetos per coin/component (e.g. "never short LIT during neg funding")
    postmortem_log  -- audit trail of every forensic run
    agent_findings  -- per-agent verdict + proposed delta per run
    param_history   -- every param change with before/after + reasoning
"""
import os
import sqlite3
import time
import json
import threading

# Render persistent disk path; falls back to /tmp for local tests
DB_PATH = os.environ.get('POSTMORTEM_DB', '/var/data/postmortem.db')
_LOCK = threading.Lock()


def _ensure_dir():
    d = os.path.dirname(DB_PATH)
    if d and not os.path.exists(d):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def _conn():
    _ensure_dir()
    c = sqlite3.connect(DB_PATH, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA synchronous=NORMAL')
    return c


def init_db():
    """Create tables if they don't exist. Idempotent."""
    with _LOCK, _conn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS signal_params (
            coin          TEXT NOT NULL,
            component     TEXT NOT NULL,
            param_name    TEXT NOT NULL,
            param_value   REAL NOT NULL,
            default_value REAL NOT NULL,
            sample_count  INTEGER DEFAULT 0,
            last_tuned_at REAL,
            last_reason   TEXT,
            PRIMARY KEY (coin, component, param_name)
        );

        CREATE TABLE IF NOT EXISTS component_vetos (
            coin         TEXT NOT NULL,
            component    TEXT NOT NULL,
            active       INTEGER DEFAULT 1,
            created_at   REAL,
            reason       TEXT,
            expires_at   REAL,
            PRIMARY KEY (coin, component)
        );

        CREATE TABLE IF NOT EXISTS postmortem_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            coin        TEXT NOT NULL,
            side        TEXT,
            engine      TEXT,
            pnl_pct     REAL,
            is_win      INTEGER,
            entry_px    REAL,
            exit_reason TEXT,
            duration_s  REAL,
            pos_json    TEXT,
            agents_run  INTEGER DEFAULT 0,
            deltas_applied INTEGER DEFAULT 0,
            status      TEXT DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS agent_findings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            log_id        INTEGER NOT NULL,
            ts            REAL NOT NULL,
            agent_name    TEXT NOT NULL,
            verdict       TEXT,
            confidence    REAL,
            reasoning     TEXT,
            proposed_delta TEXT,
            applied       INTEGER DEFAULT 0,
            FOREIGN KEY (log_id) REFERENCES postmortem_log(id)
        );

        CREATE TABLE IF NOT EXISTS param_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL NOT NULL,
            coin         TEXT NOT NULL,
            component    TEXT NOT NULL,
            param_name   TEXT NOT NULL,
            old_value    REAL,
            new_value    REAL,
            delta        REAL,
            reason       TEXT,
            log_id       INTEGER,
            agent_name   TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_pm_log_coin ON postmortem_log(coin, ts);
        CREATE INDEX IF NOT EXISTS idx_findings_log ON agent_findings(log_id);
        CREATE INDEX IF NOT EXISTS idx_history_coin ON param_history(coin, component, ts);
        ''')
        c.commit()


# ─────────────────────────────────────────────────────
# PARAM READ (hot path — called at every signal tick)
# ─────────────────────────────────────────────────────
def read_param(coin: str, component: str, param_name: str):
    """Return tuned value or None if not set."""
    try:
        with _conn() as c:
            row = c.execute(
                'SELECT param_value FROM signal_params WHERE coin=? AND component=? AND param_name=?',
                (coin, component, param_name)
            ).fetchone()
            return row['param_value'] if row else None
    except Exception:
        return None


def read_veto(coin: str, component: str):
    """Return True if component is vetoed for this coin."""
    try:
        now = time.time()
        with _conn() as c:
            row = c.execute(
                'SELECT active, expires_at FROM component_vetos WHERE coin=? AND component=?',
                (coin, component)
            ).fetchone()
            if not row: return False
            if not row['active']: return False
            if row['expires_at'] and row['expires_at'] < now: return False
            return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────
# WRITE OPS (post-mortem only)
# ─────────────────────────────────────────────────────
def upsert_param(coin, component, param_name, new_value, default_value, reason, log_id=None, agent_name=None):
    with _LOCK, _conn() as c:
        old = c.execute(
            'SELECT param_value FROM signal_params WHERE coin=? AND component=? AND param_name=?',
            (coin, component, param_name)
        ).fetchone()
        old_value = old['param_value'] if old else default_value
        c.execute('''
            INSERT INTO signal_params(coin, component, param_name, param_value, default_value,
                                      sample_count, last_tuned_at, last_reason)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(coin, component, param_name) DO UPDATE SET
                param_value = excluded.param_value,
                sample_count = signal_params.sample_count + 1,
                last_tuned_at = excluded.last_tuned_at,
                last_reason = excluded.last_reason
        ''', (coin, component, param_name, new_value, default_value, time.time(), reason))
        c.execute('''
            INSERT INTO param_history(ts, coin, component, param_name, old_value, new_value, delta, reason, log_id, agent_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (time.time(), coin, component, param_name, old_value, new_value,
              new_value - old_value, reason, log_id, agent_name))
        c.commit()


def set_veto(coin, component, reason, expires_in_sec=None, log_id=None):
    exp = time.time() + expires_in_sec if expires_in_sec else None
    with _LOCK, _conn() as c:
        c.execute('''
            INSERT INTO component_vetos(coin, component, active, created_at, reason, expires_at)
            VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(coin, component) DO UPDATE SET
                active=1, created_at=excluded.created_at,
                reason=excluded.reason, expires_at=excluded.expires_at
        ''', (coin, component, time.time(), reason, exp))
        c.commit()


def clear_veto(coin, component):
    with _LOCK, _conn() as c:
        c.execute('UPDATE component_vetos SET active=0 WHERE coin=? AND component=?', (coin, component))
        c.commit()


def create_log_entry(coin, side, engine, pnl_pct, entry_px, exit_reason, duration_s, pos_dict):
    with _LOCK, _conn() as c:
        cur = c.execute('''
            INSERT INTO postmortem_log(ts, coin, side, engine, pnl_pct, is_win, entry_px,
                                       exit_reason, duration_s, pos_json, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
        ''', (time.time(), coin, side, engine, pnl_pct, 1 if pnl_pct > 0 else 0,
              entry_px, exit_reason, duration_s, json.dumps(pos_dict, default=str)))
        c.commit()
        return cur.lastrowid


def update_log_entry(log_id, agents_run=None, deltas_applied=None, status=None):
    with _LOCK, _conn() as c:
        parts = []; args = []
        if agents_run is not None: parts.append('agents_run=?'); args.append(agents_run)
        if deltas_applied is not None: parts.append('deltas_applied=?'); args.append(deltas_applied)
        if status is not None: parts.append('status=?'); args.append(status)
        if parts:
            args.append(log_id)
            c.execute(f'UPDATE postmortem_log SET {", ".join(parts)} WHERE id=?', args)
            c.commit()


def record_finding(log_id, agent_name, verdict, confidence, reasoning, proposed_delta, applied):
    with _LOCK, _conn() as c:
        c.execute('''
            INSERT INTO agent_findings(log_id, ts, agent_name, verdict, confidence,
                                       reasoning, proposed_delta, applied)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (log_id, time.time(), agent_name, verdict, confidence,
              reasoning, json.dumps(proposed_delta, default=str), 1 if applied else 0))
        c.commit()


# ─────────────────────────────────────────────────────
# READ OPS (dashboard endpoints)
# ─────────────────────────────────────────────────────
def list_params(coin=None, limit=200):
    with _conn() as c:
        if coin:
            rows = c.execute(
                'SELECT * FROM signal_params WHERE coin=? ORDER BY last_tuned_at DESC LIMIT ?',
                (coin, limit)
            ).fetchall()
        else:
            rows = c.execute(
                'SELECT * FROM signal_params ORDER BY last_tuned_at DESC LIMIT ?',
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def list_vetos(active_only=True):
    with _conn() as c:
        sql = 'SELECT * FROM component_vetos'
        if active_only: sql += ' WHERE active=1 AND (expires_at IS NULL OR expires_at > ' + str(time.time()) + ')'
        sql += ' ORDER BY created_at DESC'
        return [dict(r) for r in c.execute(sql).fetchall()]


def list_log(limit=50):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            'SELECT * FROM postmortem_log ORDER BY ts DESC LIMIT ?', (limit,)
        ).fetchall()]


def list_findings(log_id):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            'SELECT * FROM agent_findings WHERE log_id=? ORDER BY ts ASC', (log_id,)
        ).fetchall()]


def list_history(coin=None, component=None, limit=100):
    with _conn() as c:
        where = []; args = []
        if coin: where.append('coin=?'); args.append(coin)
        if component: where.append('component=?'); args.append(component)
        sql = 'SELECT * FROM param_history'
        if where: sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY ts DESC LIMIT ?'
        args.append(limit)
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def reset_coin_params(coin):
    """Wipe all tuned params + vetos for a coin. Used for emergency rollback."""
    with _LOCK, _conn() as c:
        c.execute('DELETE FROM signal_params WHERE coin=?', (coin,))
        c.execute('DELETE FROM component_vetos WHERE coin=?', (coin,))
        c.commit()
