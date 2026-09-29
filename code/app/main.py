"""Telegram Group Post - web platforma (FastAPI)."""
import asyncio
import html
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import db
from .config import CAPTION_LIMIT, CODE_DIR, MEDIA_DIR, MIN_DELAY_FLOOR
from .sender import Sender
from .telegram_service import TelegramService

tg = TelegramService()
sender = Sender(tg)

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VID_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

STATUS_LABELS = {
    "draft": "Qoralama", "scheduled": "Rejalashtirilgan", "queued": "Navbatda",
    "running": "Yuborilmoqda", "done": "Tugadi", "cancelled": "To'xtatildi",
    "interrupted": "Uzilgan", "failed": "Xato",
    "pending": "Kutilmoqda", "sent": "Yuborildi",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    # dastur o'chib qolganda yarim qolgan joblarni "uzilgan" deb belgilaymiz
    db.ex("UPDATE jobs SET status='interrupted', next_at=NULL WHERE status IN ('running','queued')")
    sender.begin_scheduler()
    yield
    await sender.stop()
    await tg.shutdown()


app = FastAPI(title="Telegram Group Post", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=CODE_DIR / "app" / "static"), name="static")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
templates = Jinja2Templates(directory=str(CODE_DIR / "app" / "templates"))
templates.env.globals["STATUS"] = STATUS_LABELS

_ALLOWED_TAGS = ("b", "strong", "i", "em", "u", "s", "code", "pre")


def render_text(text: str, mode: str = "none") -> Markup:
    """Preview uchun xavfsiz matn (HTML rejimida faqat ruxsat etilgan teglar)."""
    esc = html.escape(text or "")
    if mode == "html":
        for t in _ALLOWED_TAGS:
            esc = re.sub(rf"&lt;(/?){t}&gt;", rf"<\1{t}>", esc)
        esc = re.sub(r'&lt;a href=&quot;(https?://[^"<>\s]+?)&quot;&gt;',
                     r'<a href="\1" target="_blank" rel="noopener">', esc)
        esc = esc.replace("&lt;/a&gt;", "</a>")
    return Markup(esc.replace("\n", "<br>"))


templates.env.filters["render_text"] = render_text


# ---------------- yordamchilar ----------------
def page(request: Request, name: str, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    return templates.TemplateResponse(request, name, ctx)


def go(url: str, msg: str | None = None, err: str | None = None):
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif err:
        url += f"{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)


async def save_upload(f: UploadFile):
    ext = Path(f.filename).suffix.lower()
    kind = "image" if ext in IMG_EXT else "video" if ext in VID_EXT else None
    if not kind:
        raise ValueError("Faqat rasm (jpg, png, webp, gif) yoki video (mp4, mov, mkv, webm) yuklash mumkin")
    name = uuid.uuid4().hex + ext
    with open(MEDIA_DIR / name, "wb") as out:
        while chunk := await f.read(1024 * 1024):
            out.write(chunk)
    return name, kind


def media_in_use(name: str) -> bool:
    if not name:
        return True
    return bool(db.one("SELECT 1 FROM jobs WHERE media_path=? UNION SELECT 1 FROM templates WHERE media_path=?",
                       (name, name)))


def drop_media_if_unused(name: str):
    if name and not media_in_use(name):
        try:
            (MEDIA_DIR / Path(name).name).unlink(missing_ok=True)
        except OSError:
            pass


def groups_context():
    groups = db.q("SELECT * FROM groups ORDER BY title COLLATE NOCASE")
    lists = []
    for l in db.q("SELECT * FROM group_lists ORDER BY name COLLATE NOCASE"):
        ids = [r["tg_id"] for r in db.q("SELECT tg_id FROM group_list_items WHERE list_id=?", (l["id"],))]
        lists.append({"id": l["id"], "name": l["name"], "ids": ids})
    return groups, lists


def compose_form(**over):
    form = {"text": "", "parse_mode": "none", "media_path": "", "media_type": "",
            "min_delay": int(db.get_setting("default_min", 20)),
            "max_delay": int(db.get_setting("default_max", 60)),
            "scheduled_at": "", "selected": [], "template_name": "", "replace_job": 0}
    form.update(over)
    return form


# ---------------- sahifalar ----------------
@app.get("/")
async def index():
    return RedirectResponse("/compose")


# ---- Sozlamalar / Telegram kirish ----
@app.get("/settings")
async def settings_page(request: Request):
    st = await tg.status()
    step = request.query_params.get("step", "")
    return page(request, "settings.html", st=st, step=step,
                api_id=db.get_setting("api_id", ""),
                has_hash=bool(db.get_setting("api_hash")),
                default_min=db.get_setting("default_min", 20),
                default_max=db.get_setting("default_max", 60))


@app.post("/settings/api")
async def settings_api(api_id: str = Form(...), api_hash: str = Form("")):
    api_id = api_id.strip()
    if not api_id.isdigit():
        return go("/settings", err="API ID faqat raqamlardan iborat bo'lishi kerak")
    db.set_setting("api_id", api_id)
    if api_hash.strip():
        db.set_setting("api_hash", api_hash.strip())
    if not db.get_setting("api_hash"):
        return go("/settings", err="API HASH ni kiriting")
    return go("/settings", msg="API ma'lumotlari saqlandi")


@app.post("/settings/login/phone")
async def login_phone(phone: str = Form(...)):
    try:
        await tg.send_code(phone.strip())
    except Exception as e:
        return go("/settings", err=f"Kod yuborilmadi: {e}")
    return go("/settings?step=code", msg="Telegram'ga kelgan kodni kiriting")


@app.post("/settings/login/code")
async def login_code(code: str = Form(...)):
    try:
        res = await tg.sign_in_code(code.strip().replace(" ", ""))
    except Exception as e:
        return go("/settings?step=code", err=f"Kod noto'g'ri yoki eskirgan: {e}")
    if res == "password":
        return go("/settings?step=password", msg="Akkauntda 2 bosqichli parol bor, uni kiriting")
    return go("/settings", msg="Telegram akkaunt ulandi")


@app.post("/settings/login/password")
async def login_password(password: str = Form(...)):
    try:
        await tg.sign_in_password(password)
    except Exception as e:
        return go("/settings?step=password", err=f"Parol noto'g'ri: {e}")
    return go("/settings", msg="Telegram akkaunt ulandi")


@app.post("/settings/logout")
async def logout():
    try:
        await tg.logout()
    except Exception as e:
        return go("/settings", err=str(e))
    return go("/settings", msg="Akkauntdan chiqildi")


@app.post("/settings/defaults")
async def defaults(min_delay: int = Form(...), max_delay: int = Form(...)):
    if min_delay < MIN_DELAY_FLOOR or max_delay < min_delay:
        return go("/settings", err=f"Interval noto'g'ri (eng kami {MIN_DELAY_FLOOR}s, max >= min)")
    db.set_setting("default_min", min_delay)
    db.set_setting("default_max", max_delay)
    return go("/settings", msg="Standart interval saqlandi")


# ---- Guruhlar ----
@app.get("/groups")
async def groups_page(request: Request):
    groups, lists = groups_context()
    st = await tg.status()
    return page(request, "groups.html", groups=groups, lists=lists, st=st)


@app.post("/groups/sync")
async def groups_sync():
    try:
        n = await tg.fetch_groups()
    except Exception as e:
        return go("/groups", err=f"Guruhlarni olib bo'lmadi: {e}")
    return go("/groups", msg=f"{n} ta guruh/kanal yangilandi")


@app.post("/groups/lists")
async def list_save(name: str = Form(...), tg_ids: list[int] = Form(default=[])):
    name = name.strip()
    if not name or not tg_ids:
        return go("/groups", err="Ro'yxat nomini kiriting va kamida bitta guruh tanlang")
    ex = db.one("SELECT id FROM group_lists WHERE name=?", (name,))
    lid = ex["id"] if ex else db.ex("INSERT INTO group_lists(name) VALUES(?)", (name,))
    db.ex("DELETE FROM group_list_items WHERE list_id=?", (lid,))
    db.many("INSERT OR IGNORE INTO group_list_items(list_id,tg_id) VALUES(?,?)", [(lid, i) for i in tg_ids])
    return go("/groups", msg=f"'{name}' ro'yxati saqlandi ({len(tg_ids)} ta guruh)")


@app.post("/groups/lists/{lid}/delete")
async def list_delete(lid: int):
    db.ex("DELETE FROM group_lists WHERE id=?", (lid,))
    return go("/groups", msg="Ro'yxat o'chirildi")


# ---- Post yaratish ----
@app.get("/compose")
async def compose_page(request: Request, template: int = 0, job: int = 0):
    groups, lists = groups_context()
    form = compose_form()
    if template:
        t = db.one("SELECT * FROM templates WHERE id=?", (template,))
        if t:
            form.update(text=t["text"] or "", parse_mode=t["parse_mode"] or "none",
                        media_path=t["media_path"] or "", media_type=t["media_type"] or "")
    elif job:
        j = db.one("SELECT * FROM jobs WHERE id=?", (job,))
        if j:
            form.update(text=j["text"] or "", parse_mode=j["parse_mode"], media_path=j["media_path"] or "",
                        media_type=j["media_type"] or "", min_delay=j["min_delay"], max_delay=j["max_delay"],
                        scheduled_at=(j["scheduled_at"] or "").replace(" ", "T")[:16],
                        selected=[r["tg_id"] for r in db.q("SELECT tg_id FROM job_targets WHERE job_id=?", (job,))],
                        replace_job=job if j["status"] == "draft" else 0)
    st = await tg.status()
    return page(request, "compose.html", groups=groups, lists=lists, form=form, st=st,
                caption_limit=CAPTION_LIMIT)


@app.post("/compose/preview")
async def compose_preview(
    request: Request,
    text: str = Form(""), parse_mode: str = Form("none"),
    media: UploadFile | None = File(None),
    keep_media: str = Form(""), keep_media_type: str = Form(""),
    tg_ids: list[int] = Form(default=[]),
    min_delay: int = Form(20), max_delay: int = Form(60),
    scheduled_at: str = Form(""), template_name: str = Form(""), replace_job: int = Form(0),
):
    form = compose_form(text=text, parse_mode=parse_mode, min_delay=min_delay, max_delay=max_delay,
                        scheduled_at=scheduled_at, selected=tg_ids, template_name=template_name,
                        replace_job=replace_job, media_path=keep_media, media_type=keep_media_type)

    async def fail(msg):
        groups, lists = groups_context()
        return page(request, "compose.html", groups=groups, lists=lists, form=form,
                    st=await tg.status(), caption_limit=CAPTION_LIMIT, err=msg)

    text = text.strip()
    media_path, media_type = "", ""
    try:
        if media and media.filename:
            media_path, media_type = await save_upload(media)
        elif keep_media and (MEDIA_DIR / Path(keep_media).name).exists():
            media_path, media_type = Path(keep_media).name, keep_media_type
    except ValueError as e:
        return await fail(str(e))
    form["media_path"], form["media_type"] = media_path, media_type

    if not text and not media_path:
        return await fail("Matn yoki rasm/video kiriting")
    if not tg_ids:
        return await fail("Kamida bitta guruh tanlang")
    if min_delay < MIN_DELAY_FLOOR or max_delay < min_delay:
        return await fail(f"Interval noto'g'ri: min kamida {MIN_DELAY_FLOOR}s, max >= min bo'lsin")
    if scheduled_at:
        try:
            datetime.strptime(scheduled_at, "%Y-%m-%dT%H:%M")
        except ValueError:
            return await fail("Rejalashtirish vaqti noto'g'ri")
    if parse_mode not in ("none", "html"):
        parse_mode = "none"

    titles = {r["tg_id"]: r["title"] for r in db.q("SELECT tg_id,title FROM groups")}
    targets = [(i, titles[i]) for i in tg_ids if i in titles]
    if not targets:
        return await fail("Tanlangan guruhlar topilmadi. Guruhlar sahifasida yangilang.")

    if replace_job:
        old = db.one("SELECT * FROM jobs WHERE id=? AND status='draft'", (replace_job,))
        if old:
            db.ex("DELETE FROM jobs WHERE id=?", (replace_job,))
            if old["media_path"] != media_path:
                drop_media_if_unused(old["media_path"])

    job_id = db.ex(
        "INSERT INTO jobs(text,parse_mode,media_path,media_type,status,min_delay,max_delay,"
        "scheduled_at,created_at,total) VALUES(?,?,?,?,'draft',?,?,?,?,?)",
        (text, parse_mode, media_path or None, media_type or None, min_delay, max_delay,
         scheduled_at.replace("T", " ") + ":00" if scheduled_at else None, db.now(), len(targets)))
    db.many("INSERT INTO job_targets(job_id,tg_id,title) VALUES(?,?,?)", [(job_id, i, t) for i, t in targets])
    if template_name.strip():
        db.ex("INSERT INTO templates(name,text,parse_mode,media_path,media_type,created_at) VALUES(?,?,?,?,?,?)",
              (template_name.strip(), text, parse_mode, media_path or None, media_type or None, db.now()))
    return go(f"/preview/{job_id}")


# ---- Oldindan ko'rish va tasdiqlash ----
@app.get("/preview/{job_id}")
async def preview(request: Request, job_id: int):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        return go("/compose", err="Post topilmadi")
    if job["status"] != "draft":
        return go(f"/jobs/{job_id}")
    targets = db.q("SELECT * FROM job_targets WHERE job_id=? ORDER BY id", (job_id,))
    n = len(targets)
    avg = (job["min_delay"] + job["max_delay"]) / 2
    est_sec = int(avg * max(0, n - 1))
    warnings = []
    if job["media_path"] and job["text"] and len(job["text"]) > CAPTION_LIMIT:
        warnings.append(f"Matn {CAPTION_LIMIT} belgidan uzun: rasm/video va matn alohida ikki xabar bo'lib ketadi.")
    if n > 30:
        warnings.append("Guruhlar soni ko'p. Spamga tushmaslik uchun intervalni kattaroq qiling (masalan 45-120s).")
    if job["min_delay"] < 10:
        warnings.append("Interval juda qisqa, akkaunt cheklanishi (FloodWait/ban) xavfi bor.")
    return page(request, "preview.html", job=job, targets=targets, est_sec=est_sec,
                warnings=warnings, st=await tg.status(),
                when=(job["scheduled_at"] or "").replace(" ", "T")[:16])


@app.post("/preview/{job_id}/confirm")
async def confirm(job_id: int, when: str = Form("")):
    job = db.one("SELECT * FROM jobs WHERE id=? AND status='draft'", (job_id,))
    if not job:
        return go("/jobs", err="Post topilmadi yoki allaqachon tasdiqlangan")
    st = await tg.status()
    if not st.get("authorized"):
        return go("/settings", err="Avval Telegram akkauntni ulang")
    sched = None
    if when:
        try:
            dt = datetime.strptime(when, "%Y-%m-%dT%H:%M")
            if dt > datetime.now():
                sched = dt.strftime("%Y-%m-%d %H:%M:00")
        except ValueError:
            return go(f"/preview/{job_id}", err="Vaqt formati noto'g'ri")
    if sched:
        db.ex("UPDATE jobs SET status='scheduled', scheduled_at=? WHERE id=?", (sched, job_id))
        return go(f"/jobs/{job_id}", msg=f"Yuborish {sched} ga rejalashtirildi")
    db.ex("UPDATE jobs SET status='queued', scheduled_at=NULL WHERE id=?", (job_id,))
    sender.start(job_id)
    return go(f"/jobs/{job_id}", msg="Yuborish boshlandi")


@app.post("/preview/{job_id}/discard")
async def discard(job_id: int):
    j = db.one("SELECT * FROM jobs WHERE id=? AND status='draft'", (job_id,))
    if j:
        db.ex("DELETE FROM jobs WHERE id=?", (job_id,))
        drop_media_if_unused(j["media_path"])
    return go("/compose", msg="Qoralama bekor qilindi")


# ---- Yuborishlar tarixi ----
@app.get("/jobs")
async def jobs_page(request: Request):
    jobs = db.q("SELECT * FROM jobs WHERE status!='draft' ORDER BY id DESC LIMIT 200")
    return page(request, "jobs.html", jobs=jobs)


@app.get("/jobs/{job_id}")
async def job_page(request: Request, job_id: int):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        return go("/jobs", err="Topilmadi")
    if job["status"] == "draft":
        return go(f"/preview/{job_id}")
    return page(request, "job.html", job=job)


@app.get("/api/jobs/{job_id}")
async def job_api(job_id: int):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    targets = db.q("SELECT id,title,status,error,sent_at FROM job_targets WHERE job_id=? ORDER BY id", (job_id,))
    return {"job": dict(job), "status_label": STATUS_LABELS.get(job["status"], job["status"]),
            "server_now": db.now(), "targets": [dict(t) for t in targets],
            "pending": sum(1 for t in targets if t["status"] == "pending")}


@app.post("/jobs/{job_id}/cancel")
async def job_cancel(job_id: int):
    db.ex("UPDATE jobs SET status='cancelled' WHERE id=? AND status IN ('queued','scheduled','running')", (job_id,))
    return go(f"/jobs/{job_id}", msg="To'xtatildi")


@app.post("/jobs/{job_id}/resume")
async def job_resume(job_id: int):
    j = db.one("SELECT * FROM jobs WHERE id=? AND status IN ('cancelled','interrupted','failed')", (job_id,))
    if not j:
        return go(f"/jobs/{job_id}", err="Davom ettirib bo'lmaydi")
    st = await tg.status()
    if not st.get("authorized"):
        return go("/settings", err="Avval Telegram akkauntni ulang")
    db.ex("UPDATE jobs SET status='queued', finished_at=NULL WHERE id=?", (job_id,))
    sender.start(job_id)
    return go(f"/jobs/{job_id}", msg="Davom ettirilmoqda")


@app.post("/jobs/{job_id}/delete")
async def job_delete(job_id: int):
    j = db.one("SELECT * FROM jobs WHERE id=? AND status NOT IN ('running','queued')", (job_id,))
    if j:
        db.ex("DELETE FROM jobs WHERE id=?", (job_id,))
        drop_media_if_unused(j["media_path"])
    return go("/jobs", msg="O'chirildi")


# ---- Shablonlar ----
@app.get("/templates")
async def templates_page(request: Request):
    return page(request, "templates.html", items=db.q("SELECT * FROM templates ORDER BY id DESC"))


@app.post("/templates/{tid}/delete")
async def template_delete(tid: int):
    t = db.one("SELECT * FROM templates WHERE id=?", (tid,))
    if t:
        db.ex("DELETE FROM templates WHERE id=?", (tid,))
        drop_media_if_unused(t["media_path"])
    return go("/templates", msg="Shablon o'chirildi")
