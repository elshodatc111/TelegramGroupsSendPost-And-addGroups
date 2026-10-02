"""Telegram SMM: ma'lumotlarga kirish yordamchilari (Telegram Guruhlar jadvallariga tegmaydi)."""
import json
import shutil

from . import db
from .config import MEDIA_DIR, log

PROFILE_FIELDS = ("type_key", "purpose", "audience", "lang", "tone", "post_freq", "cta", "bans", "lead_url", "lead_text",
                  "video_format", "samples", "brand_name", "phones", "address", "landmark", "contacts", "img_style")
COLOR_FIELDS = ("color1", "color2", "color3")
DEFAULT_COLORS = ("#0F766E", "#F59E0B", "#111827")
_HEX = __import__("re").compile(r"^#[0-9A-Fa-f]{6}$")
LANGS = {"uz": "O'zbekcha (lotin)", "uz_cyrl": "O'zbekcha (kirill)", "ru": "Ruscha", "mixed": "Aralash (uz + ru)"}
BIZ_FIELDS = (("product", "Mahsulot / xizmat"), ("price", "Narx oralig'i"), ("pains", "Mijozning og'riqli nuqtalari"),
              ("objections", "Odatiy e'tirozlar"), ("offer", "Asosiy taklif / aksiya"), ("usp", "Raqobatchilardan farqi (USP)"),
              ("geo", "Hudud / auditoriya joylashuvi"))


def now():
    return db.now()


def log_event(channel_id, kind, info=""):
    db.ex("INSERT INTO ch_log(channel_id,ts,kind,info) VALUES(?,?,?,?)", (channel_id or 0, now(), kind, str(info)[:2000]))


# ---------------------------------------------------------------- akkaunt
def channel_account():
    """Telegram SMM bo'limidagi yagona akkaunt (yo'q bo'lsa None)."""
    return db.one("SELECT * FROM accounts WHERE workspace='channels' ORDER BY id LIMIT 1")


# ---------------------------------------------------------------- kanallar
def channels(status="active"):
    if status:
        return db.q("SELECT * FROM ch_channels WHERE status=? ORDER BY title", (status,))
    return db.q("SELECT * FROM ch_channels ORDER BY status, title")


def get(cid):
    return db.one("SELECT * FROM ch_channels WHERE id=?", (cid,)) if cid else None


def biz(ch) -> dict:
    try:
        return json.loads(ch["biz_json"] or "{}")
    except Exception:
        return {}


def rubrics(ch) -> list[str]:
    try:
        v = json.loads(ch["rubrics_json"] or "[]")
        if v:
            return v
    except Exception:
        pass
    t = db.one("SELECT rubrics_json FROM ch_types WHERE `key`=?", (ch["type_key"],)) if ch["type_key"] else None
    try:
        return json.loads(t["rubrics_json"]) if t else []
    except Exception:
        return []


def types():
    return db.q("SELECT * FROM ch_types ORDER BY builtin DESC, title")


def save_profile(cid, form: dict):
    sets, args = [], []
    for f in PROFILE_FIELDS:
        if f in form:
            sets.append(f"{f}=?")
            args.append((form[f] or "").strip())
    for i, f in enumerate(COLOR_FIELDS):
        v = (form.get(f) or "").strip()
        sets.append(f"{f}=?")
        args.append(v if _HEX.match(v) else DEFAULT_COLORS[i])
    sets.append("track_on=?")
    args.append(1 if form.get("track_on") else 0)
    b = {k: (form.get("biz_" + k) or "").strip() for k, _ in BIZ_FIELDS}
    sets.append("biz_json=?")
    args.append(json.dumps(b, ensure_ascii=False))
    rub = [x.strip() for x in (form.get("rubrics") or "").splitlines() if x.strip()]
    sets.append("rubrics_json=?")
    args.append(json.dumps(rub, ensure_ascii=False))
    db.ex(f"UPDATE ch_channels SET {', '.join(sets)} WHERE id=?", args + [cid])


def apply_type_defaults(cid, type_key):
    """Tur tanlanganda bo'sh maydonlar tur presetidan to'ldiriladi."""
    t = db.one("SELECT * FROM ch_types WHERE `key`=?", (type_key,))
    ch = get(cid)
    if not t or not ch:
        return
    if not (ch["tone"] or "").strip():
        db.ex("UPDATE ch_channels SET tone=? WHERE id=?", (t["tone"], cid))
    if not (ch["post_freq"] or "").strip():
        db.ex("UPDATE ch_channels SET post_freq=? WHERE id=?", (t["freq"], cid))
    if not json.loads(ch["rubrics_json"] or "[]"):
        db.ex("UPDATE ch_channels SET rubrics_json=? WHERE id=?", (t["rubrics_json"], cid))
    if not (ch["purpose"] or "").strip():
        db.ex("UPDATE ch_channels SET purpose=? WHERE id=?", (t["description"], cid))


def attach(account_id, info: dict) -> int:
    """Kanalni biriktiradi (yoki arxivdan qaytaradi). info: tg_id,title,username,members,is_creator,rights"""
    row = db.one("SELECT id FROM ch_channels WHERE account_id=? AND tg_id=?", (account_id, info["tg_id"]))
    rights = json.dumps(info.get("rights") or {}, ensure_ascii=False)
    if row:
        db.ex("UPDATE ch_channels SET title=?,username=?,members=?,is_creator=?,rights_json=?,status='active',archived_at=NULL,synced_at=? WHERE id=?",
              (info["title"], info.get("username"), info.get("members"), int(bool(info.get("is_creator"))), rights, now(), row["id"]))
        return row["id"]
    return db.ex("INSERT INTO ch_channels(account_id,tg_id,title,username,members,is_creator,rights_json,status,created_at,synced_at,lang) "
                 "VALUES(?,?,?,?,?,?,?,'active',?,?,'uz')",
                 (account_id, info["tg_id"], info["title"], info.get("username"), info.get("members"),
                  int(bool(info.get("is_creator"))), rights, now(), now()))


def archive(cid):
    db.ex("UPDATE ch_channels SET status='archived', archived_at=? WHERE id=?", (now(), cid))
    db.ex("UPDATE ch_plan SET status='cancelled' WHERE channel_id=? AND status IN ('scheduled','draft')", (cid,))
    log_event(cid, "archive", "Kanal arxivlandi")


def restore(cid):
    db.ex("UPDATE ch_channels SET status='active', archived_at=NULL WHERE id=?", (cid,))


def delete_channel(cid):
    """Kanal va unga tegishli barcha ma'lumotlar o'chadi. Faqat AI sarfi (hisob-kitob uchun) saqlanadi."""
    for t in ("ch_posts", "ch_competitors", "ch_members_log", "ch_plan", "ch_ideas", "ch_memory", "ch_log", "ch_links",
              "ch_clicks", "ch_daily", "ch_official", "ch_audience", "ch_alerts", "ch_ab", "ch_comments", "ch_month",
              "ch_reports", "ch_images"):
        db.ex(f"DELETE FROM {t} WHERE channel_id=?", (cid,))
    db.ex("DELETE FROM ch_channels WHERE id=?", (cid,))
    shutil.rmtree(MEDIA_DIR / "ch" / str(cid), ignore_errors=True)


def delete_account_data(account_id):
    for r in db.q("SELECT id FROM ch_channels WHERE account_id=?", (account_id,)):
        delete_channel(r["id"])


# ---------------------------------------------------------------- raqobatchilar
def competitors(cid, active_only=True):
    sql = "SELECT * FROM ch_competitors WHERE channel_id=?" + (" AND active=1" if active_only else "") + " ORDER BY title"
    return db.q(sql, (cid,))


def ensure_memory(cid):
    db.ex("INSERT OR IGNORE INTO ch_memory(channel_id,memo,version) VALUES(?,?,0)", (cid, ""))
    return db.one("SELECT * FROM ch_memory WHERE channel_id=?", (cid,))


def colors(ch) -> list[str]:
    return [(ch[f] if ch[f] and _HEX.match(ch[f]) else DEFAULT_COLORS[i]) for i, f in enumerate(COLOR_FIELDS)]


async def save_logo(cid, f) -> str | None:
    """Kanal logotipi (png/jpg/webp) -> data/media/ch/<kanal>/logo.<ext>"""
    from pathlib import Path
    if not getattr(f, "filename", None):
        return None
    ext = Path(f.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        return None
    dest = MEDIA_DIR / "ch" / str(cid)
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("logo.*"):
        old.unlink(missing_ok=True)
    data = await f.read()
    if len(data) > 5 * 1024 * 1024:
        return None
    (dest / f"logo{ext}").write_bytes(data)
    name = f"ch/{cid}/logo{ext}"
    db.ex("UPDATE ch_channels SET logo=? WHERE id=?", (name, cid))
    return name


def lead_footer(ch) -> str:
    """Har bir post tavsifiga qo'shiladigan lead platformasi (kanal profilidan)."""
    text = (ch["lead_text"] or "").strip()
    url = (ch["lead_url"] or "").strip()
    if text and url and url not in text:
        return f"{text}\n{url}"
    return text or url


# ---------------------------------------------------------------- media yuklash (Telegram SMM uchun alohida papka)
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VID_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


async def save_uploads(cid, files) -> tuple[list[str], str | None]:
    """Yuklangan fayllarni data/media/ch/<kanal>/ ga saqlaydi. (nomlar ro'yxati, media_type) qaytaradi.
    Telegram Guruhlar media kutubxonasi bilan aralashmasligi uchun alohida papka."""
    import uuid
    from pathlib import Path
    dest = MEDIA_DIR / "ch" / str(cid)
    dest.mkdir(parents=True, exist_ok=True)
    names, kinds = [], set()
    for f in files:
        if not getattr(f, "filename", None):
            continue
        ext = Path(f.filename).suffix.lower()
        kind = "image" if ext in IMG_EXT else "video" if ext in VID_EXT else None
        if not kind:
            continue
        name = f"{uuid.uuid4().hex[:12]}{ext}"
        with open(dest / name, "wb") as out:
            while chunk := await f.read(1024 * 1024):
                out.write(chunk)
        names.append(f"ch/{cid}/{name}")
        kinds.add(kind)
    if not names:
        return [], None
    if len(names) > 10:
        names = names[:10]
    mtype = "video" if kinds == {"video"} else ("image" if kinds == {"image"} else "album")
    return names, mtype
