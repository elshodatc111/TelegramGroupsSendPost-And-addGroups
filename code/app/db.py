"""SQLite yordamchi funksiyalari va jadvallar sxemasi."""
import contextlib
import sqlite3
from datetime import datetime

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS groups(
    tg_id INTEGER PRIMARY KEY,
    title TEXT, username TEXT, kind TEXT,
    members INTEGER, can_post INTEGER DEFAULT 1, synced_at TEXT
);
CREATE TABLE IF NOT EXISTS group_lists(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS group_list_items(
    list_id INTEGER NOT NULL, tg_id INTEGER NOT NULL,
    PRIMARY KEY(list_id, tg_id),
    FOREIGN KEY(list_id) REFERENCES group_lists(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS templates(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, text TEXT, parse_mode TEXT DEFAULT 'none',
    media_path TEXT, media_type TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT, parse_mode TEXT DEFAULT 'none',
    media_path TEXT, media_type TEXT,
    status TEXT DEFAULT 'draft',
    min_delay INTEGER, max_delay INTEGER,
    scheduled_at TEXT, created_at TEXT, started_at TEXT, finished_at TEXT,
    next_at TEXT, error TEXT,
    total INTEGER DEFAULT 0, done INTEGER DEFAULT 0, failed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS job_targets(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL, tg_id INTEGER, title TEXT,
    status TEXT DEFAULT 'pending', error TEXT, sent_at TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_targets_job ON job_targets(job_id);
CREATE TABLE IF NOT EXISTS join_batches(
    id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, status TEXT DEFAULT 'draft',
    min_delay INTEGER, max_delay INTEGER, daily_limit INTEGER,
    created_at TEXT, started_at TEXT, finished_at TEXT, next_at TEXT, resume_at TEXT, error TEXT,
    total INTEGER DEFAULT 0, raw TEXT
);
CREATE TABLE IF NOT EXISTS join_targets(
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,
    ref TEXT, kind TEXT, key TEXT,
    status TEXT DEFAULT 'pending', detail TEXT, title TEXT, tried_at TEXT,
    FOREIGN KEY(batch_id) REFERENCES join_batches(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_jt_batch ON join_targets(batch_id);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init():
    with contextlib.closing(_conn()) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        c.commit()


def q(sql, args=()):
    with contextlib.closing(_conn()) as c:
        return c.execute(sql, args).fetchall()


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql, args=()):
    with contextlib.closing(_conn()) as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


def many(sql, seq):
    with contextlib.closing(_conn()) as c:
        c.executemany(sql, seq)
        c.commit()


def get_setting(key, default=None):
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(key, value):
    ex("INSERT INTO settings(key,value) VALUES(?,?) "
       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
