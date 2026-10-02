"""Zoom Online (Zoom darslari): sxema, sozlamalar, o'qituvchi/guruh/jadval, Zoom akkauntlar puli,
40 daqiqalik majlis zanjiri, Telegram xabarlari, ogohlantirishlar va fon sikli.

Bu modul Telegram Guruhlar / Telegram SMM jadvallariga tegmaydi: faqat zoom_* jadvallari va workspace='meet' akkaunti.
Vaqt: hamma joyda Asia/Tashkent (bazada oddiy "YYYY-MM-DD HH:MM:SS" matn, Toshkent vaqti).

Ish tartibi (qisqacha):
  jadval -> dars (zoom_lessons) -> dars boshlanishidan ~30 daq oldin bo'sh Zoom akkauntdan 1-majlis (zoom_meetings, seq=1)
  -> o'qituvchiga bot orqali (20 va 10 daq oldin eslatma + «Darsni boshlash» havolasi), guruhga 5 daq oldin havola
  -> majlis tugashiga 3 daq qolganda (dars hali tugamagan bo'lsa) o'qituvchiga bir marta «Davom etish / Bekor qilish»
  -> «Davom etish» bosilsa boshqa bo'sh akkauntdan yangi majlis yaratilib, havola o'qituvchiga va guruhga yuboriladi; sikl takrorlanadi
  -> «Bekor qilish» yoki javob bo'lmasa jarayon to'xtaydi.
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from . import db, meet_zoom as Z, notify, schema_ch
from .config import log

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Tashkent")
except Exception:                                   # Windows'da tzdata yo'q bo'lsa: Toshkent doim UTC+5
    TZ = timezone(timedelta(hours=5))

WEEKDAYS = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
WD_SHORT = ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"]
STATUS_UZ = {"planned": "Rejalashtirilgan", "live": "Davom etmoqda", "done": "O'tdi", "missed": "O'tmadi",
             "cancelled": "Bekor qilindi"}
BUSY_AFTER_MIN = 4        # majlis tugagandan keyin akkaunt yana shuncha daqiqa band hisoblanadi (Zoom 40 daq hisobi kechikishi)
BUSY_BEFORE_MIN = 2


# ---------------------------------------------------------------- vaqt
def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse(s: str) -> datetime:
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- sxema (zoom_*)
T = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
TABLES = [
    f"""CREATE TABLE IF NOT EXISTS zoom_accounts(
        id INT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(120), email VARCHAR(190), account_id VARCHAR(120), client_id VARCHAR(120),
        client_secret TEXT, max_min INT DEFAULT 40, enabled INT DEFAULT 1, status VARCHAR(12) DEFAULT 'new', last_err TEXT,
        last_ok VARCHAR(19), last_used VARCHAR(19), cool_until VARCHAR(19), plan VARCHAR(40), note TEXT, created_at VARCHAR(32),
        UNIQUE KEY u_za(email)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS zoom_teachers(
        id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(255), tg_username VARCHAR(64), phone VARCHAR(40), note TEXT,
        chat_id BIGINT, bot_linked_at VARCHAR(19), active INT DEFAULT 1, created_at VARCHAR(32)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS zoom_groups(
        id INT AUTO_INCREMENT PRIMARY KEY, tg_id BIGINT, title VARCHAR(500), username VARCHAR(120), teacher_id INT,
        active INT DEFAULT 1, note TEXT, created_at VARCHAR(32), INDEX idx_zg(active)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS zoom_schedule(
        id INT AUTO_INCREMENT PRIMARY KEY, group_id INT NOT NULL, weekday INT, start_time VARCHAR(5), duration_min INT,
        teacher_id INT, active INT DEFAULT 1, created_at VARCHAR(32), INDEX idx_zs(group_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS zoom_lessons(
        id INT AUTO_INCREMENT PRIMARY KEY, group_id INT NOT NULL, schedule_id INT, slot_day VARCHAR(10), teacher_id INT,
        start_at VARCHAR(19), end_at VARCHAR(19), duration_min INT, status VARCHAR(16) DEFAULT 'planned', flags LONGTEXT,
        stopped INT DEFAULT 0, note TEXT, cancel_reason TEXT, moved_from VARCHAR(19), prov_err TEXT, prov_at VARCHAR(19),
        created_at VARCHAR(32), UNIQUE KEY u_zl(schedule_id, slot_day), INDEX idx_zl_s(start_at), INDEX idx_zl_g(group_id, start_at)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS zoom_meetings(
        id INT AUTO_INCREMENT PRIMARY KEY, lesson_id INT, account_id INT, seq INT DEFAULT 1, start_at VARCHAR(19), end_at VARCHAR(19),
        zoom_id VARCHAR(40), join_url VARCHAR(700), start_url TEXT, passcode VARCHAR(40), topic VARCHAR(255),
        state VARCHAR(10) DEFAULT 'active', prompt VARCHAR(10) DEFAULT 'none', flags LONGTEXT, adhoc INT DEFAULT 0,
        created_by VARCHAR(12), created_at VARCHAR(19), INDEX idx_zm_a(account_id, start_at), INDEX idx_zm_l(lesson_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS zoom_settings(`key` VARCHAR(80) PRIMARY KEY, value LONGTEXT) {T}""",
    f"""CREATE TABLE IF NOT EXISTS zoom_alerts(
        id INT AUTO_INCREMENT PRIMARY KEY, ts VARCHAR(19), kind VARCHAR(24), level VARCHAR(8) DEFAULT 'warn', lesson_id INT,
        group_id INT, text TEXT, copy_text TEXT, seen INT DEFAULT 0, dedupe VARCHAR(160),
        UNIQUE KEY u_zal(dedupe), INDEX idx_zal(seen, ts)) {T}""",
]
TABLE_NAMES = ["zoom_accounts", "zoom_teachers", "zoom_groups", "zoom_schedule", "zoom_lessons", "zoom_meetings", "zoom_settings", "zoom_alerts"]
OLD_TABLES = ["mt_teachers", "mt_groups", "mt_schedule", "mt_lessons", "mt_attendance", "mt_settings", "mt_alerts"]   # eski Google Meet bo'limi
_ready = False


def ensure_schema():
    """Zoom Online jadvallarini yaratadi (mavjud bo'lsa tegmaydi) va eski Google Meet (mt_*) jadvallarini olib tashlaydi."""
    global _ready
    if db.IS_MYSQL:
        from . import mysqldb
        for t in OLD_TABLES:
            mysqldb.raw(f"DROP TABLE IF EXISTS {t}")
        for st in TABLES:
            mysqldb.raw(st)
    else:
        for t in OLD_TABLES:
            db.ex(f"DROP TABLE IF EXISTS {t}")
        for st in TABLES:
            create, idx = schema_ch._to_sqlite(st)
            db.ex(create)
            for i in idx:
                db.ex(i)
    _ready = True


def ready():
    if not _ready:
        ensure_schema()


# ---------------------------------------------------------------- sozlamalar
DEFAULTS = {
    "remind_a": 60, "remind_b": 10,            # guruhga eslatmalar (daqiqa oldin)
    "group_link_min": 5,                       # guruhga havola (daqiqa oldin)
    "t_remind_a": 20, "t_remind_b": 10,        # o'qituvchiga bot eslatmalari; ikkinchisida boshlash havolasi ham boradi
    "create_lead_min": 30,                     # majlisni dars boshlanishidan necha daqiqa oldin yaratish
    "prompt_before_min": 3,                    # majlis tugashiga necha daqiqa qolganda «Davom etasizmi?»
    "min_remaining": 2,                        # dars tugashiga shundan kam qolsa «Davom etasizmi?» yuborilmaydi
    "seg_default_min": 40,                     # yangi akkaunt uchun standart majlis davomiyligi (bepul: 40, Pro: 0=cheksiz)
    "horizon_days": 7,
    "remind_on": 1,
    "admin_username": "",
    "bot_token": "",
}
SECRET_KEYS = {"bot_token"}


def get(key, default=None):
    from . import secure
    r = db.one("SELECT value FROM zoom_settings WHERE key=?", (key,))
    if not r or r["value"] is None:
        return default
    v = r["value"]
    if key in SECRET_KEYS and secure.is_encrypted(v):
        d = secure.dec(v)
        return default if d is None else d
    return v


def put(key, value):
    from . import secure
    value = "" if value is None else str(value)
    if key in SECRET_KEYS and value:
        value = secure.enc(value)
    db.ex("INSERT INTO zoom_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def drop(key):
    db.ex("DELETE FROM zoom_settings WHERE key=?", (key,))


def cfg(key):
    d = DEFAULTS[key]
    v = get(key)
    if v is None or v == "":
        return d
    if isinstance(d, int):
        try:
            return int(float(v))
        except ValueError:
            return d
    return v


def jget(key, default):
    try:
        v = json.loads(get(key) or "")
        return v if v is not None else default
    except Exception:
        return default


def jput(key, value):
    put(key, json.dumps(value, ensure_ascii=False))


# ---------------------------------------------------------------- o'qituvchilar
_UN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def teachers(active_only=False):
    sql = "SELECT * FROM zoom_teachers" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
    return [dict(r) for r in db.q(sql)]


def teacher(tid):
    r = db.one("SELECT * FROM zoom_teachers WHERE id=?", (tid,)) if tid else None
    return dict(r) if r else None


def teacher_by_username(un: str):
    un = (un or "").lstrip("@").lower()
    if not un:
        return None
    for t in teachers():
        if (t.get("tg_username") or "").lower() == un:
            return t
    return None


def save_teacher(form: dict, tid=None) -> str | None:
    """Xato matnini qaytaradi (yoki None — saqlandi)."""
    name = (form.get("name") or "").strip()
    un = (form.get("tg_username") or "").strip().lstrip("@")
    if not name:
        return "Ism kiritilmagan"
    if not un:
        return "Telegram @username kiritilishi shart (bot o'qituvchini shu orqali taniydi)"
    if not _UN.match(un):
        return "Telegram @username noto'g'ri (5-32 ta lotin harf, raqam yoki _)"
    other = teacher_by_username(un)
    if other and other["id"] != tid:
        return f"Bu @username boshqa o'qituvchida ({other['name']}) bor"
    vals = (name, un, (form.get("phone") or "").strip(), (form.get("note") or "").strip())
    if tid:
        old = teacher(tid)
        db.ex("UPDATE zoom_teachers SET name=?, tg_username=?, phone=?, note=? WHERE id=?", vals + (tid,))
        if old and (old.get("tg_username") or "").lower() != un.lower():                 # @username almashsa bot qayta bog'lanadi
            db.ex("UPDATE zoom_teachers SET chat_id=NULL, bot_linked_at=NULL WHERE id=?", (tid,))
    else:
        db.ex("INSERT INTO zoom_teachers(name,tg_username,phone,note,active,created_at) VALUES(?,?,?,?,1,?)", vals + (fmt(now()),))
    return None


def delete_teacher(tid):
    db.ex("UPDATE zoom_groups SET teacher_id=NULL WHERE teacher_id=?", (tid,))
    db.ex("UPDATE zoom_schedule SET teacher_id=NULL WHERE teacher_id=?", (tid,))
    db.ex("UPDATE zoom_lessons SET teacher_id=NULL WHERE teacher_id=? AND status IN ('planned','live')", (tid,))
    db.ex("DELETE FROM zoom_teachers WHERE id=?", (tid,))


# ---------------------------------------------------------------- guruhlar
def groups(active_only=False):
    sql = ("SELECT g.*, t.name teacher_name FROM zoom_groups g LEFT JOIN zoom_teachers t ON t.id=g.teacher_id"
           + (" WHERE g.active=1" if active_only else "") + " ORDER BY g.title")
    return [dict(r) for r in db.q(sql)]


def group(gid):
    r = db.one("SELECT g.*, t.name teacher_name FROM zoom_groups g LEFT JOIN zoom_teachers t ON t.id=g.teacher_id WHERE g.id=?", (gid,)) if gid else None
    return dict(r) if r else None


def add_group(tg_id, title, username="", teacher_id=None) -> int:
    old = db.one("SELECT id FROM zoom_groups WHERE tg_id=?", (tg_id,)) if tg_id else None
    if old:
        return old["id"]
    return db.ex("INSERT INTO zoom_groups(tg_id,title,username,teacher_id,active,created_at) VALUES(?,?,?,?,1,?)",
                 (tg_id, title, username or "", teacher_id, fmt(now())))


async def delete_group(gid):
    """Guruh, jadvali va darslari o'chiriladi; band majlislar Zoom'dan ham o'chiriladi."""
    for r in db.q("SELECT id FROM zoom_lessons WHERE group_id=?", (gid,)):
        await _drop_meetings(r["id"])
        db.ex("DELETE FROM zoom_meetings WHERE lesson_id=?", (r["id"],))
    db.ex("DELETE FROM zoom_lessons WHERE group_id=?", (gid,))
    db.ex("DELETE FROM zoom_schedule WHERE group_id=?", (gid,))
    db.ex("DELETE FROM zoom_alerts WHERE group_id=?", (gid,))
    db.ex("DELETE FROM zoom_groups WHERE id=?", (gid,))


# ---------------------------------------------------------------- jadval
def schedules(gid):
    return [dict(r) for r in db.q("SELECT s.*, t.name teacher_name FROM zoom_schedule s LEFT JOIN zoom_teachers t ON t.id=s.teacher_id "
                                  "WHERE s.group_id=? ORDER BY s.weekday, s.start_time", (gid,))]


def add_schedule(gid, weekdays, start_time, duration, teacher_id=None) -> tuple[list[str], list[str]]:
    """(xatolar, ogohlantirishlar). Xato bo'lsa hech narsa qo'shilmaydi."""
    errs, warns = [], []
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", start_time or ""):
        errs.append("Vaqt noto'g'ri (SS:DD, masalan 19:00)")
    try:
        duration = int(duration)
        if not 10 <= duration <= 480:
            errs.append("Davomiylik 10 dan 480 daqiqagacha bo'lishi kerak")
    except (TypeError, ValueError):
        errs.append("Davomiylik raqam bo'lishi kerak")
    wds = sorted({int(w) for w in weekdays if str(w).isdigit() and 0 <= int(w) <= 6})
    if not wds:
        errs.append("Hafta kunlarini belgilang")
    if errs:
        return errs, warns
    for w in wds:
        if not db.one("SELECT 1 FROM zoom_schedule WHERE group_id=? AND weekday=? AND start_time=?", (gid, w, start_time)):
            db.ex("INSERT INTO zoom_schedule(group_id,weekday,start_time,duration_min,teacher_id,active,created_at) VALUES(?,?,?,?,?,1,?)",
                  (gid, w, start_time, duration, teacher_id, fmt(now())))
    n = db.one("SELECT COUNT(*) c FROM zoom_schedule WHERE group_id=? AND active=1", (gid,))["c"]
    if not 3 <= n <= 6:
        warns.append(f"Haftasiga {n} ta dars bor (odatda 3-6 ta bo'ladi). Agar shunday kerak bo'lsa, davom eting.")
    w = duration_note(duration)
    if w:
        warns.append(w)
    return [], warns


def duration_note(minutes: int) -> str:
    """Bepul akkauntlarda dars necha bo'lakka bo'linishini aytadi (ma'lumot uchun)."""
    free = [a for a in accounts(True) if a["max_min"]]
    if not free or minutes <= min(a["max_min"] for a in free):
        return ""
    m = min(a["max_min"] for a in free)
    n = -(-minutes // m)
    return f"Dars {minutes} daqiqa: bepul Zoom akkauntlarda {m} daqiqalik {n} ta majlisga bo'linadi (har bo'lak oxirida o'qituvchidan «Davom etasizmi?» so'raladi)."


# ---------------------------------------------------------------- Zoom akkauntlar puli
def _acc_public(r) -> dict:
    d = dict(r)
    d["has_secret"] = bool(d.get("client_secret"))
    d.pop("client_secret", None)
    return d


def accounts(enabled_only=False) -> list[dict]:
    sql = "SELECT * FROM zoom_accounts" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
    return [_acc_public(r) for r in db.q(sql)]


def account_raw(aid) -> dict | None:
    r = db.one("SELECT * FROM zoom_accounts WHERE id=?", (aid,)) if aid else None
    return dict(r) if r else None


_MAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def add_account(label, email, account_id, client_id, client_secret, max_min=None) -> tuple[int | None, str | None]:
    from . import secure
    email = (email or "").strip().lower()
    account_id, client_id, client_secret = (account_id or "").strip(), (client_id or "").strip(), (client_secret or "").strip()
    if not _MAIL.match(email):
        return None, f"Zoom email noto'g'ri: {email or '—'}"
    if not (account_id and client_id and client_secret):
        return None, f"{email}: Account ID, Client ID va Client Secret to'liq kiritilishi kerak"
    if db.one("SELECT 1 FROM zoom_accounts WHERE LOWER(email)=?", (email,)):
        return None, f"{email}: bu akkaunt allaqachon qo'shilgan"
    try:
        mm = int(max_min) if str(max_min or "").strip() != "" else cfg("seg_default_min")
    except ValueError:
        return None, "Majlis davomiyligi raqam bo'lishi kerak (0 = cheklovsiz)"
    mm = max(0, min(1440, mm))
    aid = db.ex("INSERT INTO zoom_accounts(label,email,account_id,client_id,client_secret,max_min,enabled,status,created_at) VALUES(?,?,?,?,?,?,1,'new',?)",
                ((label or "").strip() or email, email, account_id, client_id, secure.enc(client_secret), mm, fmt(now())))
    return aid, None


def update_account(aid, label, email, account_id, client_id, client_secret, max_min) -> str | None:
    from . import secure
    a = account_raw(aid)
    if not a:
        return "Akkaunt topilmadi"
    email = (email or "").strip().lower()
    if not _MAIL.match(email):
        return "Zoom email noto'g'ri"
    other = db.one("SELECT id FROM zoom_accounts WHERE LOWER(email)=? AND id<>?", (email, aid))
    if other:
        return "Bu email boshqa akkauntda bor"
    try:
        mm = max(0, min(1440, int(max_min)))
    except (TypeError, ValueError):
        return "Majlis davomiyligi raqam bo'lishi kerak (0 = cheklovsiz)"
    sec = a["client_secret"] if not (client_secret or "").strip() else secure.enc(client_secret.strip())
    db.ex("UPDATE zoom_accounts SET label=?, email=?, account_id=?, client_id=?, client_secret=?, max_min=?, status='new', last_err=NULL, cool_until=NULL WHERE id=?",
          ((label or "").strip() or email, email, (account_id or "").strip() or a["account_id"], (client_id or "").strip() or a["client_id"], sec, mm, aid))
    return None


def bulk_add(text: str) -> tuple[int, list[str]]:
    """Har qatorda: email ; account_id ; client_id ; client_secret [; davomiylik [; nom]]  (ajratgich: ; yoki | yoki tab)."""
    n, errs = 0, []
    for ln, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = [x.strip() for x in re.split(r"[;|\t]", line)]
        if len(p) < 4:
            errs.append(f"{ln}-qator: kamida 4 ta qiymat kerak (email; account_id; client_id; client_secret)")
            continue
        aid, err = add_account(p[5] if len(p) > 5 else "", p[0], p[1], p[2], p[3], p[4] if len(p) > 4 else None)
        if err:
            errs.append(f"{ln}-qator: {err}")
        else:
            n += 1
    return n, errs


def set_account_ok(aid, plan=""):
    db.ex("UPDATE zoom_accounts SET status='ok', last_err=NULL, last_ok=?, cool_until=NULL, plan=COALESCE(NULLIF(?,''),plan) WHERE id=?", (fmt(now()), plan, aid))


def set_account_err(aid, err: str, cool_min=10):
    db.ex("UPDATE zoom_accounts SET status='error', last_err=?, cool_until=? WHERE id=?",
          (err[:600], fmt(now() + timedelta(minutes=cool_min)), aid))


async def delete_account(aid) -> str | None:
    if db.one("SELECT 1 FROM zoom_meetings WHERE account_id=? AND state='active'", (aid,)):
        return "Akkauntda faol majlis bor: avval «Bo'shatish» ni bosing yoki majlis tugashini kuting"
    db.ex("DELETE FROM zoom_accounts WHERE id=?", (aid,))
    return None


# ---------------------------------------------------------------- majlislar (segmentlar)
def meeting(mid) -> dict | None:
    r = db.one("SELECT * FROM zoom_meetings WHERE id=?", (mid,)) if mid else None
    return dict(r) if r else None


def mflags(m) -> dict:
    try:
        return json.loads(m.get("flags") or "{}")
    except Exception:
        return {}


def set_mflag(mid, key, val):
    m = meeting(mid)
    f = mflags(m or {})
    f[key] = val
    db.ex("UPDATE zoom_meetings SET flags=? WHERE id=?", (json.dumps(f, ensure_ascii=False), mid))


def start_url_of(m) -> str:
    from . import secure
    v = m.get("start_url") or ""
    if secure.is_encrypted(v):
        return secure.dec(v) or ""
    return v


def meetings_of(lid, include_deleted=False) -> list[dict]:
    sql = "SELECT * FROM zoom_meetings WHERE lesson_id=?" + ("" if include_deleted else " AND state<>'deleted'") + " ORDER BY seq, id"
    return [dict(r) for r in db.q(sql, (lid,))]


def account_busy(aid, a: datetime, b: datetime, ignore_mid=None) -> dict | None:
    """Akkaunt [a, b] oralig'ida band bo'lsa, shu majlisni qaytaradi."""
    for r in db.q("SELECT * FROM zoom_meetings WHERE account_id=? AND state='active'", (aid,)):
        m = dict(r)
        if ignore_mid and m["id"] == ignore_mid:
            continue
        ms, me = parse(m["start_at"]) - timedelta(minutes=BUSY_BEFORE_MIN), parse(m["end_at"]) + timedelta(minutes=BUSY_AFTER_MIN)
        if ms < b and me > a:
            return m
    return None


def account_status(a: dict, t: datetime | None = None) -> dict:
    """Akkaunt holati: free | busy | soon | disabled | error."""
    t = t or now()
    if not a["enabled"]:
        return {"state": "disabled", "text": "O'chirilgan", "until": ""}
    cur = account_busy(a["id"], t, t + timedelta(seconds=1))
    if cur:
        les = lesson(cur["lesson_id"]) if cur.get("lesson_id") else None
        g = group(les["group_id"]) if les else None
        what = (g["title"] if g else None) or cur.get("topic") or "majlis"
        return {"state": "busy", "text": f"Band: {what}", "until": cur["end_at"][11:16], "meeting_id": cur["id"]}
    nxt = account_busy(a["id"], t, t + timedelta(minutes=cfg("create_lead_min") + 10))
    if a.get("status") == "error" and a.get("cool_until") and parse(a["cool_until"]) > t:
        return {"state": "error", "text": "Xato: " + (a.get("last_err") or "")[:120], "until": ""}
    if nxt:
        return {"state": "soon", "text": f"Bo'sh, {nxt['start_at'][11:16]} da band bo'ladi", "until": nxt["start_at"][11:16]}
    return {"state": "free", "text": "Bo'sh", "until": ""}


def pool_summary() -> dict:
    t = now()
    accs = accounts()
    sts = [account_status(a, t) for a in accs]
    en = [s for a, s in zip(accs, sts) if a["enabled"]]
    return {"total": len(accs), "enabled": len(en), "free": sum(1 for s in en if s["state"] in ("free", "soon")),
            "busy": sum(1 for s in en if s["state"] == "busy"), "error": sum(1 for s in en if s["state"] == "error")}


def pool_problems() -> list[dict]:
    """Veb-platformada qizil ogohlantirish: bo'sh akkaunt yetishmagan darslar va davom ettirish kutayotganlar."""
    out = []
    for r in db.q("SELECT id, group_id, start_at, prov_err FROM zoom_lessons WHERE status IN ('planned','live') AND prov_err LIKE 'BAND%' AND end_at>? ORDER BY start_at",
                  (fmt(now()),)):
        g = group(r["group_id"]) or {}
        out.append({"lesson_id": r["id"], "text": f"«{g.get('title', '?')}» {r['start_at'][5:16]} darsi uchun bo'sh Zoom akkaunt yo'q — barcha akkauntlar band", "kind": "busy"})
    for r in db.q("SELECT * FROM zoom_meetings WHERE state<>'deleted' AND lesson_id IS NOT NULL AND prompt='yes' AND flags LIKE '%pending_continue%'"):
        m = dict(r)
        if mflags(m).get("pending_continue"):
            les = lesson(m["lesson_id"]) or {}
            g = group(les.get("group_id")) or {}
            out.append({"lesson_id": m["lesson_id"], "text": f"«{g.get('title', '?')}» darsini davom ettirish uchun bo'sh Zoom akkaunt yo'q — barcha akkauntlar band", "kind": "busy"})
    return out


def _candidates(start: datetime, lesson_end: datetime | None, exclude=(), only=None) -> list[dict]:
    """Akkauntlarni tanlash tartibi: cheksiz (Pro) akkaunt uzun dars uchun, so'ng eng uzoq ishlatilmagani."""
    t = now()
    accs = []
    for r in db.q("SELECT * FROM zoom_accounts WHERE enabled=1 ORDER BY id"):
        a = dict(r)
        if a["id"] in exclude or (only and a["id"] != only):
            continue
        if not only and a.get("cool_until") and parse(a["cool_until"]) > t:
            continue
        accs.append(a)
    long_need = bool(lesson_end and (lesson_end - start).total_seconds() / 60 > 40)
    accs.sort(key=lambda a: (0 if (a["max_min"] == 0) == long_need else 1, a.get("last_used") or ""))
    return accs


async def create_segment(les: dict | None, seq: int, start: datetime, *, topic: str = "", adhoc=False, only_account=None,
                         exclude=(), created_by="auto") -> tuple[int | None, str]:
    """Bo'sh akkauntdan yangi Zoom majlisi yaratadi. (meeting_id, xato) qaytaradi.
    Xato 'BAND' bilan boshlansa — hamma akkauntlar band; 'XATO' bilan boshlansa — Zoom/akkaunt xatosi."""
    from . import secure
    lend = parse(les["end_at"]) if les else None
    grp = group(les["group_id"]) if les else None
    topic = topic or (f"{grp['title']} — dars" if grp else "Majlis")
    accs = _candidates(start, lend, exclude, only_account)
    if not accs:
        return None, "XATO: yoqilgan Zoom akkaunt yo'q (Zoom akkauntlar sahifasida qo'shing)"
    errors, busy_n = [], 0
    for a in accs:
        mm = a["max_min"] or 0
        if mm:
            end = start + timedelta(minutes=mm)
        else:
            end = lend or (start + timedelta(minutes=120))
            if end <= start:
                end = start + timedelta(minutes=60)
        if account_busy(a["id"], start, end):
            busy_n += 1
            continue
        lim_end = min(end, lend) if (lend and mm) else end
        dur = max(10, int((lim_end - start).total_seconds() // 60))
        try:
            d = await Z.create_meeting(a, topic + (f" ({seq}-qism)" if seq > 1 else ""), start, dur if not adhoc else (mm or 120))
        except Z.ZoomError as e:
            set_account_err(a["id"], e.short())
            errors.append(f"{a['label']}: {e.short()}")
            continue
        mid = db.ex("INSERT INTO zoom_meetings(lesson_id,account_id,seq,start_at,end_at,zoom_id,join_url,start_url,passcode,topic,state,prompt,flags,adhoc,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'active','none','{}',?,?,?)",
                    (les["id"] if les else None, a["id"], seq, fmt(start), fmt(end), str(d["id"]), d["join_url"],
                     secure.enc(d.get("start_url") or "") if d.get("start_url") else "", d.get("password") or "", topic, 1 if adhoc else 0, created_by, fmt(now())))
        db.ex("UPDATE zoom_accounts SET last_used=? WHERE id=?", (fmt(now()), a["id"]))
        set_account_ok(a["id"])
        return mid, ""
    if errors and busy_n == 0:
        return None, "XATO: " + " | ".join(errors)[:700]
    if errors:
        return None, "BAND: qolgan akkauntlar band; xatolar: " + " | ".join(errors)[:500]
    return None, f"BAND: barcha Zoom akkauntlar band ({busy_n} ta)"


async def _drop_meetings(lid, delete_remote=True):
    """Darsning faol majlislarini Zoom'dan o'chiradi va 'deleted' deb belgilaydi."""
    for m in meetings_of(lid):
        if delete_remote and m.get("zoom_id") and m["state"] == "active":
            a = account_raw(m["account_id"])
            if a:
                await Z.delete_meeting(a, m["zoom_id"])
        db.ex("UPDATE zoom_meetings SET state='deleted' WHERE id=?", (m["id"],))


async def release_account(aid) -> int:
    """Akkauntni qo'lda bo'shatish: faol majlislar tugatilgan deb belgilanadi (Zoom'da ham tugatishga urinadi)."""
    a = account_raw(aid)
    n = 0
    for r in db.q("SELECT * FROM zoom_meetings WHERE account_id=? AND state='active'", (aid,)):
        m = dict(r)
        if a and m.get("zoom_id"):
            await Z.end_meeting(a, m["zoom_id"])
        db.ex("UPDATE zoom_meetings SET state='ended' WHERE id=?", (m["id"],))
        n += 1
    return n


# ---------------------------------------------------------------- darslar
def lesson(lid):
    r = db.one("SELECT * FROM zoom_lessons WHERE id=?", (lid,)) if lid else None
    return dict(r) if r else None


def flags(les) -> dict:
    try:
        return json.loads(les.get("flags") or "{}")
    except Exception:
        return {}


def set_flag(lid, key, val):
    f = flags(lesson(lid) or {})
    f[key] = val
    db.ex("UPDATE zoom_lessons SET flags=? WHERE id=?", (json.dumps(f, ensure_ascii=False), lid))


def lesson_teacher(les):
    return teacher(les.get("teacher_id")) or teacher((group(les["group_id"]) or {}).get("teacher_id"))


def decorate(les: dict, with_meetings=True) -> dict:
    g = group(les["group_id"]) or {}
    t = lesson_teacher(les)
    s, e = parse(les["start_at"]), parse(les["end_at"])
    les.update(group_title=g.get("title", "?"), teacher_name=(t or {}).get("name", ""), day=s.strftime("%Y-%m-%d"),
               hhmm=s.strftime("%H:%M"), end_hhmm=e.strftime("%H:%M"), wd=WD_SHORT[s.weekday()],
               status_uz=STATUS_UZ.get(les["status"], les["status"]), fl=flags(les))
    if with_meetings:
        ms = meetings_of(les["id"])
        for m in ms:
            m["start_url_plain"] = start_url_of(m)
            a = account_raw(m["account_id"])
            m["account_label"] = (a or {}).get("label", "?")
        act = [m for m in ms if m["state"] == "active"]
        les["meetings"] = ms
        les["cur"] = act[-1] if act else None
        les["join_url"] = (act[-1] if act else {}).get("join_url", "")
    return les


def lessons_between(a: str, b: str, group_id=None, statuses=None):
    sql, args = "SELECT * FROM zoom_lessons WHERE start_at>=? AND start_at<?", [a, b]
    if group_id:
        sql += " AND group_id=?"
        args.append(group_id)
    if statuses:
        sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
        args += list(statuses)
    return [decorate(dict(r)) for r in db.q(sql + " ORDER BY start_at", args)]


async def generate(group_id=None) -> int:
    """Faol jadvallardan keyingi `horizon_days` kun uchun darslar yaratadi (mavjud bo'lsa tegmaydi)."""
    ready()
    horizon = max(1, cfg("horizon_days"))
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    n = 0
    sql = ("SELECT s.* FROM zoom_schedule s JOIN zoom_groups g ON g.id=s.group_id WHERE s.active=1 AND g.active=1"
           + (" AND s.group_id=?" if group_id else ""))
    for s in db.q(sql, (group_id,) if group_id else ()):
        for d in range(horizon + 1):
            day = today + timedelta(days=d)
            if day.weekday() != s["weekday"]:
                continue
            start = datetime.strptime(f"{day:%Y-%m-%d} {s['start_time']}", "%Y-%m-%d %H:%M")
            end = start + timedelta(minutes=s["duration_min"])
            if end < now():
                continue
            slot = f"{day:%Y-%m-%d}"
            if db.one("SELECT 1 FROM zoom_lessons WHERE schedule_id=? AND slot_day=?", (s["id"], slot)):
                continue
            db.ex("INSERT INTO zoom_lessons(group_id,schedule_id,slot_day,teacher_id,start_at,end_at,duration_min,status,flags,created_at) "
                  "VALUES(?,?,?,?,?,?,?,'planned','{}',?)",
                  (s["group_id"], s["id"], slot, s["teacher_id"], fmt(start), fmt(end), s["duration_min"], fmt(now())))
            n += 1
    return n


def add_extra_lesson(gid, start: datetime, duration: int, teacher_id=None) -> int:
    end = start + timedelta(minutes=duration)
    return db.ex("INSERT INTO zoom_lessons(group_id,schedule_id,slot_day,teacher_id,start_at,end_at,duration_min,status,flags,created_at) "
                 "VALUES(?,NULL,?,?,?,?,?,'planned','{}',?)",
                 (gid, f"{start:%Y-%m-%d}", teacher_id, fmt(start), fmt(end), duration, fmt(now())))


# ---------------------------------------------------------------- 1-majlisni yaratish
async def provision(lid) -> tuple[bool, str]:
    """Darsning birinchi Zoom majlisini yaratadi (bo'sh akkauntdan). Muvaffaqiyatsiz bo'lsa sababini saqlaydi."""
    les = lesson(lid)
    if not les or les["status"] in ("cancelled", "done", "missed"):
        return False, "Dars topilmadi yoki yakunlangan"
    if meetings_of(lid):
        return True, "Majlis allaqachon yaratilgan"
    grp = group(les["group_id"])
    s = parse(les["start_at"])
    start = max(now(), s - timedelta(minutes=max(0, cfg("group_link_min"))))      # 40 daqiqalik hisob guruhga havola ketganda boshlanadi
    mid, err = await create_segment(les, 1, start)
    if mid:
        db.ex("UPDATE zoom_lessons SET prov_err=NULL, prov_at=? WHERE id=?", (fmt(now()), lid))
        return True, "Zoom majlisi tayyor"
    db.ex("UPDATE zoom_lessons SET prov_err=?, prov_at=? WHERE id=?", (err[:900], fmt(now()), lid))
    is_busy = err.startswith("BAND")
    mins = int((s - now()).total_seconds() // 60)
    await alert("pool_busy" if is_busy else "zoom_error",
                (f"«{grp['title']}» {les['start_at'][11:16]} darsi ({mins} daqiqadan keyin) uchun Zoom majlisi yaratilmadi: "
                 + (err.split(": ", 1)[1] if ": " in err else err)),
                level="error", lesson_id=lid, group_id=grp["id"], dedupe=f"prov:{lid}:{err[:4]}:{now():%Y%m%d%H}{now().minute // 15}")
    return False, err


async def retry_provision():
    """Boshlanishiga `create_lead_min` daqiqa qolgan (yoki boshlangan) darslarga majlis yaratadi; muvaffaqiyatsiz bo'lsa qayta urinadi."""
    t = now()
    lead = fmt(t + timedelta(minutes=cfg("create_lead_min")))
    for r in db.q("SELECT id, prov_at FROM zoom_lessons WHERE status IN ('planned','live') AND start_at<=? AND end_at>? ORDER BY start_at", (lead, fmt(t))):
        if meetings_of(r["id"]):
            continue
        if r["prov_at"] and t - parse(r["prov_at"]) < timedelta(seconds=45):
            continue
        await provision(r["id"])


# ---------------------------------------------------------------- bekor qilish / ko'chirish
async def cancel_lesson(lid, reason="") -> str:
    les = lesson(lid)
    if not les or les["status"] in ("cancelled", "done"):
        return "Bu darsni bekor qilib bo'lmaydi"
    grp, t = group(les["group_id"]), lesson_teacher(les)
    await _drop_meetings(lid)
    db.ex("UPDATE zoom_lessons SET status='cancelled', cancel_reason=?, prov_err=NULL WHERE id=?", (reason.strip(), lid))
    s = parse(les["start_at"])
    text = f"❌ {s:%d.%m} kuni soat {s:%H:%M} dagi dars bekor qilindi." + (f"\nSabab: {reason.strip()}" if reason.strip() else "")
    await notify_lesson_people(les, grp, t, text, "cancel")
    return "Dars bekor qilindi"


async def move_lesson(lid, new_start: datetime, duration=None) -> str:
    les = lesson(lid)
    if not les or les["status"] in ("cancelled", "done"):
        return "Bu darsni ko'chirib bo'lmaydi"
    grp, t = group(les["group_id"]), lesson_teacher(les)
    dur = int(duration or les["duration_min"])
    old = parse(les["start_at"])
    await _drop_meetings(lid)
    end = new_start + timedelta(minutes=dur)
    db.ex("UPDATE zoom_lessons SET start_at=?, end_at=?, duration_min=?, status='planned', stopped=0, moved_from=?, flags='{}', prov_err=NULL, prov_at=NULL WHERE id=?",
          (fmt(new_start), fmt(end), dur, les.get("moved_from") or fmt(old), lid))
    text = f"🔄 Dars ko'chirildi: {old:%d.%m %H:%M} → {new_start:%d.%m %H:%M}. Yangi havola dars boshlanishidan oldin yuboriladi."
    await notify_lesson_people(lesson(lid), grp, t, text, "move")
    return "Dars ko'chirildi"


# ---------------------------------------------------------------- Telegram (Zoom Online akkaunti: guruhlarga va admin)
class TgFail(Exception):
    pass


def meet_account():
    r = db.one("SELECT * FROM accounts WHERE workspace='meet' ORDER BY id LIMIT 1")
    return dict(r) if r else None


def _friendly(e: Exception) -> str:
    n = type(e).__name__
    m = {"UsernameNotOccupiedError": "bunday @username topilmadi", "UsernameInvalidError": "@username noto'g'ri",
         "UserPrivacyRestrictedError": "foydalanuvchi maxfiylik sozlamasi xabar yuborishga ruxsat bermaydi",
         "PeerFloodError": "Telegram spam cheklovi (PeerFlood): akkaunt hozircha begonalarga yoza olmaydi",
         "ChatWriteForbiddenError": "akkauntning guruhga yozish huquqi yo'q",
         "ChannelPrivateError": "akkaunt bu guruhda a'zo emas", "UserBannedInChannelError": "akkaunt guruhdan bloklangan",
         "FloodWaitError": f"Telegram kutishni so'radi ({getattr(e, 'seconds', '?')} s)",
         "InputUserDeactivatedError": "foydalanuvchi akkaunti o'chirilgan"}
    return m.get(n, f"{n}: {e}")


async def _client():
    from .core import manager
    acc = meet_account()
    if not acc:
        raise TgFail("Zoom Online uchun Telegram akkaunt ulanmagan")
    try:
        c = await manager.get(acc["id"])._get_client()
        if not await c.is_user_authorized():
            raise TgFail("Zoom Online Telegram akkaunti ulanmagan (Akkaunt sahifasida qayta ulang)")
    except TgFail:
        raise
    except Exception as e:
        raise TgFail(f"Telegramga ulanib bo'lmadi: {e}")
    return c


async def tg_send(target, text: str):
    """target: 'me' | '@username' | tg_id (int). Muvaffaqiyatsiz bo'lsa TgFail."""
    c = await _client()
    try:
        if target == "me":
            ent = "me"
        elif isinstance(target, int):
            try:
                ent = await c.get_entity(target)
            except (ValueError, TypeError, KeyError):
                await c.get_dialogs()
                ent = await c.get_entity(target)
        else:
            ent = target if str(target).startswith("@") else "@" + str(target)
        await c.send_message(ent, text, link_preview=False)
    except TgFail:
        raise
    except Exception as e:
        raise TgFail(_friendly(e))


async def tg_groups() -> list[dict]:
    """Zoom Online akkaunti a'zo bo'lgan guruhlar (tanlash uchun)."""
    c = await _client()
    out = []
    async for d in c.iter_dialogs():
        if d.is_group:
            out.append({"tg_id": d.id, "title": d.name or "Nomsiz", "username": getattr(d.entity, "username", None) or ""})
    out.sort(key=lambda x: x["title"].lower())
    return out


async def tg_resolve(ref: str) -> dict:
    c = await _client()
    ref = ref.strip()
    ent = await c.get_entity(int(ref) if re.fullmatch(r"-?\d+", ref) else ref)
    from telethon import utils
    return {"tg_id": utils.get_peer_id(ent), "title": getattr(ent, "title", None) or getattr(ent, "first_name", "") or ref,
            "username": getattr(ent, "username", None) or ""}


# ---------------------------------------------------------------- ogohlantirishlar
def add_alert(kind, text, level="warn", lesson_id=None, group_id=None, copy=None, dedupe=None) -> int | None:
    """Ogohlantirishni yozadi (dedupe bo'yicha takrorlanmaydi). Yangi yozilsa id qaytaradi."""
    ready()
    if dedupe and db.one("SELECT 1 FROM zoom_alerts WHERE dedupe=?", (dedupe,)):
        return None
    aid = db.ex("INSERT INTO zoom_alerts(ts,kind,level,lesson_id,group_id,text,copy_text,seen,dedupe) VALUES(?,?,?,?,?,?,?,0,?)",
                (fmt(now()), kind, level, lesson_id, group_id, text[:1500], copy, dedupe))
    try:
        notify.event("warn" if level != "info" else "info", "majlis", text)
        notify.toast("Zoom Online", text)
    except Exception:
        pass
    return aid or None


async def alert(kind, text, level="warn", lesson_id=None, group_id=None, copy=None, dedupe=None, admin=True):
    """Ogohlantirish + adminga Telegram xabari (admin @username yoki «Saqlangan xabarlar»)."""
    aid = add_alert(kind, text, level, lesson_id, group_id, copy, dedupe)
    if aid and admin:
        await tg_admin(text)
    return aid


async def tg_admin(text: str) -> bool:
    try:
        await tg_send("@" + cfg("admin_username").lstrip("@") if cfg("admin_username") else "me", text)
        return True
    except TgFail as e:
        log.info("Adminga xabar yetmadi: %s", e)
        return False


def _bot():
    from . import meet_bot
    return meet_bot


async def to_teacher(les, grp, t, text_html: str, flag_key: str, buttons=None, copy_text="") -> bool:
    """O'qituvchiga bot orqali yuboradi; yetmasa adminga ogohlantirish (+ qo'lda nusxalash uchun matn)."""
    try:
        if not t:
            raise _bot().BotFail("dars uchun o'qituvchi tanlanmagan")
        await _bot().send_teacher(t, text_html, buttons)
        return True
    except _bot().BotFail as e:
        await alert("send_fail", f"«{grp['title']}» {les['start_at'][11:16]} darsi: o'qituvchiga bot orqali xabar yetmadi — {e}. Havolani qo'lda yuboring.",
                    level="error", lesson_id=les["id"], group_id=grp["id"], copy=copy_text or re.sub(r"<[^>]+>", "", text_html), dedupe=f"sf:{les['id']}:{flag_key}")
        return False


async def to_group(les, grp, text: str, flag_key: str) -> bool:
    if not grp.get("tg_id"):
        return False
    try:
        await tg_send(int(grp["tg_id"]), text)
        return True
    except TgFail as e:
        await alert("send_fail", f"«{grp['title']}» guruhiga xabar yetmadi ({flag_key}): {e}. Havolani qo'lda nusxalab yuboring.",
                    level="error", lesson_id=les["id"] if les else None, group_id=grp["id"], copy=text, dedupe=f"sf:{les['id'] if les else 0}:{flag_key}:g")
        return False


async def notify_lesson_people(les, grp, t, text: str, kind: str):
    """Bekor qilish / ko'chirish: guruhga (Telegram akkaunt) va o'qituvchiga (bot)."""
    await to_group(les, grp, text, kind)
    await to_teacher(les, grp, t, Z.esc(text), kind + ":t")


# ---------------------------------------------------------------- xabar matnlari
def fmt_when(les) -> str:
    return f"{les['start_at'][11:16]}–{les['end_at'][11:16]}"


def msg_teacher_remind(les, grp, mins) -> str:
    return (f"⏰ <b>{mins} daqiqadan keyin</b> darsingiz bor\n👥 Guruh: <b>{Z.esc(grp['title'])}</b>\n🕒 {fmt_when(les)} ({les['duration_min']} daqiqa)")


def msg_teacher_link(les, grp, m, mins, cont=False) -> tuple[str, list]:
    head = ("🔄 <b>Dars davom etmoqda: yangi Zoom majlisi</b>" if cont
            else f"▶️ <b>Dars {max(0, mins)} daqiqadan keyin</b> boshlanadi" if mins > 0 else "▶️ <b>Dars boshlanmoqda</b>")
    txt = (f"{head}\n👥 Guruh: <b>{Z.esc(grp['title'])}</b>\n🕒 {fmt_when(les)}\n"
           f"⏱ Majlis taxminan {m['end_at'][11:16]} gacha ishlaydi (bepul Zoom 40 daqiqa).\n\n"
           f"«Darsni boshlash» tugmasi sizni Zoom'da host qilib kiritadi. O'quvchilarga havola alohida yuboriladi.")
    su = start_url_of(m)
    buttons = []
    if su:
        buttons.append([{"text": "▶️ Darsni boshlash (host)", "url": su}])
    buttons.append([{"text": "🔗 O'quvchilar havolasi", "url": m["join_url"]}])
    return txt, buttons


def msg_group_link(les, grp, t, m, cont=False) -> str:
    head = "🔄 Dars davom etmoqda. Yangi havola (oldingisi tez orada tugaydi):" if cont else "📚 Dars boshlanadi."
    return (f"{head}\n🕒 {fmt_when(les)}" + (f"\n👩‍🏫 O'qituvchi: {t['name']}" if t else "") + f"\n🔗 Kirish: {m['join_url']}")


def msg_group_remind(les, mins) -> str:
    return f"⏰ Eslatma: soat {les['start_at'][11:16]} da dars bor ({mins} daqiqadan keyin)."


async def send_links(les, grp, t, m, cont=False, who=("teacher", "group")) -> dict:
    """Majlis havolalarini yuboradi: o'qituvchiga (bot) va/yoki guruhga (Telegram akkaunt)."""
    res = {}
    mins = int((parse(les["start_at"]) - now()).total_seconds() // 60)
    if "teacher" in who:
        txt, btn = msg_teacher_link(les, grp, m, mins, cont)
        plain = f"{grp['title']} {fmt_when(les)}\nBoshlash (host): {start_url_of(m)}\nO'quvchilar: {m['join_url']}"
        res["teacher"] = await to_teacher(les, grp, t, txt, f"link{m['seq']}", btn, copy_text=plain)
    if "group" in who:
        res["group"] = await to_group(les, grp, msg_group_link(les, grp, t, m, cont), f"glink{m['seq']}")
    return res


async def resend(lid, who: str) -> str:
    """Qo'lda (qayta) yuborish: who = 'teacher' | 'group'."""
    les = lesson(lid)
    ms = [m for m in meetings_of(lid) if m["state"] == "active"]
    if not les or not ms:
        return "Dars uchun faol Zoom majlisi hali yo'q"
    m = ms[-1]
    grp, t = group(les["group_id"]), lesson_teacher(les)
    if who == "teacher":
        if not t:
            return "Dars uchun o'qituvchi tanlanmagan"
        ok = (await send_links(les, grp, t, m, cont=m["seq"] > 1, who=("teacher",))).get("teacher")
    else:
        if not grp.get("tg_id"):
            return "Guruhning Telegram ID si yo'q"
        ok = (await send_links(les, grp, t, m, cont=m["seq"] > 1, who=("group",))).get("group")
    return "Yuborildi" if ok else "Yuborilmadi (Ogohlantirishlar sahifasiga qarang)"


# ---------------------------------------------------------------- davom ettirish (40 daqiqalik zanjir)
async def continue_lesson(mid, source="teacher") -> tuple[bool, str]:
    """Joriy majlis (mid) tugagach dars davom etishi uchun boshqa bo'sh akkauntdan yangi majlis yaratadi, havolalarni yuboradi."""
    m = meeting(mid)
    if not m or not m.get("lesson_id"):
        return False, "Majlis topilmadi"
    les = lesson(m["lesson_id"])
    if not les or les["status"] in ("cancelled", "missed"):
        return False, "Dars bekor qilingan"
    if any(x["seq"] > m["seq"] and x["state"] != "deleted" for x in meetings_of(les["id"])):
        return True, "Dars allaqachon davom ettirilgan"
    t = now()
    if parse(les["end_at"]) - t < timedelta(minutes=cfg("min_remaining")):
        db.ex("UPDATE zoom_meetings SET prompt='no' WHERE id=?", (mid,))
        return False, "Dars vaqti tugagan"
    grp, teacher_ = group(les["group_id"]), lesson_teacher(les)
    if les["status"] == "done":                                   # kech bosilgan bo'lsa ham dars hali tugamagan: qayta ochamiz
        db.ex("UPDATE zoom_lessons SET status='live' WHERE id=?", (les["id"],))
    nid, err = await create_segment(les, m["seq"] + 1, t, exclude=())
    db.ex("UPDATE zoom_meetings SET prompt='yes' WHERE id=?", (mid,))
    if not nid:
        set_mflag(mid, "pending_continue", fmt(t))
        await alert("pool_busy" if err.startswith("BAND") else "zoom_error",
                    f"«{grp['title']}» darsini davom ettirish uchun Zoom majlisi yaratilmadi: {err.split(': ', 1)[-1]}. Avtomatik qayta uriniladi.",
                    level="error", lesson_id=les["id"], group_id=grp["id"], dedupe=f"cont:{mid}:{err[:4]}:{t:%H%M}")
        return False, err
    set_mflag(mid, "pending_continue", "")
    db.ex("UPDATE zoom_lessons SET prov_err=NULL WHERE id=?", (les["id"],))
    new = meeting(nid)
    set_mflag(nid, "sent", fmt(t))
    await send_links(les, grp, teacher_, new, cont=True)
    return True, "Yangi Zoom majlisi yaratildi, havolalar yuborildi"


async def stop_lesson(mid) -> str:
    """O'qituvchi «Bekor qilish» bosdi: dars bu majlis bilan tugaydi (davom etilmaydi)."""
    m = meeting(mid)
    if m:
        db.ex("UPDATE zoom_meetings SET prompt='no' WHERE id=?", (mid,))
        if m.get("lesson_id"):
            db.ex("UPDATE zoom_lessons SET stopped=1 WHERE id=?", (m["lesson_id"],))
    return "Dars davom ettirilmaydi"


async def adhoc_meeting(aid, topic="") -> tuple[int | None, str]:
    """Tanlangan akkauntda darsga bog'lanmagan majlis (havolasini nusxalash uchun)."""
    a = account_raw(aid)
    if not a:
        return None, "Akkaunt topilmadi"
    mid, err = await create_segment(None, 1, now(), topic=topic or f"Majlis ({a['label']})", adhoc=True, only_account=aid, created_by="admin")
    return mid, err


# ---------------------------------------------------------------- fon sikli
async def process_lessons():
    t = now()
    # 1) tugagan majlislar
    for r in db.q("SELECT * FROM zoom_meetings WHERE state='active'"):
        m = dict(r)
        if t > parse(m["end_at"]) + timedelta(minutes=1):
            db.ex("UPDATE zoom_meetings SET state='ended' WHERE id=?", (m["id"],))
    # 2) davom ettirish kutayotganlarni qayta urinish
    for r in db.q("SELECT * FROM zoom_meetings WHERE lesson_id IS NOT NULL AND prompt='yes' AND state<>'deleted' AND flags LIKE '%pending_continue%'"):
        m = dict(r)
        if mflags(m).get("pending_continue"):
            await continue_lesson(m["id"], "retry")
    # 3) darslar
    lim = fmt(t + timedelta(minutes=max(cfg("remind_a"), cfg("t_remind_a")) + 2))
    for r in db.q("SELECT * FROM zoom_lessons WHERE status IN ('planned','live') AND start_at<=? AND end_at>=?", (lim, fmt(t - timedelta(hours=1)))):
        les = dict(r)
        grp = group(les["group_id"])
        if not grp or not grp.get("active"):
            continue
        tch = lesson_teacher(les)
        fl = flags(les)
        mins = (parse(les["start_at"]) - t).total_seconds() / 60
        ms = [m for m in meetings_of(les["id"])]
        act = [m for m in ms if m["state"] == "active"]
        # o'qituvchiga eslatmalar (20 va 10 daq oldin) — bot
        if mins > 0 and les["status"] == "planned":
            ta, tb = cfg("t_remind_a"), cfg("t_remind_b")
            if "t_a" not in fl and tb < mins <= ta:
                set_flag(les["id"], "t_a", fmt(t))
                await to_teacher(les, grp, tch, msg_teacher_remind(les, grp, max(1, round(mins))), "t_a")
            elif "t_b" not in fl and mins <= tb and not act:
                set_flag(les["id"], "t_b", fmt(t))
                await to_teacher(les, grp, tch, msg_teacher_remind(les, grp, max(1, round(mins))), "t_b")
        # guruhga eslatmalar (60 va 10 daq oldin)
        if cfg("remind_on") and grp.get("tg_id") and mins > 0:
            a_, b_ = cfg("remind_a"), cfg("remind_b")
            if "g_b" not in fl and mins <= b_:
                set_flag(les["id"], "g_b", fmt(t))
                if "g_a" not in fl:
                    set_flag(les["id"], "g_a", "o'tkazildi")
                await to_group(les, grp, msg_group_remind(les, max(1, round(mins))), "g_b")
            elif "g_a" not in fl and b_ < mins <= a_:
                set_flag(les["id"], "g_a", fmt(t))
                await to_group(les, grp, msg_group_remind(les, max(1, round(mins))), "g_a")
        # havolalar (1-majlis): o'qituvchiga -10 daq, guruhga -5 daq; keyingi majlislar darhol yuboriladi
        for m in act:
            mf = mflags(m)
            if m["seq"] == 1:
                if "t_sent" not in mf and mins <= cfg("t_remind_b"):
                    set_mflag(m["id"], "t_sent", fmt(t))
                    txt, btn = msg_teacher_link(les, grp, m, max(0, round(mins)))
                    await to_teacher(les, grp, tch, txt, f"link{m['seq']}", btn,
                                     copy_text=f"{grp['title']} {fmt_when(les)}\nBoshlash (host): {start_url_of(m)}\nO'quvchilar: {m['join_url']}")
                if "g_sent" not in mf and mins <= cfg("group_link_min"):
                    set_mflag(m["id"], "g_sent", fmt(t))
                    await to_group(les, grp, msg_group_link(les, grp, tch, m), f"glink{m['seq']}")
            # davom etish so'rovi (tugashiga prompt_before_min qolganda, dars hali tugamagan bo'lsa), bir marta
            if m["prompt"] == "none":
                remaining = (parse(les["end_at"]) - parse(m["end_at"])).total_seconds() / 60
                to_end = (parse(m["end_at"]) - t).total_seconds() / 60
                if les["stopped"] or remaining < cfg("min_remaining"):
                    db.ex("UPDATE zoom_meetings SET prompt='na' WHERE id=?", (m["id"],))
                elif to_end <= cfg("prompt_before_min"):
                    await prompt_continue(les, grp, tch, m)
        # kutilgan davom ettirish javobi muddati o'tdi
        for m in ms:
            if m["prompt"] == "sent" and t > parse(m["end_at"]) + timedelta(minutes=6):
                db.ex("UPDATE zoom_meetings SET prompt='expired' WHERE id=?", (m["id"],))
        # holat
        if les["status"] == "planned" and act and t >= parse(les["start_at"]) - timedelta(minutes=cfg("group_link_min")):
            db.ex("UPDATE zoom_lessons SET status='live' WHERE id=?", (les["id"],))
        await finish_check(les["id"])


async def prompt_continue(les, grp, tch, m):
    """Tugashiga 3 daqiqa qolganda o'qituvchiga bir marta: «Davom etasizmi?» (Davom etish / Bekor qilish)."""
    until = m["end_at"][11:16]
    txt = (f"⏳ <b>{Z.esc(grp['title'])}</b> darsining Zoom majlisi <b>{until}</b> da tugaydi (~{cfg('prompt_before_min')} daqiqa).\n"
           f"Dars {les['end_at'][11:16]} gacha rejalashtirilgan. Darsni davom ettirasizmi?")
    buttons = [[{"text": "✅ Davom etish", "callback_data": f"z:y:{m['id']}"}, {"text": "❌ Bekor qilish", "callback_data": f"z:n:{m['id']}"}]]
    if not tch or not tch.get("chat_id"):
        db.ex("UPDATE zoom_meetings SET prompt='na' WHERE id=?", (m["id"],))
        await alert("send_fail", f"«{grp['title']}» darsi: o'qituvchi botga ulanmagan, «Davom etasizmi?» so'rovi yuborilmadi. "
                    f"Davom ettirish kerak bo'lsa, dars sahifasidagi «Davom ettirish» tugmasini bosing.", level="error",
                    lesson_id=les["id"], group_id=grp["id"], dedupe=f"pr:{m['id']}")
        return
    try:
        res = await _bot().send_teacher(tch, txt, buttons)
        db.ex("UPDATE zoom_meetings SET prompt='sent' WHERE id=?", (m["id"],))
        set_mflag(m["id"], "prompt_msg", [res.get("chat", {}).get("id") if isinstance(res, dict) else None, res.get("message_id") if isinstance(res, dict) else None])
    except _bot().BotFail as e:
        db.ex("UPDATE zoom_meetings SET prompt='na' WHERE id=?", (m["id"],))
        await alert("send_fail", f"«{grp['title']}» darsi: «Davom etasizmi?» so'rovi o'qituvchiga yetmadi — {e}. Davom ettirish kerak bo'lsa, dars sahifasidagi tugmani bosing.",
                    level="error", lesson_id=les["id"], group_id=grp["id"], dedupe=f"pr:{m['id']}")


async def finish_check(lid):
    """Dars holatini yangilaydi: tugagan bo'lsa 'done', majlissiz o'tib ketgan bo'lsa 'missed'."""
    les = lesson(lid)
    if not les or les["status"] not in ("planned", "live"):
        return
    t = now()
    ms = [m for m in meetings_of(lid) if m["state"] != "deleted"]
    end = parse(les["end_at"])
    if not ms:
        if t > end:
            db.ex("UPDATE zoom_lessons SET status='missed' WHERE id=?", (lid,))
            grp = group(les["group_id"]) or {}
            await alert("missed", f"«{grp.get('title', '?')}» {les['start_at'][5:16]} darsi uchun Zoom majlisi yaratilmadi, dars o'tmadi deb belgilandi.",
                        level="error", lesson_id=lid, group_id=les["group_id"], dedupe=f"missed:{lid}")
        return
    last = ms[-1]
    if last["state"] == "active":
        return
    if t > end or last["prompt"] in ("no", "expired", "na") or les["stopped"]:
        db.ex("UPDATE zoom_lessons SET status='done' WHERE id=?", (lid,))


async def loop():
    """Fon sikli (supervise ostida): darslarni oldindan yaratish, majlis ochish, havola/eslatma va davom ettirish so'rovlari."""
    await asyncio.sleep(10)
    last_gen = datetime.min
    while True:
        try:
            ready()
            if (now() - last_gen) > timedelta(minutes=30):
                await generate()
                last_gen = now()
            await retry_provision()
            await process_lessons()
            put("sched_beat", fmt(now()))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("meet_sched.loop")
            notify.event("error", "majlis", "Zoom Online siklida xato (loglarga qarang)")
        await asyncio.sleep(20)


# ---------------------------------------------------------------- kalendar (ICS eksport)
def ics(lessons: list[dict], name="Zoom Online") -> str:
    def esc(s):
        return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Telegram Group Post//Zoom Online//UZ", f"X-WR-CALNAME:{esc(name)}", "X-WR-TIMEZONE:Asia/Tashkent"]
    for l in lessons:
        if l["status"] == "cancelled":
            continue
        s, e = parse(l["start_at"]), parse(l["end_at"])
        lines += ["BEGIN:VEVENT", f"UID:zoom-lesson-{l['id']}@majlislar", f"DTSTAMP:{now():%Y%m%dT%H%M%S}",
                  f"DTSTART;TZID=Asia/Tashkent:{s:%Y%m%dT%H%M%S}", f"DTEND;TZID=Asia/Tashkent:{e:%Y%m%dT%H%M%S}",
                  f"SUMMARY:{esc(l['group_title'] + ' — dars')}", f"DESCRIPTION:{esc((l.get('teacher_name') or '') and 'O`qituvchi: ' + l['teacher_name'])}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------- Tizim tahlili uchun
def health_items() -> list[dict]:
    """syscheck.run_all() ga bitta chaqiruv bilan qo'shiladi."""
    ready()
    g = "Zoom Online (Zoom)"

    def it(title, state, detail="", fix="", key=None):
        return {"group": g, "title": title, "state": state, "detail": detail, "fix": fix, "key": key or title}
    out = []
    ps = pool_summary()
    if ps["total"] == 0:
        out.append(it("Zoom akkauntlar", "info", "Hali qo'shilmagan", "Zoom Online → Zoom akkauntlar: akkaunt qo'shing (Yo'riqnoma 1-qism).", "zoom_pool"))
    else:
        st = "ok" if ps["free"] > 0 else "warn"
        out.append(it("Zoom akkauntlar", st, f"jami {ps['total']}, bo'sh {ps['free']}, band {ps['busy']}, xato {ps['error']}",
                      "" if ps["free"] else "Hamma akkauntlar band: akkaunt qo'shing yoki jadvalni tekshiring.", "zoom_pool"))
    errs = [a for a in accounts(True) if a.get("status") == "error"]
    if errs:
        out.append(it("Zoom akkaunt xatolari", "fail" if len(errs) == len(accounts(True)) else "warn",
                      "; ".join(f"{a['label']}: {(a.get('last_err') or '')[:80]}" for a in errs[:3]),
                      "Zoom Online → Zoom akkauntlar → «Tekshirish»: sababi ko'rsatiladi.", "zoom_err"))
    pp = pool_problems()
    if pp:
        out.append(it("Bo'sh akkaunt yetishmayapti", "fail", pp[0]["text"], "Zoom akkaunt qo'shing yoki darslar vaqtini o'zgartiring.", "zoom_busy"))
    tok = get("bot_token")
    if not tok:
        out.append(it("O'qituvchilar boti", "info", "Bot tokeni kiritilmagan", "Zoom Online → Sozlamalar: @BotFather dan olingan tokenni kiriting.", "zoom_bot"))
    else:
        err = get("bot_err")
        ts = teachers(True)
        linked = sum(1 for t_ in ts if t_.get("chat_id"))
        out.append(it("O'qituvchilar boti", "fail" if err else "ok", (f"@{get('bot_username', '?')}; o'qituvchilar ulangan: {linked}/{len(ts)}" if not err else f"Xato: {err}"),
                      "Token to'g'ri ekanini va internetni tekshiring." if err else ("Ulanmagan o'qituvchilar botga /start bosishi kerak." if linked < len(ts) else ""), "zoom_bot"))
    live = db.one("SELECT COUNT(*) c FROM zoom_lessons WHERE status='live'")["c"]
    soon = db.q("SELECT l.start_at, g.title FROM zoom_lessons l JOIN zoom_groups g ON g.id=l.group_id WHERE l.status IN ('planned','live') AND l.start_at<? AND l.end_at>? ORDER BY l.start_at LIMIT 5",
                (fmt(now() + timedelta(hours=24)), fmt(now())))
    out.append(it("Faol majlislar", "info", f"hozir davom etayotgan darslar: {live}; 24 soat ichida: {len(soon)}"
                  + ("; " + ", ".join(f"{s['title']} {s['start_at'][11:16]}" for s in soon) if soon else ""), key="zoom_live"))
    unseen = db.one("SELECT COUNT(*) c FROM zoom_alerts WHERE seen=0 AND kind='send_fail'")["c"]
    if unseen:
        out.append(it("Yetmagan xabarlar", "warn", f"{unseen} ta xabar yetmagan", "Zoom Online → Ogohlantirishlar: havolani qo'lda nusxalab yuboring.", "zoom_sendfail"))
    if not meet_account():
        out.append(it("Telegram akkaunt (Zoom Online)", "info", "Ulanmagan", "Zoom Online → Telegram akkaunt.", "zoom_tg"))
    else:
        from .core import manager
        svc = manager.services.get(meet_account()["id"])
        out.append(it("Telegram akkaunt (Zoom Online)", "ok" if svc and svc.info else "fail", f"+{svc.info['phone']}" if svc and svc.info else "Ulanmagan",
                      "" if svc and svc.info else "Zoom Online → Telegram akkaunt: qayta ulang.", "zoom_tg"))
    try:
        from . import syscheck
        beats = {r["name"]: r for r in db.q("SELECT * FROM sys_beat WHERE name IN ('meet_sched','meet_bot')")}
        for nm, title in (("meet_sched", "Zoom Online: jadval va eslatmalar"), ("meet_bot", "Zoom Online: o'qituvchilar boti")):
            tk, b = syscheck.TASKS.get(nm), beats.get(nm)
            if tk is None:
                out.append(it(title, "info", "Kuzatilmayapti", key=nm))
            elif tk.done() and not tk.cancelled():
                out.append(it(title, "fail", "To'xtagan", "Dasturni qayta ishga tushiring.", nm))
            elif b and b["state"] == "crashed":
                out.append(it(title, "warn", f"{b['restarts']} marta yiqilgan: {b['info'][:150]}", "Avtomatik qayta ishga tushadi.", nm))
            else:
                out.append(it(title, "ok", "Ishlayapti", key=nm))
    except Exception:
        pass
    return out
