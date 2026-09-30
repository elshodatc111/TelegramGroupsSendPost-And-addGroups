"""SQLite: sxema, migratsiya va yordamchi funksiyalar."""
import contextlib
from contextvars import ContextVar
import json
import sqlite3
from datetime import datetime

from .config import DB_PATH, SESSION_DIR

GROUPS_DDL = """CREATE TABLE IF NOT EXISTS groups(
    account_id INTEGER NOT NULL DEFAULT 1, tg_id INTEGER NOT NULL,
    title TEXT, username TEXT, kind TEXT, members INTEGER, can_post INTEGER DEFAULT 1, synced_at TEXT,
    about TEXT, slowmode INTEGER DEFAULT 0, no_media INTEGER DEFAULT 0, no_links INTEGER DEFAULT 0,
    ads_flag INTEGER DEFAULT 0, ads_ok INTEGER DEFAULT 0, checked_at TEXT,
    PRIMARY KEY(account_id, tg_id))"""
LISTS_DDL = """CREATE TABLE IF NOT EXISTS group_lists(
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL DEFAULT 1, name TEXT,
    UNIQUE(account_id, name))"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, phone TEXT, username TEXT, session TEXT,
    created_at TEXT, daily_limit INTEGER DEFAULT 150, work_start TEXT DEFAULT '', work_end TEXT DEFAULT ''
);
{GROUPS_DDL};
{LISTS_DDL};
CREATE TABLE IF NOT EXISTS group_list_items(
    list_id INTEGER NOT NULL, tg_id INTEGER NOT NULL, PRIMARY KEY(list_id, tg_id),
    FOREIGN KEY(list_id) REFERENCES group_lists(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS group_tags(
    account_id INTEGER NOT NULL, tg_id INTEGER NOT NULL, tag TEXT NOT NULL,
    PRIMARY KEY(account_id, tg_id, tag)
);
CREATE TABLE IF NOT EXISTS group_members_log(
    account_id INTEGER NOT NULL, tg_id INTEGER NOT NULL, day TEXT NOT NULL, members INTEGER,
    PRIMARY KEY(account_id, tg_id, day)
);
CREATE TABLE IF NOT EXISTS blacklist(
    account_id INTEGER NOT NULL, tg_id INTEGER NOT NULL, title TEXT, reason TEXT, created_at TEXT,
    PRIMARY KEY(account_id, tg_id)
);
CREATE TABLE IF NOT EXISTS templates(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, text TEXT, parse_mode TEXT DEFAULT 'none',
    media_path TEXT, media_type TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS campaigns(
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL DEFAULT 1, name TEXT, created_at TEXT,
    min_delay INTEGER, max_delay INTEGER, utm_on INTEGER DEFAULT 0, utm_source TEXT, utm_campaign TEXT,
    skip_ads INTEGER DEFAULT 1, target_json TEXT DEFAULT '[]',
    recurrence TEXT DEFAULT 'none', rec_time TEXT DEFAULT '10:00', rec_days TEXT DEFAULT '',
    rec_active INTEGER DEFAULT 0, next_run TEXT, last_run TEXT,
    UNIQUE(account_id, name)
);
CREATE TABLE IF NOT EXISTS campaign_variants(
    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER NOT NULL, idx INTEGER,
    text TEXT, parse_mode TEXT DEFAULT 'none', media_json TEXT DEFAULT '[]', media_type TEXT,
    FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS variant_ptr(key TEXT PRIMARY KEY, ptr INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL DEFAULT 1, campaign_id INTEGER,
    text TEXT, parse_mode TEXT DEFAULT 'none', media_path TEXT, media_type TEXT,
    status TEXT DEFAULT 'draft', min_delay INTEGER, max_delay INTEGER,
    scheduled_at TEXT, created_at TEXT, started_at TEXT, finished_at TEXT, next_at TEXT, resume_at TEXT, error TEXT,
    utm_on INTEGER DEFAULT 0, utm_source TEXT, utm_campaign TEXT, name TEXT,
    total INTEGER DEFAULT 0, done INTEGER DEFAULT 0, failed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS job_variants(
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, idx INTEGER,
    text TEXT, parse_mode TEXT DEFAULT 'none', media_json TEXT DEFAULT '[]', media_type TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS job_targets(
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, tg_id INTEGER, title TEXT,
    status TEXT DEFAULT 'pending', error TEXT, sent_at TEXT,
    variant_id INTEGER, msg_ids TEXT, views INTEGER, forwards INTEGER, reactions INTEGER, replies INTEGER,
    deleted INTEGER DEFAULT 0, stat_at TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_targets_job ON job_targets(job_id);
CREATE TABLE IF NOT EXISTS join_batches(
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL DEFAULT 1, filename TEXT,
    status TEXT DEFAULT 'draft', min_delay INTEGER, max_delay INTEGER, daily_limit INTEGER,
    created_at TEXT, started_at TEXT, finished_at TEXT, next_at TEXT, resume_at TEXT, error TEXT,
    total INTEGER DEFAULT 0, raw TEXT
);
CREATE TABLE IF NOT EXISTS join_targets(
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL, ref TEXT, kind TEXT, key TEXT,
    status TEXT DEFAULT 'pending', detail TEXT, title TEXT, tried_at TEXT,
    FOREIGN KEY(batch_id) REFERENCES join_batches(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_jt_batch ON join_targets(batch_id);
CREATE TABLE IF NOT EXISTS account_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, kind TEXT, ts TEXT, info TEXT
);
CREATE INDEX IF NOT EXISTS idx_events ON account_events(account_id, kind, ts);
CREATE TABLE IF NOT EXISTS inbox(
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, chat_id INTEGER, chat_title TEXT,
    sender_id INTEGER, sender_name TEXT, sender_username TEXT, text TEXT, msg_id INTEGER,
    is_private INTEGER DEFAULT 0, kind TEXT, date TEXT, is_read INTEGER DEFAULT 0, replied INTEGER DEFAULT 0,
    UNIQUE(account_id, chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS canned_replies(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, name TEXT, picture TEXT,
    role TEXT DEFAULT 'user', status TEXT DEFAULT 'active', created_at TEXT, last_login TEXT
);
CREATE TABLE IF NOT EXISTS user_settings(
    user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY(user_id, key)
);
CREATE TABLE IF NOT EXISTS warmup(
    account_id INTEGER PRIMARY KEY, active INTEGER DEFAULT 0, start_date TEXT, plan_json TEXT
);
CREATE TABLE IF NOT EXISTS autojoin(
    account_id INTEGER PRIMARY KEY, active INTEGER DEFAULT 0, daily_target INTEGER DEFAULT 20,
    min_members INTEGER DEFAULT 500, min_per_day INTEGER DEFAULT 10, min_uz INTEGER DEFAULT 40,
    ad_wait_days INTEGER DEFAULT 2, min_delay INTEGER DEFAULT 120, max_delay INTEGER DEFAULT 300,
    manual_all INTEGER DEFAULT 0, keywords_json TEXT, block_extra TEXT, last_search TEXT, kw_ptr INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS disc_candidates(
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, username TEXT NOT NULL, title TEXT, about TEXT,
    members INTEGER, per_day REAL, uz INTEGER, category TEXT, keyword TEXT, score REAL, status TEXT, reason TEXT,
    batch_id INTEGER, found_at TEXT, joined_at TEXT, ad_at TEXT, tg_id INTEGER, UNIQUE(account_id, username)
);
CREATE TABLE IF NOT EXISTS audit_cfg(
    account_id INTEGER PRIMARY KEY, auto INTEGER DEFAULT 1, min_members INTEGER DEFAULT 100, check_ads INTEGER DEFAULT 1,
    check_post INTEGER DEFAULT 1, mute_all INTEGER DEFAULT 1, max_leave INTEGER DEFAULT 30,
    min_delay INTEGER DEFAULT 25, max_delay INTEGER DEFAULT 60, last_scan TEXT);
CREATE TABLE IF NOT EXISTS top_groups(
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, username TEXT NOT NULL, title TEXT, about TEXT,
    members INTEGER, per_day REAL, uz INTEGER, category TEXT, status TEXT, reason TEXT, batch_id INTEGER, found_at TEXT,
    UNIQUE(account_id, username));
CREATE TABLE IF NOT EXISTS top_meta(account_id INTEGER PRIMARY KEY, ptr_json TEXT, last_run TEXT, runs INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS leave_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, tg_id INTEGER, title TEXT,
    reason TEXT, auto INTEGER DEFAULT 0, ts TEXT
);
CREATE TABLE IF NOT EXISTS media_items(
    name TEXT PRIMARY KEY, original TEXT, kind TEXT, size INTEGER, created_at TEXT
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn(fk=True):
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    if fk:
        c.execute("PRAGMA foreign_keys=ON")
    return c


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


# Foydalanuvchiga xos sozlamalar (boshqalari umumiy: secret, parol)
USER_KEYS = {"api_id", "api_hash", "default_min", "default_max", "join_min", "join_max", "join_daily",
             "leave_on", "leave_bad", "leave_low", "leave_min_posts", "leave_min_views"}
cur_uid: ContextVar[int] = ContextVar("cur_uid", default=1)


def uget(uid, key, default=None):
    r = one("SELECT value FROM user_settings WHERE user_id=? AND key=?", (uid, key))
    return r["value"] if r else default


def uset(uid, key, value):
    ex("INSERT INTO user_settings(user_id,key,value) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",
       (uid, key, str(value)))


def get_setting(key, default=None):
    if key in USER_KEYS:
        return uget(cur_uid.get(), key, default)
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(key, value):
    if key in USER_KEYS:
        return uset(cur_uid.get(), key, value)
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, str(value)))


def del_setting(key):
    if key in USER_KEYS:
        return ex("DELETE FROM user_settings WHERE user_id=? AND key=?", (cur_uid.get(), key))
    ex("DELETE FROM settings WHERE key=?", (key,))


# ---------------- migratsiya ----------------
def _cols(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def _add(c, table, col, ddl):
    if col not in _cols(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init():
    with contextlib.closing(_conn(fk=False)) as c:
        c.execute("PRAGMA journal_mode=WAL")
        # eski sxemalarni yangilash (jadval yaratilishidan oldin)
        old_groups = _cols(c, "groups")
        if old_groups and "account_id" not in old_groups:
            c.executescript("ALTER TABLE groups RENAME TO groups_old;")
            c.executescript(GROUPS_DDL + ";")
            c.executescript("INSERT INTO groups(account_id,tg_id,title,username,kind,members,can_post,synced_at) "
                            "SELECT 1,tg_id,title,username,kind,members,can_post,synced_at FROM groups_old; "
                            "DROP TABLE groups_old;")
        old_lists = _cols(c, "group_lists")
        if old_lists and "account_id" not in old_lists:
            c.executescript("CREATE TABLE group_lists_n(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                            "account_id INTEGER NOT NULL DEFAULT 1, name TEXT, UNIQUE(account_id,name)); "
                            "INSERT INTO group_lists_n(id,account_id,name) SELECT id,1,name FROM group_lists; "
                            "DROP TABLE group_lists; ALTER TABLE group_lists_n RENAME TO group_lists;")
        c.executescript(SCHEMA)
        for table, col, ddl in [
            ("jobs", "account_id", "INTEGER NOT NULL DEFAULT 1"), ("jobs", "campaign_id", "INTEGER"),
            ("jobs", "resume_at", "TEXT"), ("jobs", "utm_on", "INTEGER DEFAULT 0"), ("jobs", "utm_source", "TEXT"),
            ("jobs", "utm_campaign", "TEXT"), ("jobs", "name", "TEXT"),
            ("job_targets", "variant_id", "INTEGER"), ("job_targets", "msg_ids", "TEXT"),
            ("job_targets", "views", "INTEGER"), ("job_targets", "forwards", "INTEGER"),
            ("job_targets", "reactions", "INTEGER"), ("job_targets", "replies", "INTEGER"),
            ("job_targets", "deleted", "INTEGER DEFAULT 0"), ("job_targets", "stat_at", "TEXT"),
            ("join_batches", "account_id", "INTEGER NOT NULL DEFAULT 1"),
            ("accounts", "user_id", "INTEGER"), ("media_items", "user_id", "INTEGER"),
            ("canned_replies", "user_id", "INTEGER"), ("autojoin", "ban_json", "TEXT"), ("groups", "muted", "INTEGER DEFAULT 0"), ("groups", "verdict", "TEXT"), ("groups", "verdict_reason", "TEXT"), ("groups", "audited_at", "TEXT"), ("jobs", "extra_json", "TEXT"),
        ]:
            _add(c, table, col, ddl)
        c.commit()
        # v6.3: guruh auditi avtomatik rejimi standart bo'yicha yoqiladi (bir marta)
        if not c.execute("SELECT 1 FROM settings WHERE key='audit_auto_v63'").fetchone():
            c.execute("UPDATE audit_cfg SET auto=1, max_leave=MAX(max_leave,30)")
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('audit_auto_v63','1')")
            c.commit()
        # foydalanuvchilar: birinchisi lokal administrator (eski ma'lumotlar unga tegishli)
        if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            c.execute("INSERT INTO users(id,email,name,role,status,created_at) VALUES(1,NULL,'Lokal foydalanuvchi','admin','active',?)", (now(),))
        c.execute("UPDATE accounts SET user_id=1 WHERE user_id IS NULL")
        c.execute("UPDATE media_items SET user_id=1 WHERE user_id IS NULL")
        c.execute("UPDATE canned_replies SET user_id=1 WHERE user_id IS NULL")
        for k in USER_KEYS:
            r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
            if r:
                c.execute("INSERT OR IGNORE INTO user_settings(user_id,key,value) VALUES(1,?,?)", (k, r[0]))
                c.execute("DELETE FROM settings WHERE key=?", (k,))
        c.commit()
        # birinchi akkaunt: eski session faylidan
        if not c.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() and (SESSION_DIR / "account.session").exists():
            c.execute("INSERT INTO accounts(id,name,session,created_at,user_id) VALUES(1,'Asosiy akkaunt','account',?,1)", (now(),))
        # eski joblar uchun variantlar
        for j in c.execute("SELECT * FROM jobs WHERE id NOT IN (SELECT job_id FROM job_variants)").fetchall():
            media = json.dumps([j["media_path"]]) if j["media_path"] else "[]"
            c.execute("INSERT INTO job_variants(job_id,idx,text,parse_mode,media_json,media_type) VALUES(?,?,?,?,?,?)",
                      (j["id"], 0, j["text"], j["parse_mode"], media, j["media_type"]))
        # eski shablonlar -> kampaniyalar (bir marta)
        done = c.execute("SELECT value FROM settings WHERE key='templates_migrated'").fetchone()
        if not done:
            for t in c.execute("SELECT * FROM templates").fetchall():
                cur = c.execute("INSERT OR IGNORE INTO campaigns(account_id,name,created_at,min_delay,max_delay) "
                                "VALUES(1,?,?,20,60)", (t["name"], t["created_at"] or now()))
                if cur.lastrowid:
                    media = json.dumps([t["media_path"]]) if t["media_path"] else "[]"
                    c.execute("INSERT INTO campaign_variants(campaign_id,idx,text,parse_mode,media_json,media_type) "
                              "VALUES(?,?,?,?,?,?)", (cur.lastrowid, 0, t["text"], t["parse_mode"], media, t["media_type"]))
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('templates_migrated','1')")
        c.commit()
