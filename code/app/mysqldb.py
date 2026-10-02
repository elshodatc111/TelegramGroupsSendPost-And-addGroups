"""MySQL / MariaDB (XAMPP) backend: ulanish, SQL tarjimoni, sxema.

Dastur kodi SQLite sintaksisida yozilgan (`?`, INSERT OR IGNORE, ON CONFLICT ...). Bu modul shu so'rovlarni
MySQL'ga avtomatik tarjima qiladi, shuning uchun Telegram Guruhlar bo'limining kodini qayta yozish shart emas.
"""
import json
import queue
import re
import threading
from decimal import Decimal

import pymysql
from pymysql.constants import FIELD_TYPE
from pymysql.converters import conversions

from . import secure
from .config import DBCONF_PATH, log
from .xampp import DbUnavailable, ensure_running

# ------------------------------------------------------------------ sozlama
DEFAULT_CONF = {"host": "127.0.0.1", "port": 3306, "user": "root", "password": "", "name": "telegram_group_post",
                "xampp_dir": "", "root_password": ""}


def load_conf() -> dict:
    conf = dict(DEFAULT_CONF)
    if DBCONF_PATH.exists():
        try:
            raw = json.loads(DBCONF_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        conf.update({k: raw[k] for k in raw if k in DEFAULT_CONF})
        for k in ("password", "root_password"):
            v = conf.get(k)
            if secure.is_encrypted(v):
                conf[k] = secure.dec(v)
                if conf[k] is None:
                    conf[k] = ""
    return conf


def save_conf(conf: dict):
    out = {k: conf.get(k, DEFAULT_CONF[k]) for k in DEFAULT_CONF}
    for k in ("password", "root_password"):
        out[k] = secure.enc(out[k]) if out[k] else ""
    DBCONF_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ tarjimon
_LIT = re.compile(r"('(?:[^']|'')*')")
_DT = re.compile(r"\b(date|datetime)\(\s*'now'\s*(?:,\s*'localtime'\s*)?(?:,\s*'([+-]?\d+)\s*(day|hour|minute|month|year)s?'\s*)?\)", re.I)
QUOTE = ("key", "groups")          # MySQL'da zahiralangan so'zlar, SQLite kodida oddiy nom sifatida ishlatilgan


def _dt(m):
    kind, n, unit = m.group(1).lower(), m.group(2), m.group(3)
    base = "NOW()"
    if n is not None:
        base = f"DATE_ADD(NOW(), INTERVAL {int(n)} {unit.upper()})"
    fmt = "%Y-%m-%d" if kind == "date" else "%Y-%m-%d %H:%i:%S"
    return f"DATE_FORMAT({base}, '{fmt}')"


_cache: dict[str, str] = {}


def translate(sql: str) -> str:
    """SQLite -> MySQL. Natija keshlanadi."""
    hit = _cache.get(sql)
    if hit is not None:
        return hit
    s = _DT.sub(_dt, sql)
    parts = _LIT.split(s)
    for i, p in enumerate(parts):
        if i % 2 == 1:                            # matn literali: faqat % ni qochirish
            parts[i] = p.replace("%", "%%")
            continue
        p = p.replace("%", "%%").replace("?", "%s")
        p = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT IGNORE INTO", p, flags=re.I)
        p = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", "REPLACE INTO", p, flags=re.I)
        p = re.sub(r"\s+COLLATE\s+NOCASE\b", "", p, flags=re.I)
        for w in QUOTE:
            p = re.sub(rf"(?<![\w`.]){w}(?![\w`(])", f"`{w}`", p, flags=re.I)
        p = re.sub(r"ON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET", "ON DUPLICATE KEY UPDATE", p, flags=re.I)
        p = re.sub(r"\bexcluded\.(\w+)", r"VALUES(\1)", p, flags=re.I)
        parts[i] = p
    out = "".join(parts)
    if len(_cache) < 4000:
        _cache[sql] = out
    return out


# ------------------------------------------------------------------ satr va tur moslashtirish
class Row(dict):
    """sqlite3.Row ga o'xshash: r["nom"] va r[0] ikkalasi ishlaydi."""
    __slots__ = ()

    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return dict.__getitem__(self, k)


def _decimal(s):
    if s is None:
        return None
    return float(s) if "." in s else int(s)


_CONV = dict(conversions)
_CONV[FIELD_TYPE.NEWDECIMAL] = _decimal
_CONV[FIELD_TYPE.DECIMAL] = _decimal
_CONV[Decimal] = lambda v, m=None: str(v)


class DictRowCursor(pymysql.cursors.DictCursor):
    dict_type = Row


# ------------------------------------------------------------------ ulanishlar
class Pool:
    def __init__(self):
        self.q: queue.LifoQueue = queue.LifoQueue()
        self.lock = threading.Lock()
        self.conf: dict | None = None
        self.size = 10
        self.created = 0

    def configure(self, conf: dict):
        self.conf = conf

    def _new(self, database=True):
        c = self.conf
        conn = pymysql.connect(host=c["host"], port=int(c["port"]), user=c["user"], password=c["password"] or "",
                               database=c["name"] if database else None, charset="utf8mb4", autocommit=True,
                               cursorclass=DictRowCursor, conv=_CONV, connect_timeout=8, read_timeout=120,
                               write_timeout=120)
        with conn.cursor() as cur:
            # ONLY_FULL_GROUP_BY va qat'iy rejim SQLite bilan yozilgan so'rovlarga xalaqit bermasligi uchun
            cur.execute("SET SESSION sql_mode='NO_ENGINE_SUBSTITUTION'")
        return conn

    def get(self):
        try:
            conn = self.q.get_nowait()
        except queue.Empty:
            return self._new()
        try:
            conn.ping(reconnect=True)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conn = self._new()
        return conn

    def put(self, conn):
        if self.q.qsize() >= self.size:
            try:
                conn.close()
            except Exception:
                pass
        else:
            self.q.put(conn)

    def close_all(self):
        while True:
            try:
                self.q.get_nowait().close()
            except queue.Empty:
                break
            except Exception:
                pass


pool = Pool()


def connect_server() -> dict:
    """XAMPP MySQL ishlayotganini ta'minlaydi, bazani (va foydalanuvchini) yaratadi. Ulanish sozlamasini qaytaradi."""
    conf = load_conf()
    conf["port"] = ensure_running(conf["host"], int(conf["port"]), conf.get("xampp_dir") or None)
    first = not DBCONF_PATH.exists()
    if first:
        _bootstrap(conf)
    pool.configure(conf)
    try:
        pool.put(pool._new())
    except pymysql.err.OperationalError as e:
        if e.args and e.args[0] == 1049:          # baza yo'q
            _create_db(conf)
            pool.put(pool._new())
        else:
            raise DbUnavailable(f"MySQL'ga ulanib bo'lmadi: {e}")
    return conf


def _root_conn(conf):
    try:
        return pymysql.connect(host=conf["host"], port=int(conf["port"]), user="root",
                               password=conf.get("root_password") or "", charset="utf8mb4", autocommit=True,
                               connect_timeout=8)
    except pymysql.err.OperationalError as e:
        raise DbUnavailable(
            f"MySQL'ga root sifatida ulanib bo'lmadi ({e.args[-1] if e.args else e}). Agar root parol qo'ygan bo'lsangiz, "
            "data\\dbconf.json faylida \"root_password\" maydoniga yozing.")


def _create_db(conf):
    with _root_conn(conf) as c, c.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{conf['name']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")


def _bootstrap(conf):
    """Birinchi ishga tushish: baza va alohida foydalanuvchi (tgpost) yaratiladi, sozlama saqlanadi."""
    import secrets
    _create_db(conf)
    pw = secrets.token_urlsafe(18)
    try:
        with _root_conn(conf) as c, c.cursor() as cur:
            for host in ("localhost", "127.0.0.1"):
                cur.execute(f"CREATE USER IF NOT EXISTS 'tgpost'@'{host}' IDENTIFIED BY %s", (pw,))
                cur.execute(f"ALTER USER 'tgpost'@'{host}' IDENTIFIED BY %s", (pw,))
                cur.execute(f"GRANT ALL PRIVILEGES ON `{conf['name']}`.* TO 'tgpost'@'{host}'")
            try:
                cur.execute("FLUSH PRIVILEGES")      # CREATE USER/GRANT o'zi kuchga kiradi, flush shart emas
            except pymysql.err.Error:
                log.warning("FLUSH PRIVILEGES bajarilmadi (mysql tizim jadvali shikastlangan bo'lishi mumkin)")
    except pymysql.err.Error as e:
        if "Aria" in str(e) or "checksum" in str(e):
            raise DbUnavailable(
                "XAMPP MySQL tizim jadvali (mysql papkasi, Aria) shikastlangan: kompyuter/MySQL noto'g'ri o'chirilgan. "
                "Tuzatish: XAMPP'da MySQL ni Stop qiling, cmd ochib "
                "'cd C:\\xampp\\mysql\\bin' va "
                "'for %f in (..\\data\\mysql\\*.MAI) do aria_chk -r \"%f\"' ni bajaring, so'ng run.bat ni qayta ishga tushiring.")
        raise DbUnavailable(f"MySQL'da foydalanuvchi yaratib bo'lmadi: {e}")
    conf["user"], conf["password"] = "tgpost", pw
    save_conf(conf)
    log.info("MySQL: baza '%s' va foydalanuvchi 'tgpost' yaratildi", conf["name"])


def run(sql: str, args=(), many=False):
    """Bitta so'rovni bajaradi. SELECT bo'lsa Row ro'yxati, aks holda lastrowid."""
    conn = pool.get()
    try:
        with conn.cursor() as cur:
            t = translate(sql)
            if many:
                cur.executemany(t, list(args))
                return None
            cur.execute(t, tuple(args))
            return cur.fetchall() if cur.description else cur.lastrowid
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        conn = None
        raise
    finally:
        if conn is not None:
            pool.put(conn)


def raw(sql: str, args=None):
    """Tarjimasiz so'rov (DDL va boshqa MySQL-ga xos buyruqlar uchun)."""
    conn = pool.get()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall() if cur.description else cur.lastrowid
    finally:
        pool.put(conn)


# ------------------------------------------------------------------ sxema
T = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
TS = "VARCHAR(32)"          # sana/vaqt matn sifatida saqlanadi ("YYYY-MM-DD HH:MM:SS") - SQLite bilan bir xil

SCHEMA_CORE = [
    f"CREATE TABLE IF NOT EXISTS settings(`key` VARCHAR(120) PRIMARY KEY, value LONGTEXT) {T}",
    f"""CREATE TABLE IF NOT EXISTS accounts(
        id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(255), phone VARCHAR(40), username VARCHAR(120), session VARCHAR(120),
        created_at {TS}, daily_limit INT DEFAULT 150, work_start VARCHAR(10) DEFAULT '', work_end VARCHAR(10) DEFAULT '',
        user_id INT, workspace VARCHAR(16) NOT NULL DEFAULT 'posting') {T}""",
    f"""CREATE TABLE IF NOT EXISTS `groups`(
        account_id INT NOT NULL DEFAULT 1, tg_id BIGINT NOT NULL, title VARCHAR(600), username VARCHAR(120), kind VARCHAR(20),
        members INT, can_post INT DEFAULT 1, synced_at {TS}, about TEXT, slowmode INT DEFAULT 0, no_media INT DEFAULT 0,
        no_links INT DEFAULT 0, ads_flag INT DEFAULT 0, ads_ok INT DEFAULT 0, checked_at {TS}, muted INT DEFAULT 0,
        verdict VARCHAR(20), verdict_reason VARCHAR(255), audited_at {TS}, PRIMARY KEY(account_id, tg_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS group_lists(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL DEFAULT 1, name VARCHAR(190), UNIQUE KEY u_gl(account_id, name)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS group_list_items(
        list_id INT NOT NULL, tg_id BIGINT NOT NULL, PRIMARY KEY(list_id, tg_id),
        FOREIGN KEY(list_id) REFERENCES group_lists(id) ON DELETE CASCADE) {T}""",
    f"""CREATE TABLE IF NOT EXISTS group_tags(
        account_id INT NOT NULL, tg_id BIGINT NOT NULL, tag VARCHAR(120) NOT NULL, PRIMARY KEY(account_id, tg_id, tag)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS group_members_log(
        account_id INT NOT NULL, tg_id BIGINT NOT NULL, day VARCHAR(10) NOT NULL, members INT,
        PRIMARY KEY(account_id, tg_id, day)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS blacklist(
        account_id INT NOT NULL, tg_id BIGINT NOT NULL, title VARCHAR(600), reason TEXT, created_at {TS},
        PRIMARY KEY(account_id, tg_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS templates(
        id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(255), text LONGTEXT, parse_mode VARCHAR(10) DEFAULT 'none',
        media_path VARCHAR(600), media_type VARCHAR(20), created_at {TS}) {T}""",
    f"""CREATE TABLE IF NOT EXISTS campaigns(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL DEFAULT 1, name VARCHAR(190), created_at {TS},
        min_delay INT, max_delay INT, utm_on INT DEFAULT 0, utm_source VARCHAR(255), utm_campaign VARCHAR(255),
        skip_ads INT DEFAULT 1, target_json LONGTEXT DEFAULT ('[]'), recurrence VARCHAR(20) DEFAULT 'none',
        rec_time VARCHAR(10) DEFAULT '10:00', rec_days VARCHAR(40) DEFAULT '', rec_active INT DEFAULT 0,
        next_run {TS}, last_run {TS}, UNIQUE KEY u_camp(account_id, name)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS campaign_variants(
        id INT AUTO_INCREMENT PRIMARY KEY, campaign_id INT NOT NULL, idx INT, text LONGTEXT,
        parse_mode VARCHAR(10) DEFAULT 'none', media_json LONGTEXT DEFAULT ('[]'), media_type VARCHAR(20),
        FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE) {T}""",
    f"CREATE TABLE IF NOT EXISTS variant_ptr(`key` VARCHAR(120) PRIMARY KEY, ptr INT DEFAULT 0) {T}",
    f"""CREATE TABLE IF NOT EXISTS jobs(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL DEFAULT 1, campaign_id INT, text LONGTEXT,
        parse_mode VARCHAR(10) DEFAULT 'none', media_path VARCHAR(600), media_type VARCHAR(20), status VARCHAR(20) DEFAULT 'draft',
        min_delay INT, max_delay INT, scheduled_at {TS}, created_at {TS}, started_at {TS}, finished_at {TS}, next_at {TS},
        resume_at {TS}, error TEXT, utm_on INT DEFAULT 0, utm_source VARCHAR(255), utm_campaign VARCHAR(255),
        name VARCHAR(255), total INT DEFAULT 0, done INT DEFAULT 0, failed INT DEFAULT 0, extra_json LONGTEXT,
        INDEX idx_jobs_acc(account_id, status)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS job_variants(
        id INT AUTO_INCREMENT PRIMARY KEY, job_id INT NOT NULL, idx INT, text LONGTEXT, parse_mode VARCHAR(10) DEFAULT 'none',
        media_json LONGTEXT DEFAULT ('[]'), media_type VARCHAR(20),
        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE) {T}""",
    f"""CREATE TABLE IF NOT EXISTS job_targets(
        id INT AUTO_INCREMENT PRIMARY KEY, job_id INT NOT NULL, tg_id BIGINT, title VARCHAR(600),
        status VARCHAR(20) DEFAULT 'pending', error TEXT, sent_at {TS}, variant_id INT, msg_ids TEXT, views INT,
        forwards INT, reactions INT, replies INT, deleted INT DEFAULT 0, stat_at {TS},
        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE, INDEX idx_targets_job(job_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS join_batches(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL DEFAULT 1, filename VARCHAR(255),
        status VARCHAR(20) DEFAULT 'draft', min_delay INT, max_delay INT, daily_limit INT, created_at {TS},
        started_at {TS}, finished_at {TS}, next_at {TS}, resume_at {TS}, error TEXT, total INT DEFAULT 0, raw LONGTEXT) {T}""",
    f"""CREATE TABLE IF NOT EXISTS join_targets(
        id INT AUTO_INCREMENT PRIMARY KEY, batch_id INT NOT NULL, ref VARCHAR(600), kind VARCHAR(20), `key` VARCHAR(255),
        status VARCHAR(20) DEFAULT 'pending', detail TEXT, title VARCHAR(600), tried_at {TS},
        FOREIGN KEY(batch_id) REFERENCES join_batches(id) ON DELETE CASCADE, INDEX idx_jt_batch(batch_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS account_events(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, kind VARCHAR(50), ts {TS}, info TEXT,
        INDEX idx_events(account_id, kind, ts)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS inbox(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, chat_id BIGINT, chat_title VARCHAR(600),
        sender_id BIGINT, sender_name VARCHAR(400), sender_username VARCHAR(120), text TEXT, msg_id BIGINT,
        is_private INT DEFAULT 0, kind VARCHAR(20), date {TS}, is_read INT DEFAULT 0, replied INT DEFAULT 0,
        UNIQUE KEY u_inbox(account_id, chat_id, msg_id)) {T}""",
    f"CREATE TABLE IF NOT EXISTS canned_replies(id INT AUTO_INCREMENT PRIMARY KEY, title VARCHAR(255), text TEXT, user_id INT) {T}",
    f"""CREATE TABLE IF NOT EXISTS users(
        id INT AUTO_INCREMENT PRIMARY KEY, email VARCHAR(190) UNIQUE, name VARCHAR(255), picture VARCHAR(600),
        role VARCHAR(20) DEFAULT 'user', status VARCHAR(20) DEFAULT 'active', created_at {TS}, last_login {TS}) {T}""",
    f"""CREATE TABLE IF NOT EXISTS user_settings(
        user_id INT NOT NULL, `key` VARCHAR(120) NOT NULL, value LONGTEXT, PRIMARY KEY(user_id, `key`)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS warmup(
        account_id INT PRIMARY KEY, active INT DEFAULT 0, start_date VARCHAR(10), plan_json LONGTEXT) {T}""",
    f"""CREATE TABLE IF NOT EXISTS autojoin(
        account_id INT PRIMARY KEY, active INT DEFAULT 0, daily_target INT DEFAULT 20, min_members INT DEFAULT 500,
        min_per_day INT DEFAULT 10, min_uz INT DEFAULT 40, ad_wait_days INT DEFAULT 2, min_delay INT DEFAULT 120,
        max_delay INT DEFAULT 300, manual_all INT DEFAULT 0, keywords_json LONGTEXT, block_extra LONGTEXT,
        last_search {TS}, kw_ptr INT DEFAULT 0, ban_json LONGTEXT) {T}""",
    f"""CREATE TABLE IF NOT EXISTS disc_candidates(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, username VARCHAR(190) NOT NULL, title VARCHAR(600),
        about TEXT, members INT, per_day DOUBLE, uz INT, category VARCHAR(120), keyword VARCHAR(255), score DOUBLE,
        status VARCHAR(20), reason VARCHAR(512), batch_id INT, found_at {TS}, joined_at {TS}, ad_at {TS}, tg_id BIGINT,
        UNIQUE KEY u_disc(account_id, username)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS audit_cfg(
        account_id INT PRIMARY KEY, auto INT DEFAULT 1, min_members INT DEFAULT 100, check_ads INT DEFAULT 1,
        check_post INT DEFAULT 1, mute_all INT DEFAULT 1, max_leave INT DEFAULT 30, min_delay INT DEFAULT 25,
        max_delay INT DEFAULT 60, last_scan {TS}) {T}""",
    f"""CREATE TABLE IF NOT EXISTS top_groups(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, username VARCHAR(190) NOT NULL, title VARCHAR(600),
        about TEXT, members INT, per_day DOUBLE, uz INT, category VARCHAR(120), status VARCHAR(20), reason VARCHAR(512),
        batch_id INT, found_at {TS}, UNIQUE KEY u_top(account_id, username)) {T}""",
    f"CREATE TABLE IF NOT EXISTS top_meta(account_id INT PRIMARY KEY, ptr_json LONGTEXT, last_run {TS}, runs INT DEFAULT 0) {T}",
    f"""CREATE TABLE IF NOT EXISTS leave_log(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, tg_id BIGINT, title VARCHAR(600), reason VARCHAR(512),
        auto INT DEFAULT 0, ts {TS}) {T}""",
    f"""CREATE TABLE IF NOT EXISTS media_items(
        name VARCHAR(190) PRIMARY KEY, original VARCHAR(600), kind VARCHAR(20), size BIGINT, created_at {TS}, user_id INT) {T}""",
]

# sxemaga keyin qo'shilgan ustunlar (eski MySQL bazalarini yangilash uchun)
EXTRA_COLUMNS = [("accounts", "workspace", "VARCHAR(16) NOT NULL DEFAULT 'posting'")]


def ensure_columns(extra):
    for table, col, ddl in extra:
        have = {r["COLUMN_NAME"] for r in raw(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))}
        if col not in have:
            raw(f"ALTER TABLE `{table}` ADD COLUMN {col} {ddl}")


def create_schema(extra_ddl=()):
    for ddl in list(SCHEMA_CORE) + list(extra_ddl):
        raw(ddl)
    ensure_columns(EXTRA_COLUMNS)
