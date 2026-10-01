"""Majlislar (Google Meet darslari): sxema, sozlamalar, o'qituvchi/guruh/jadval, darslarni yaratish,
Google Calendar, Telegram xabarlari, ogohlantirishlar va eslatma sikli.

Bu modul Group Post / Kanallarim jadvallariga tegmaydi: faqat mt_* jadvallari va workspace='meet' akkaunti.
Vaqt: hamma joyda Asia/Tashkent (bazada oddiy "YYYY-MM-DD HH:MM:SS" matn, Toshkent vaqti).
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from . import db, meet_google as G, notify, schema_ch
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
GMAIL_LIMIT_MIN = 60


# ---------------------------------------------------------------- vaqt
def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse(s: str) -> datetime:
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def from_utc(s: str) -> datetime:
    """Google RFC3339 (UTC) -> Toshkent vaqti (naive)."""
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.strip()).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).replace(tzinfo=None)


# ---------------------------------------------------------------- sxema (mt_*)
T = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
TABLES = [
    f"""CREATE TABLE IF NOT EXISTS mt_teachers(
        id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(255), gmail VARCHAR(255), tg_username VARCHAR(64), phone VARCHAR(40),
        note TEXT, meet_name VARCHAR(255), active INT DEFAULT 1, created_at VARCHAR(32)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS mt_groups(
        id INT AUTO_INCREMENT PRIMARY KEY, tg_id BIGINT, title VARCHAR(500), username VARCHAR(120), teacher_id INT,
        calendar_id VARCHAR(255), active INT DEFAULT 1, note TEXT, created_at VARCHAR(32),
        INDEX idx_mtg(active)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS mt_schedule(
        id INT AUTO_INCREMENT PRIMARY KEY, group_id INT NOT NULL, weekday INT, start_time VARCHAR(5), duration_min INT,
        teacher_id INT, active INT DEFAULT 1, created_at VARCHAR(32), INDEX idx_mts(group_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS mt_lessons(
        id INT AUTO_INCREMENT PRIMARY KEY, group_id INT NOT NULL, schedule_id INT, slot_day VARCHAR(10), teacher_id INT,
        start_at VARCHAR(19), end_at VARCHAR(19), duration_min INT, status VARCHAR(16) DEFAULT 'planned',
        space_name VARCHAR(80), meeting_uri VARCHAR(255), meeting_code VARCHAR(40), cal_event_id VARCHAR(255),
        cohost_ok INT DEFAULT 0, record_url VARCHAR(600), flags LONGTEXT, real_start VARCHAR(19), real_end VARCHAR(19),
        peak INT DEFAULT 0, warn_text TEXT, note TEXT, cancel_reason TEXT, moved_from VARCHAR(19), prov_at VARCHAR(19),
        prov_err TEXT, created_at VARCHAR(32),
        UNIQUE KEY u_mtl(schedule_id, slot_day), INDEX idx_mtl_s(start_at), INDEX idx_mtl_g(group_id, start_at)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS mt_attendance(
        id INT AUTO_INCREMENT PRIMARY KEY, lesson_id INT NOT NULL, part_name VARCHAR(190), raw_name VARCHAR(255),
        person VARCHAR(255), kind VARCHAR(10), user_id VARCHAR(80), first_in VARCHAR(19), last_out VARCHAR(19),
        total_sec INT DEFAULT 0, in_now INT DEFAULT 0, sessions_json LONGTEXT, sig VARCHAR(120), is_teacher INT DEFAULT 0,
        updated_at VARCHAR(19), UNIQUE KEY u_mta(lesson_id, part_name), INDEX idx_mta_l(lesson_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS mt_settings(`key` VARCHAR(80) PRIMARY KEY, value LONGTEXT) {T}""",
    f"""CREATE TABLE IF NOT EXISTS mt_alerts(
        id INT AUTO_INCREMENT PRIMARY KEY, ts VARCHAR(19), kind VARCHAR(24), level VARCHAR(8) DEFAULT 'warn', lesson_id INT,
        group_id INT, text TEXT, copy_text TEXT, seen INT DEFAULT 0, dedupe VARCHAR(160),
        UNIQUE KEY u_mtal(dedupe), INDEX idx_mtal(seen, ts)) {T}""",
]
TABLE_NAMES = ["mt_teachers", "mt_groups", "mt_schedule", "mt_lessons", "mt_attendance", "mt_settings", "mt_alerts"]
_ready = False


def ensure_schema():
    """Majlislar jadvallarini yaratadi (mavjud bo'lsa tegmaydi). Dastur ishga tushganda va birinchi so'rovda chaqiriladi."""
    global _ready
    if db.IS_MYSQL:
        from . import mysqldb
        for st in TABLES:
            mysqldb.raw(st)
    else:
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
    "host_type": "gmail",        # gmail | workspace
    "moderation": "ON",          # Meet moderatsiya (co-host boshqaruvi)
    "late_min": 10, "early_min": 10,
    "remind_a": 60, "remind_b": 10,            # guruhga eslatmalar (daqiqa oldin)
    "teacher_dm_min": 10, "group_link_min": 5,  # havola yuborish (o'qituvchiga / guruhga)
    "teacher_late_min": 5,
    "poll_sec": 45, "horizon_days": 7, "max_people": 0,
    "remind_on": 1, "report_on": 1, "report_to_group": 0,
    "admin_username": "",
}
SECRET_KEYS = {"google_client_secret", "g_refresh", "g_access"}


def get(key, default=None):
    from . import secure
    r = db.one("SELECT value FROM mt_settings WHERE key=?", (key,))
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
    db.ex("INSERT INTO mt_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def drop(key):
    db.ex("DELETE FROM mt_settings WHERE key=?", (key,))


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
_MAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def teachers(active_only=False):
    sql = "SELECT * FROM mt_teachers" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
    return [dict(r) for r in db.q(sql)]


def teacher(tid):
    r = db.one("SELECT * FROM mt_teachers WHERE id=?", (tid,)) if tid else None
    return dict(r) if r else None


def save_teacher(form: dict, tid=None) -> str | None:
    """Xato matnini qaytaradi (yoki None — saqlandi)."""
    name = (form.get("name") or "").strip()
    gmail = (form.get("gmail") or "").strip().lower()
    un = (form.get("tg_username") or "").strip().lstrip("@")
    if not name:
        return "Ism kiritilmagan"
    if gmail and not _MAIL.match(gmail):
        return "Gmail manzili noto'g'ri"
    if un and not _UN.match(un):
        return "Telegram @username noto'g'ri (5-32 ta lotin harf, raqam yoki _)"
    vals = (name, gmail, un, (form.get("phone") or "").strip(), (form.get("note") or "").strip(),
            (form.get("meet_name") or "").strip())
    if tid:
        db.ex("UPDATE mt_teachers SET name=?, gmail=?, tg_username=?, phone=?, note=?, meet_name=? WHERE id=?", vals + (tid,))
    else:
        db.ex("INSERT INTO mt_teachers(name,gmail,tg_username,phone,note,meet_name,active,created_at) VALUES(?,?,?,?,?,?,1,?)",
              vals + (fmt(now()),))
    return None


def delete_teacher(tid):
    db.ex("UPDATE mt_groups SET teacher_id=NULL WHERE teacher_id=?", (tid,))
    db.ex("UPDATE mt_schedule SET teacher_id=NULL WHERE teacher_id=?", (tid,))
    db.ex("UPDATE mt_lessons SET teacher_id=NULL WHERE teacher_id=? AND status IN ('planned','live')", (tid,))
    db.ex("DELETE FROM mt_teachers WHERE id=?", (tid,))


# ---------------------------------------------------------------- guruhlar
def groups(active_only=False):
    sql = ("SELECT g.*, t.name teacher_name FROM mt_groups g LEFT JOIN mt_teachers t ON t.id=g.teacher_id"
           + (" WHERE g.active=1" if active_only else "") + " ORDER BY g.title")
    return [dict(r) for r in db.q(sql)]


def group(gid):
    r = db.one("SELECT g.*, t.name teacher_name FROM mt_groups g LEFT JOIN mt_teachers t ON t.id=g.teacher_id WHERE g.id=?", (gid,)) if gid else None
    return dict(r) if r else None


def add_group(tg_id, title, username="", teacher_id=None) -> int:
    old = db.one("SELECT id FROM mt_groups WHERE tg_id=?", (tg_id,)) if tg_id else None
    if old:
        return old["id"]
    return db.ex("INSERT INTO mt_groups(tg_id,title,username,teacher_id,active,created_at) VALUES(?,?,?,?,1,?)",
                 (tg_id, title, username or "", teacher_id, fmt(now())))


def delete_group(gid):
    """Guruh, jadvali, darslari va davomati (Google Calendar xato bo'lsa ham) o'chiriladi."""
    ids = [r["id"] for r in db.q("SELECT id FROM mt_lessons WHERE group_id=?", (gid,))]
    for lid in ids:
        db.ex("DELETE FROM mt_attendance WHERE lesson_id=?", (lid,))
    db.ex("DELETE FROM mt_lessons WHERE group_id=?", (gid,))
    db.ex("DELETE FROM mt_schedule WHERE group_id=?", (gid,))
    db.ex("DELETE FROM mt_alerts WHERE group_id=?", (gid,))
    drop(f"alias_{gid}")
    drop(f"roster_{gid}")
    db.ex("DELETE FROM mt_groups WHERE id=?", (gid,))


# ---------------------------------------------------------------- jadval
def schedules(gid):
    return [dict(r) for r in db.q("SELECT s.*, t.name teacher_name FROM mt_schedule s LEFT JOIN mt_teachers t ON t.id=s.teacher_id "
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
        if not db.one("SELECT 1 FROM mt_schedule WHERE group_id=? AND weekday=? AND start_time=?", (gid, w, start_time)):
            db.ex("INSERT INTO mt_schedule(group_id,weekday,start_time,duration_min,teacher_id,active,created_at) VALUES(?,?,?,?,?,1,?)",
                  (gid, w, start_time, duration, teacher_id, fmt(now())))
    n = db.one("SELECT COUNT(*) c FROM mt_schedule WHERE group_id=? AND active=1", (gid,))["c"]
    if not 3 <= n <= 6:
        warns.append(f"Haftasiga {n} ta dars bor (odatda 3-6 ta bo'ladi). Agar shunday kerak bo'lsa, davom eting.")
    w60 = duration_warning(duration)
    if w60:
        warns.append(w60)
    return [], warns


def duration_warning(minutes: int) -> str:
    if cfg("host_type") == "gmail" and minutes > GMAIL_LIMIT_MIN:
        return (f"Dars {minutes} daqiqa, Gmail akkauntda Meet uchrashuvi 60 daqiqa bilan cheklanadi: "
                f"{GMAIL_LIMIT_MIN} daqiqadan keyin Meet o'zi tugatadi (Workspace akkaunt kerak).")
    return ""


# ---------------------------------------------------------------- darslar
def lesson(lid):
    r = db.one("SELECT * FROM mt_lessons WHERE id=?", (lid,)) if lid else None
    return dict(r) if r else None


def flags(les) -> dict:
    try:
        return json.loads(les.get("flags") or "{}")
    except Exception:
        return {}


def set_flag(lid, key, val):
    f = flags(lesson(lid) or {})
    f[key] = val
    db.ex("UPDATE mt_lessons SET flags=? WHERE id=?", (json.dumps(f, ensure_ascii=False), lid))


def lesson_teacher(les):
    return teacher(les.get("teacher_id")) or teacher((group(les["group_id"]) or {}).get("teacher_id"))


def elapsed_min(les) -> int:
    """«Dars qancha vaqt o'tdi»: Meet haqiqatan boshlangandan beri (davom etayotgan bo'lsa hozirgacha)."""
    if not les.get("real_start"):
        return 0
    end = parse(les["real_end"]) if les.get("real_end") else now()
    return max(0, int((end - parse(les["real_start"])).total_seconds() // 60))


def decorate(les: dict) -> dict:
    g = group(les["group_id"]) or {}
    t = lesson_teacher(les)
    s, e = parse(les["start_at"]), parse(les["end_at"])
    les.update(group_title=g.get("title", "?"), teacher_name=(t or {}).get("name", ""), day=s.strftime("%Y-%m-%d"),
               hhmm=s.strftime("%H:%M"), end_hhmm=e.strftime("%H:%M"), wd=WD_SHORT[s.weekday()],
               status_uz=STATUS_UZ.get(les["status"], les["status"]), elapsed=elapsed_min(les),
               warn=les.get("warn_text") or "", fl=flags(les))
    return les


def lessons_between(a: str, b: str, group_id=None, statuses=None):
    sql, args = "SELECT * FROM mt_lessons WHERE start_at>=? AND start_at<?", [a, b]
    if group_id:
        sql += " AND group_id=?"
        args.append(group_id)
    if statuses:
        sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
        args += list(statuses)
    return [decorate(dict(r)) for r in db.q(sql + " ORDER BY start_at", args)]


async def generate(group_id=None, provision_now=True) -> int:
    """Faol jadvallardan keyingi `horizon_days` kun uchun darslar yaratadi (mavjud bo'lsa tegmaydi)."""
    ready()
    horizon = max(1, cfg("horizon_days"))
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    created = []
    sql = ("SELECT s.* FROM mt_schedule s JOIN mt_groups g ON g.id=s.group_id WHERE s.active=1 AND g.active=1"
           + (" AND s.group_id=?" if group_id else ""))
    for s in db.q(sql, (group_id,) if group_id else ()):
        for d in range(horizon + 1):
            day = today + timedelta(days=d)
            if day.weekday() != s["weekday"]:
                continue
            start = datetime.strptime(f"{day:%Y-%m-%d} {s['start_time']}", "%Y-%m-%d %H:%M")
            if start < now() - timedelta(minutes=5):
                continue
            slot = f"{day:%Y-%m-%d}"
            if db.one("SELECT 1 FROM mt_lessons WHERE schedule_id=? AND slot_day=?", (s["id"], slot)):
                continue
            end = start + timedelta(minutes=s["duration_min"])
            lid = db.ex("INSERT INTO mt_lessons(group_id,schedule_id,slot_day,teacher_id,start_at,end_at,duration_min,status,flags,created_at) "
                        "VALUES(?,?,?,?,?,?,?,'planned','{}',?)",
                        (s["group_id"], s["id"], slot, s["teacher_id"], fmt(start), fmt(end), s["duration_min"], fmt(now())))
            created.append(lid)
    if provision_now:
        for lid in created:
            await provision(lid)
    return len(created)


def add_extra_lesson(gid, start: datetime, duration: int, teacher_id=None) -> int:
    end = start + timedelta(minutes=duration)
    return db.ex("INSERT INTO mt_lessons(group_id,schedule_id,slot_day,teacher_id,start_at,end_at,duration_min,status,flags,created_at) "
                 "VALUES(?,NULL,?,?,?,?,?,'planned','{}',?)",
                 (gid, f"{start:%Y-%m-%d}", teacher_id, fmt(start), fmt(end), duration, fmt(now())))


# ---------------------------------------------------------------- Google: Meet xonasi + Calendar
def _event_body(les, grp, t) -> dict:
    s, e = parse(les["start_at"]), parse(les["end_at"])
    desc = [f"Meet havolasi: {les['meeting_uri']}"] if les.get("meeting_uri") else []
    if t:
        desc.append(f"O'qituvchi: {t['name']}")
    desc.append("Telegram Group Post · Majlislar")
    return {"summary": f"{grp['title']} — dars", "description": "\n".join(desc), "location": les.get("meeting_uri") or "",
            "start": {"dateTime": s.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tashkent"},
            "end": {"dateTime": e.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tashkent"},
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]}}


async def ensure_calendar(grp) -> str:
    """Har Telegram guruh uchun alohida Google Calendar."""
    if grp.get("calendar_id"):
        return grp["calendar_id"]
    cal = await G.create_calendar(f"{grp['title']} — darslar")
    db.ex("UPDATE mt_groups SET calendar_id=? WHERE id=?", (cal["id"], grp["id"]))
    grp["calendar_id"] = cal["id"]
    t = teacher(grp.get("teacher_id"))
    if t and t.get("gmail"):
        try:
            await G.share_calendar(cal["id"], t["gmail"], "reader")
        except G.GoogleError as e:
            log.info("Kalendarni o'qituvchi bilan ulashib bo'lmadi: %s", e)
    return cal["id"]


async def provision(lid) -> tuple[bool, str]:
    """Darsga yangi Meet xonasi (havola ochiq), o'qituvchini co-host qilish va kalendar yozuvi."""
    les = lesson(lid)
    if not les or les["status"] in ("cancelled", "done", "missed"):
        return False, "Dars topilmadi yoki yakunlangan"
    if not G.connected():
        return False, "Google ulanmagan"
    grp = group(les["group_id"])
    t = lesson_teacher(les)
    warns = []
    try:
        if not les.get("space_name"):
            sp = await G.create_space(cfg("moderation"))
            db.ex("UPDATE mt_lessons SET space_name=?, meeting_uri=?, meeting_code=? WHERE id=?",
                  (sp["name"], sp.get("meetingUri", ""), sp.get("meetingCode", ""), lid))
            les.update(space_name=sp["name"], meeting_uri=sp.get("meetingUri", ""))
            if t and t.get("gmail"):
                try:
                    await G.add_cohost(sp["name"], t["gmail"])
                    db.ex("UPDATE mt_lessons SET cohost_ok=1 WHERE id=?", (lid,))
                except G.GoogleError as e:
                    warns.append(f"O'qituvchi ({t['gmail']}) co-host qilinmadi: {e.short()}")
            elif t:
                warns.append("O'qituvchida Gmail kiritilmagan: co-host qo'shilmadi")
            else:
                warns.append("Dars uchun o'qituvchi tanlanmagan")
        w60 = duration_warning(les["duration_min"])
        if w60:
            warns.append(w60)
        if not les.get("cal_event_id"):
            cid = await ensure_calendar(grp)
            ev = await G.insert_event(cid, _event_body(les, grp, t))
            db.ex("UPDATE mt_lessons SET cal_event_id=? WHERE id=?", (ev["id"], lid))
        db.ex("UPDATE mt_lessons SET warn_text=?, prov_err=NULL, prov_at=? WHERE id=?", ("\n".join(warns), fmt(now()), lid))
        return True, "Tayyor"
    except G.GoogleError as e:
        db.ex("UPDATE mt_lessons SET prov_err=?, prov_at=? WHERE id=?", (e.full()[:900], fmt(now()), lid))
        add_alert("provision", f"«{grp['title']}» {les['start_at'][:16]} darsi uchun Meet/Kalendar yaratilmadi: {e.short()}",
                  lesson_id=lid, group_id=grp["id"], dedupe=f"prov:{lid}:{(les.get('prov_at') or '')[:13]}")
        return False, e.short()


async def retry_provision():
    """Havolasi yo'q yoki kalendarga yozilmagan yaqin darslarni 10 daqiqada bir urinib ko'radi."""
    if not G.connected():
        return
    lim = fmt(now() + timedelta(days=max(1, cfg("horizon_days")) + 1))
    for r in db.q("SELECT id, prov_at FROM mt_lessons WHERE status='planned' AND (space_name IS NULL OR space_name='' OR cal_event_id IS NULL OR cal_event_id='') "
                  "AND start_at>? AND start_at<?", (fmt(now()), lim)):
        if r["prov_at"] and now() - parse(r["prov_at"]) < timedelta(minutes=10):
            continue
        await provision(r["id"])


async def cancel_lesson(lid, reason="") -> str:
    les = lesson(lid)
    if not les or les["status"] in ("cancelled", "done"):
        return "Bu darsni bekor qilib bo'lmaydi"
    grp, t = group(les["group_id"]), lesson_teacher(les)
    db.ex("UPDATE mt_lessons SET status='cancelled', cancel_reason=? WHERE id=?", (reason.strip(), lid))
    cal_note = ""
    if les.get("cal_event_id") and grp.get("calendar_id") and G.connected():
        try:
            await G.delete_event(grp["calendar_id"], les["cal_event_id"])
            db.ex("UPDATE mt_lessons SET cal_event_id=NULL WHERE id=?", (lid,))
        except G.GoogleError as e:
            cal_note = f" (kalendar yangilanmadi: {e.short()})"
    s = parse(les["start_at"])
    text = f"❌ {s:%d.%m} kuni soat {s:%H:%M} dagi dars bekor qilindi." + (f"\nSabab: {reason.strip()}" if reason.strip() else "")
    await notify_lesson_people(les, grp, t, text, "cancel")
    return "Dars bekor qilindi" + cal_note


async def move_lesson(lid, new_start: datetime, duration=None) -> str:
    les = lesson(lid)
    if not les or les["status"] in ("cancelled", "done", "live"):
        return "Bu darsni ko'chirib bo'lmaydi"
    grp, t = group(les["group_id"]), lesson_teacher(les)
    dur = int(duration or les["duration_min"])
    old = parse(les["start_at"])
    end = new_start + timedelta(minutes=dur)
    db.ex("UPDATE mt_lessons SET start_at=?, end_at=?, duration_min=?, status='planned', moved_from=?, flags='{}' WHERE id=?",
          (fmt(new_start), fmt(end), dur, les.get("moved_from") or fmt(old), lid))
    les = lesson(lid)
    cal_note = ""
    w60 = duration_warning(dur)
    db.ex("UPDATE mt_lessons SET warn_text=? WHERE id=?", (w60, lid))
    if les.get("cal_event_id") and grp.get("calendar_id") and G.connected():
        try:
            await G.patch_event(grp["calendar_id"], les["cal_event_id"], _event_body(les, grp, t))
        except G.GoogleError as e:
            cal_note = f" (kalendar yangilanmadi: {e.short()})"
    elif G.connected():
        await provision(lid)
    text = (f"🔄 Dars ko'chirildi: {old:%d.%m %H:%M} → {new_start:%d.%m %H:%M}."
            + (f"\n🔗 Havola o'sha: {les['meeting_uri']}" if les.get("meeting_uri") else ""))
    await notify_lesson_people(les, grp, t, text, "move")
    return "Dars ko'chirildi" + (" · " + w60 if w60 else "") + cal_note


# ---------------------------------------------------------------- Telegram
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
        raise TgFail("Majlislar uchun Telegram akkaunt ulanmagan")
    try:
        c = await manager.get(acc["id"])._get_client()
        if not await c.is_user_authorized():
            raise TgFail("Majlislar Telegram akkaunti ulanmagan (Akkaunt sahifasida qayta ulang)")
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
    """Majlislar akkaunti a'zo bo'lgan guruhlar (tanlash uchun)."""
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


def add_alert(kind, text, level="warn", lesson_id=None, group_id=None, copy=None, dedupe=None) -> int | None:
    """Ogohlantirishni yozadi (dedupe bo'yicha takrorlanmaydi). Yangi yozilsa id qaytaradi."""
    ready()
    if dedupe and db.one("SELECT 1 FROM mt_alerts WHERE dedupe=?", (dedupe,)):
        return None
    aid = db.ex("INSERT INTO mt_alerts(ts,kind,level,lesson_id,group_id,text,copy_text,seen,dedupe) VALUES(?,?,?,?,?,?,?,0,?)",
                (fmt(now()), kind, level, lesson_id, group_id, text[:1500], copy, dedupe))
    try:
        notify.event("warn" if level != "info" else "info", "majlis", text)
        notify.toast("Majlislar", text)
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


async def _deliver(les, grp, target, text, flag, who) -> bool:
    """Xabarni yuboradi; yetmasa adminga ogohlantirish + havolani qo'lda nusxalash uchun matn saqlanadi."""
    try:
        await tg_send(target, text)
        set_flag(les["id"], flag, fmt(now()))
        return True
    except TgFail as e:
        set_flag(les["id"], flag, "fail " + fmt(now()))
        await alert("send_fail", f"«{grp['title']}» {les['start_at'][11:16]} darsi: {who}ga xabar yetmadi — {e}. Havolani qo'lda yuboring.",
                    level="error", lesson_id=les["id"], group_id=grp["id"], copy=text, dedupe=f"sf:{les['id']}:{flag}")
        return False


async def notify_lesson_people(les, grp, t, text, kind):
    """Bekor qilish / ko'chirish: guruhga va o'qituvchiga."""
    if grp.get("tg_id"):
        try:
            await tg_send(int(grp["tg_id"]), text)
        except TgFail as e:
            await alert("send_fail", f"«{grp['title']}» guruhiga xabar yetmadi ({kind}): {e}", level="error", lesson_id=les["id"],
                        group_id=grp["id"], copy=text, dedupe=f"sf:{les['id']}:{kind}:g")
    if t and t.get("tg_username"):
        try:
            await tg_send("@" + t["tg_username"], text)
        except TgFail as e:
            await alert("send_fail", f"O'qituvchi {t['name']} ga xabar yetmadi ({kind}): {e}", level="error", lesson_id=les["id"],
                        group_id=grp["id"], copy=text, dedupe=f"sf:{les['id']}:{kind}:t")


def msg_teacher_link(les, grp, mins):
    when = f"{mins} daqiqadan keyin" if mins > 0 else "boshlandi"
    return (f"📚 Dars {when}: {grp['title']}\n🕒 {les['start_at'][11:16]}–{les['end_at'][11:16]}\n🔗 {les['meeting_uri']}\n\n"
            f"Begona kirsa, Meet ichida o'zingiz chiqarasiz.")


def msg_group_link(les, grp, t, mins):
    when = f"{mins} daqiqadan keyin boshlanadi" if mins > 0 else "boshlandi"
    return (f"📚 Dars {when}.\n🕒 {les['start_at'][11:16]} ({les['duration_min']} daqiqa)"
            + (f"\n👩‍🏫 O'qituvchi: {t['name']}" if t else "") + f"\n🔗 Kirish: {les['meeting_uri']}")


def msg_group_remind(les, mins):
    return f"⏰ Eslatma: soat {les['start_at'][11:16]} da dars bor ({mins} daqiqadan keyin)."


async def send_now(lid, who: str) -> str:
    """Qo'lda (qayta) yuborish: who = 'teacher' | 'group'."""
    les = lesson(lid)
    if not les or not les.get("meeting_uri"):
        return "Dars uchun havola hali yo'q"
    grp, t = group(les["group_id"]), lesson_teacher(les)
    mins = int((parse(les["start_at"]) - now()).total_seconds() // 60)
    if who == "teacher":
        if not t or not t.get("tg_username"):
            return "O'qituvchining Telegram @username si kiritilmagan"
        ok = await _deliver(les, grp, "@" + t["tg_username"], msg_teacher_link(les, grp, mins), "t_link", "o'qituvchi")
    else:
        if not grp.get("tg_id"):
            return "Guruhning Telegram ID si yo'q"
        ok = await _deliver(les, grp, int(grp["tg_id"]), msg_group_link(les, grp, t, mins), "g_link", "guruh")
    return "Yuborildi" if ok else "Yuborilmadi (Ogohlantirishlar sahifasiga qarang)"


# ---------------------------------------------------------------- eslatma sikli
async def process_reminders():
    """Havola yuborish (o'qituvchiga -10, guruhga -5), guruh eslatmalari (-60, -10) va o'qituvchi kechikishi."""
    t_now = now()
    a, b = cfg("remind_a"), cfg("remind_b")
    horizon = fmt(t_now + timedelta(minutes=max(a, b, cfg("teacher_dm_min"), cfg("group_link_min")) + 2))
    rows = db.q("SELECT * FROM mt_lessons WHERE status IN ('planned','live') AND start_at<=? AND end_at>=?", (horizon, fmt(t_now)))
    for r in rows:
        les = dict(r)
        fl = flags(les)
        grp = group(les["group_id"])
        if not grp or not grp.get("active"):
            continue
        t = lesson_teacher(les)
        mins = (parse(les["start_at"]) - t_now).total_seconds() / 60
        if les.get("meeting_uri"):
            if mins <= cfg("teacher_dm_min") and "t_link" not in fl:
                if t and t.get("tg_username"):
                    await _deliver(les, grp, "@" + t["tg_username"], msg_teacher_link(les, grp, max(0, round(mins))), "t_link", "o'qituvchi")
                else:
                    set_flag(les["id"], "t_link", "yo'q")
                    await alert("send_fail", f"«{grp['title']}» {les['start_at'][11:16]} darsi: o'qituvchining @username si yo'q, havola yuborilmadi.",
                                level="error", lesson_id=les["id"], group_id=grp["id"], copy=msg_teacher_link(les, grp, max(0, round(mins))),
                                dedupe=f"sf:{les['id']}:t_link")
            if mins <= cfg("group_link_min") and "g_link" not in fl and grp.get("tg_id"):
                await _deliver(les, grp, int(grp["tg_id"]), msg_group_link(les, grp, t, max(0, round(mins))), "g_link", "guruh")
        elif mins <= cfg("teacher_dm_min") and "no_link" not in fl:
            set_flag(les["id"], "no_link", fmt(t_now))
            await alert("provision", f"«{grp['title']}» {les['start_at'][11:16]} darsi boshlanishiga {max(0, round(mins))} daqiqa, lekin Meet havolasi yo'q"
                        + (f" ({les.get('prov_err')})" if les.get("prov_err") else " (Google ulanmagan bo'lishi mumkin)") + ".",
                        level="error", lesson_id=les["id"], group_id=grp["id"], dedupe=f"nolink:{les['id']}")
        if cfg("remind_on") and grp.get("tg_id") and mins > 0:
            if "g_b" not in fl and mins <= b:
                await _deliver(les, grp, int(grp["tg_id"]), msg_group_remind(les, max(1, round(mins))), "g_b", "guruh")
                if "g_a" not in fl:
                    set_flag(les["id"], "g_a", "o'tkazildi")
            elif "g_a" not in fl and mins <= a and mins > b:
                await _deliver(les, grp, int(grp["tg_id"]), msg_group_remind(les, max(1, round(mins))), "g_a", "guruh")
        late = -mins
        if late >= cfg("teacher_late_min") and "t_late" not in fl and "t_in" not in fl and les["status"] in ("planned", "live"):
            set_flag(les["id"], "t_late", fmt(t_now))
            who = t["name"] if t else "O'qituvchi"
            await alert("teacher_late", f"«{grp['title']}» darsi {les['start_at'][11:16]} da boshlanishi kerak edi, {who} {int(late)} daqiqadan beri Meet'da yo'q.",
                        level="error", lesson_id=les["id"], group_id=grp["id"], dedupe=f"tl:{les['id']}")


async def loop():
    """Fon sikli (supervise ostida): darslarni oldindan yaratish, havola va eslatmalar."""
    await asyncio.sleep(15)
    last_gen = datetime.min
    while True:
        try:
            ready()
            if (now() - last_gen) > timedelta(minutes=30):
                await generate()
                last_gen = now()
            await retry_provision()
            await process_reminders()
            put("sched_beat", fmt(now()))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("meet_sched.loop")
            notify.event("error", "majlis", "Majlislar eslatma siklida xato (loglarga qarang)")
        await asyncio.sleep(30)


# ---------------------------------------------------------------- Tizim tahlili uchun
def health_items() -> list[dict]:
    """syscheck.run_all() ga bitta chaqiruv bilan qo'shiladi."""
    ready()
    g = "Majlislar (Google Meet)"

    def it(title, state, detail="", fix="", key=None):
        return {"group": g, "title": title, "state": state, "detail": detail, "fix": fix, "key": key or title}
    out = []
    st = get("g_state", "none")
    if not get("google_client_id"):
        out.append(it("Google ulanishi", "info", "Sozlanmagan", "Majlislar → Sozlamalar: OAuth Client ID/Secret kiriting (Yo'riqnomaga qarang).", "meet_google"))
    elif st == "ok":
        exp = float(get("g_exp", "0") or 0)
        out.append(it("Google token", "ok", f"{get('g_email', '?')} ulangan; ulangan sana {str(get('g_connected_at', ''))[:16]}; "
                      f"access token {'amal qiladi' if exp > datetime.now().timestamp() else 'keyingi so`rovda yangilanadi'}", key="meet_token"))
    elif st == "expired":
        out.append(it("Google token", "fail", "Token eskirgan yoki bekor qilingan (invalid_grant)",
                      "Majlislar → Sozlamalar → «Google'ga ulash». OAuth ilovasi «Testing» holatida bo'lsa token 7 kunda o'chadi: Production'ga o'tkazing.", "meet_token"))
    else:
        out.append(it("Google token", "warn", "Google'ga ulanmagan", "Majlislar → Sozlamalar → «Google'ga ulash».", "meet_token"))
    host = cfg("host_type")
    out.append(it("Meet limiti", "info" if host == "gmail" else "ok",
                  "Meet limiti: 60 daqiqa (Gmail)" if host == "gmail" else "Workspace: 60 daqiqalik Gmail cheklovi yo'q (tarifga qarang)", key="meet_limit"))
    long_sched = db.one("SELECT COUNT(*) c FROM mt_schedule WHERE active=1 AND duration_min>?", (GMAIL_LIMIT_MIN,))["c"]
    if host == "gmail" and long_sched:
        out.append(it("60 daqiqadan uzun darslar", "warn", f"{long_sched} ta jadval qatori 60 daqiqadan uzun",
                      "Gmail'da Meet 60 daqiqada tugaydi. Davomiylikni qisqartiring yoki Workspace akkauntga o'ting.", "meet_long"))
    last = get("last_poll_at")
    live = db.one("SELECT COUNT(*) c FROM mt_lessons WHERE status='live'")["c"]
    soon = db.q("SELECT l.start_at, g.title FROM mt_lessons l JOIN mt_groups g ON g.id=l.group_id WHERE l.status IN ('planned','live') AND l.start_at<? AND l.end_at>? ORDER BY l.start_at LIMIT 5",
                (fmt(now() + timedelta(hours=24)), fmt(now())))
    stale = bool(last and live and now() - parse(last) > timedelta(seconds=max(180, cfg("poll_sec") * 4)))
    out.append(it("Oxirgi polling", "warn" if stale else "ok" if last else "info",
                  f"{last[:19]}" + (f", xato: {get('last_poll_err')}" if get("last_poll_err") else "") if last else "Hali dars bo'lmagan",
                  "Fon jarayoni to'xtagan bo'lishi mumkin: dasturni qayta ishga tushiring." if stale else "", "meet_poll"))
    errs = int(get("poll_err_24", "0") or 0)
    out.append(it("Polling xatolari (24 soat)", "ok" if errs == 0 else "warn" if errs < 10 else "fail",
                  f"{errs} ta" + (f"; oxirgisi: {get('last_poll_err')}" if get("last_poll_err") else ""),
                  "" if errs == 0 else "Google ulanishi va Meet API yoqilganini tekshiring (Majlislar → Google diagnostika).", "meet_perr"))
    out.append(it("Faol majlislar", "info", f"hozir davom etayotgan: {live}; 24 soat ichida: {len(soon)}"
                  + ("; " + ", ".join(f"{s['title']} {s['start_at'][11:16]}" for s in soon) if soon else ""), key="meet_live"))
    unseen = db.one("SELECT COUNT(*) c FROM mt_alerts WHERE seen=0 AND kind='send_fail'")["c"]
    if unseen:
        out.append(it("Yetmagan xabarlar", "warn", f"{unseen} ta xabar Telegramga yetmagan", "Majlislar → Ogohlantirishlar: havolani qo'lda nusxalab yuboring.", "meet_sendfail"))
    if not meet_account():
        out.append(it("Telegram akkaunt (Majlislar)", "info", "Ulanmagan", "Majlislar → Akkaunt.", "meet_tg"))
    else:
        from .core import manager
        svc = manager.services.get(meet_account()["id"])
        out.append(it("Telegram akkaunt (Majlislar)", "ok" if svc and svc.info else "fail", f"+{svc.info['phone']}" if svc and svc.info else "Ulanmagan",
                      "" if svc and svc.info else "Majlislar → Akkaunt: qayta ulang.", "meet_tg"))
    try:
        from . import syscheck
        beats = {r["name"]: r for r in db.q("SELECT * FROM sys_beat WHERE name IN ('meet_sched','meet_track')")}
        for nm, title in (("meet_sched", "Majlislar: eslatmalar"), ("meet_track", "Majlislar: davomat kuzatuvi")):
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
