"""Instagram: akkauntlar, sozlamalar, fayl yuklash (boshqa bo'limlarga tegmaydi)."""
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from . import cf, db, secure
from .config import MEDIA_DIR, log

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VID_EXT = {".mp4", ".mov", ".m4v"}
LANGS = {"uz": "O'zbekcha (lotin)", "ru": "Ruscha", "mixed": "Aralash (uz + ru)"}
MTYPES = {"IMAGE": "Rasm (post)", "REELS": "Reels (video)", "CAROUSEL": "Karusel (2-10 rasm/video)", "STORIES": "Stories"}
MODE_LABELS = {"reminder": "Eslatma (qo'lda joylash)", "auto": "Avto (cloudflared orqali)"}


def log_event(aid, kind, info=""):
    db.ex("INSERT INTO ig_log(account_id,ts,kind,info) VALUES(?,?,?,?)", (aid or 0, db.now(), kind, str(info)[:2000]))


# ---------------------------------------------------------------- sozlamalar
def setting(key, default=None):
    return db.get_setting(key, default)


def publish_mode() -> str:
    return "auto" if setting("ig_publish_mode", "reminder") == "auto" else "reminder"


def remind_min() -> int:
    try:
        return max(0, min(240, int(setting("ig_remind_min", 10) or 10)))
    except ValueError:
        return 10


def max_per_day() -> int:
    try:
        return max(1, min(10, int(setting("ig_max_per_day", 1) or 1)))
    except ValueError:
        return 1


def app_conf() -> dict:
    return {"app_id": setting("ig_app_id", "") or "", "secret": setting("ig_app_secret", "") or "",
            "redirect": setting("ig_redirect", "") or "http://localhost:8000/ig/oauth/callback"}


# ---------------------------------------------------------------- akkauntlar
def accounts():
    return db.q("SELECT * FROM ig_accounts ORDER BY username")


def get(aid):
    return db.one("SELECT * FROM ig_accounts WHERE id=?", (aid,)) if aid else None


def token(acc) -> str | None:
    return secure.dec(acc["token"]) if acc and acc["token"] else None


def upsert_account(prof: dict, tok: str, expires: str | None) -> int:
    uid = str(prof.get("user_id") or prof.get("id"))
    row = db.one("SELECT id FROM ig_accounts WHERE ig_user_id=?", (uid,))
    vals = (prof.get("username"), prof.get("name"), prof.get("account_type"), prof.get("biography"), prof.get("website"),
            int(prof.get("followers_count") or 0), int(prof.get("follows_count") or 0), int(prof.get("media_count") or 0),
            prof.get("profile_picture_url"), secure.enc(tok), expires, db.now())
    if row:
        db.ex("UPDATE ig_accounts SET username=?,name=?,account_type=?,bio=?,website=?,followers=?,follows=?,media_count=?,picture=?,"
              "token=?,token_expires=?,token_at=?,status='active',last_error=NULL WHERE id=?", vals + (row["id"],))
        return row["id"]
    return db.ex("INSERT INTO ig_accounts(username,name,account_type,bio,website,followers,follows,media_count,picture,token,token_expires,"
                 "token_at,ig_user_id,created_at,lang) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals + (uid, db.now(), "uz"))


def save_profile(aid, form: dict):
    db.ex("UPDATE ig_accounts SET niche=?, audience=?, tone=?, offer=?, lang=?, extra=? WHERE id=?",
          ((form.get("niche") or "").strip(), (form.get("audience") or "").strip(), (form.get("tone") or "").strip(),
           (form.get("offer") or "").strip(), form.get("lang") if form.get("lang") in LANGS else "uz",
           (form.get("extra") or "").strip(), aid))


def delete_account(aid):
    """Akkauntni va unga tegishli hamma ma'lumotni (rejalar, statistika, AI natijalar, fayllar) o'chiradi."""
    for t in ("ig_daily", "ig_media", "ig_demo", "ig_plan", "ig_ideas", "ig_reports", "ig_log"):
        db.ex(f"DELETE FROM {t} WHERE account_id=?", (aid,))
    db.ex("DELETE FROM ch_usage WHERE channel_id=?", (-aid,))
    db.ex("DELETE FROM ig_accounts WHERE id=?", (aid,))
    shutil.rmtree(MEDIA_DIR / "ig" / str(aid), ignore_errors=True)


def token_days(acc) -> int | None:
    if not acc or not acc["token_expires"]:
        return None
    try:
        return (datetime.strptime(acc["token_expires"], "%Y-%m-%d %H:%M:%S") - datetime.now()).days
    except ValueError:
        return None


def profile_complete(acc) -> bool:
    return bool((acc["niche"] or "").strip())


# ---------------------------------------------------------------- fayl yuklash
def media_list(item) -> list[str]:
    try:
        return json.loads(item["media_json"] or "[]")
    except Exception:
        return []


def media_path(name: str) -> Path:
    return MEDIA_DIR / name


def _to_jpeg(path: Path) -> Path:
    """Instagram faqat JPEG rasm qabul qiladi: PNG/WEBP ni JPEG'ga o'giramiz."""
    if path.suffix.lower() in (".jpg", ".jpeg"):
        return path
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        out = path.with_suffix(".jpg")
        im.save(out, "JPEG", quality=92)
        path.unlink(missing_ok=True)
        return out
    except Exception:
        log.warning("Rasmni JPEG'ga o'girib bo'lmadi: %s", path, exc_info=True)
        return path


async def save_uploads(aid, files) -> list[str]:
    dest = MEDIA_DIR / "ig" / str(aid)
    dest.mkdir(parents=True, exist_ok=True)
    names = []
    for f in files:
        if not getattr(f, "filename", None):
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in IMG_EXT | VID_EXT:
            continue
        p = dest / f"{uuid.uuid4().hex[:12]}{ext}"
        with open(p, "wb") as out:
            while chunk := await f.read(1024 * 1024):
                out.write(chunk)
        if ext in IMG_EXT:
            p = _to_jpeg(p)
        names.append(f"ig/{aid}/{p.name}")
    return names


def is_video(name: str) -> bool:
    fid = cf.ref_id(name)
    if fid:
        r = cf.get(fid)
        return bool(r and r["kind"] == "video")
    return Path(name).suffix.lower() in VID_EXT


def media_items(names: list[str]) -> list[dict]:
    """Rejadagi media: mahalliy fayl (/media/...) yoki bulut fayli (cf:<id> → ochiq R2 havolasi)."""
    out = []
    for n in names:
        fid = cf.ref_id(n)
        if fid:
            r = cf.get(fid)
            out.append({"name": n, "cloud": True, "fid": fid, "missing": not r, "label": r["name"] if r else f"(o'chirilgan fayl #{fid})",
                        "u": cf.public_url(r) if r else "", "v": bool(r and r["kind"] == "video")})
        else:
            out.append({"name": n, "cloud": False, "fid": 0, "missing": False, "label": Path(n).name, "u": "/media/" + n, "v": is_video(n)})
    return out


def media_urls(item) -> list[dict]:
    return media_items(media_list(item))


def guess_mtype(names: list[str], want: str | None = None) -> str:
    if want in MTYPES:
        return want
    if len(names) > 1:
        return "CAROUSEL"
    if names and is_video(names[0]):
        return "REELS"
    return "IMAGE"
