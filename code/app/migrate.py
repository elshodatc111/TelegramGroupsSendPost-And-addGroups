"""SQLite (data/app.db) -> MySQL ko'chirish. Ma'lumotlar o'chirilmaydi: app.db joyida qoladi va alohida zaxira nusxasi olinadi.

Avtomatik: dastur birinchi marta MySQL bilan ishga tushganda (MySQL bo'sh bo'lsa) o'zi bajaradi.
Qo'lda:   python -m app.migrate            (MySQL bo'sh bo'lsa)
          python -m app.migrate --replace  (MySQL'dagi ma'lumotni o'chirib, qayta ko'chiradi)
"""
import contextlib
import sqlite3
import sys
from datetime import datetime

from . import db, secure
from .config import BACKUP_DIR, DB_PATH, log

# Tashqi kalitlar (FOREIGN KEY) tartibiga mos
ORDER = ["settings", "users", "accounts", "user_settings", "groups", "group_lists", "group_list_items", "group_tags",
         "group_members_log", "blacklist", "templates", "campaigns", "campaign_variants", "variant_ptr", "jobs",
         "job_variants", "job_targets", "join_batches", "join_targets", "account_events", "inbox", "canned_replies",
         "warmup", "autojoin", "disc_candidates", "audit_cfg", "top_groups", "top_meta", "leave_log", "media_items"]

FLAG = "sqlite_migrated"


def _mysql_rowcount(table: str) -> int:
    from . import mysqldb
    return mysqldb.raw(f"SELECT COUNT(*) c FROM `{table}`")[0]["c"]


def mysql_has_data() -> bool:
    # settings ichida faqat bizning ichki belgilar bo'lishi mumkin; asosiy jadvallarni tekshiramiz
    return any(_mysql_rowcount(t) for t in ("accounts", "groups", "jobs", "campaigns", "join_batches", "users"))


def backup_sqlite() -> str:
    BACKUP_DIR.mkdir(exist_ok=True)
    dst = BACKUP_DIR / f"app-pre-mysql-{datetime.now():%Y%m%d-%H%M%S}.db"
    with contextlib.closing(sqlite3.connect(DB_PATH)) as src, contextlib.closing(sqlite3.connect(dst)) as out:
        src.backup(out)
    return str(dst)


def copy_all(src_path, progress=print) -> dict:
    """src_path (SQLite nusxa) dagi barcha jadvallarni MySQL'ga ko'chiradi. {jadval: (manba, nishon)} qaytaradi."""
    from . import mysqldb
    conn = mysqldb.pool._new()
    result = {}
    try:
        cur = conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        with contextlib.closing(sqlite3.connect(src_path)) as s:
            s.row_factory = sqlite3.Row
            have = {r[0] for r in s.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ORDER:
                if t not in have:
                    continue
                scols = [r[1] for r in s.execute(f"PRAGMA table_info({t})")]
                cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (t,))
                mcols = {r["COLUMN_NAME"] for r in cur.fetchall()}
                cols = [c for c in scols if c in mcols]
                total = s.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                if not cols:
                    continue
                sql = f"INSERT IGNORE INTO `{t}`({','.join('`'+c+'`' for c in cols)}) VALUES({','.join(['%s']*len(cols))})"
                sel = s.execute(f"SELECT {','.join(cols)} FROM {t}")
                n = 0
                while True:
                    rows = sel.fetchmany(500)
                    if not rows:
                        break
                    cur.executemany(sql, [tuple(r[c] if not isinstance(r[c], bytes) else r[c].decode("utf-8", "ignore") for c in cols) for r in rows])
                    n += len(rows)
                cur.execute(f"SELECT COUNT(*) c FROM `{t}`")
                got = cur.fetchone()["c"]
                result[t] = (total, got)
                progress(f"  {t}: {total} -> {got}")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
    finally:
        conn.close()
    return result


def _reencrypt_secrets():
    for k in db.SECRET_KEYS:
        for table, where in (("settings", "key=?"), ("user_settings", "key=?")):
            for r in db.q(f"SELECT * FROM {table} WHERE {where}", (k,)):
                v = r["value"]
                if v and not secure.is_encrypted(v):
                    if table == "settings":
                        db.ex("UPDATE settings SET value=? WHERE key=?", (secure.enc(v), k))
                    else:
                        db.ex("UPDATE user_settings SET value=? WHERE user_id=? AND key=?", (secure.enc(v), r["user_id"], k))


def clear_mysql():
    from . import mysqldb
    conn = mysqldb.pool.get()
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for t in reversed(ORDER):
                cur.execute(f"TRUNCATE TABLE `{t}`")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
    finally:
        mysqldb.pool.put(conn)


def migrate(replace: bool = False, progress=print) -> dict:
    if not DB_PATH.exists():
        raise RuntimeError("data/app.db topilmadi, ko'chiriladigan ma'lumot yo'q")
    if mysql_has_data():
        if not replace:
            raise RuntimeError("MySQL bazasida ma'lumot bor. Ustiga yozish uchun: python -m app.migrate --replace")
        clear_mysql()
    snap = backup_sqlite()
    progress(f"SQLite zaxira nusxasi: {snap}")
    res = copy_all(snap, progress)
    bad = {t: v for t, v in res.items() if v[0] != v[1]}
    if bad:
        clear_mysql()
        raise RuntimeError(f"Ko'chirish tekshiruvi muvaffaqiyatsiz (manba/nishon): {bad}. MySQL tozalandi, app.db tegilmagan.")
    _reencrypt_secrets()
    db.ex("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (FLAG, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return res


def auto_migrate():
    """db.init() ichidan chaqiriladi: eski app.db bor va hali ko'chirilmagan bo'lsa, o'zi ko'chiradi."""
    if not DB_PATH.exists() or db.one("SELECT 1 FROM settings WHERE key=?", (FLAG,)):
        return
    if mysql_has_data():
        log.warning("app.db bor, lekin MySQL'da ham ma'lumot bor: avtomatik ko'chirish o'tkazib yuborildi")
        db.ex("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (FLAG, "skipped"))
        return
    log.info("SQLite -> MySQL avtomatik ko'chirish boshlandi")
    res = migrate(False, progress=lambda m: log.info(m))
    log.info("SQLite -> MySQL ko'chirish tugadi: %s", {t: v[1] for t, v in res.items()})


if __name__ == "__main__":
    from . import db as _db
    _db._init_mysql()
    try:
        out = migrate(replace="--replace" in sys.argv)
        print("Tayyor. Qatorlar:", {t: v[1] for t, v in out.items()})
    except Exception as e:
        print("XATO:", e)
        sys.exit(1)
