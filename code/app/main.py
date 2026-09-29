"""Telegram Group Post - web platforma (FastAPI)."""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db
from .config import (CAPTION_LIMIT, CODE_DIR, IMPORT_DIR, JOIN_MIN_FLOOR, MAX_UPLOAD_MB, MEDIA_DIR,
                     MIN_DELAY_FLOOR, log)
from .excel_import import SUPPORTED, analyze, export_report
from .joiner import Joiner
from .sender import Sender
from .telegram_service import TelegramService
from .web import BATCH_LABELS, JOIN_LABELS, STATUS_LABELS, go, page, templates

tg = TelegramService()
sender = Sender(tg)
joiner = Joiner(tg)

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VID_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    # dastur to'satdan o'chgan bo'lsa, yarim qolgan ishlarni "uzilgan" deb belgilaymiz
    db.ex("UPDATE jobs SET status='interrupted', next_at=NULL WHERE status IN ('running','queued')")
    db.ex("UPDATE job_targets SET status='pending' WHERE status='sending'")
    db.ex("UPDATE join_batches SET status='interrupted', next_at=NULL WHERE status IN ('running','queued')")
    sender.begin_scheduler()
    joiner.begin_scheduler()
    import asyncio
    asyncio.create_task(tg.status())     # sidebar uchun akkaunt holatini oldindan aniqlash
    log.info("Dastur ishga tushdi")
    yield
    await sender.stop()
    await joiner.stop()
    await tg.shutdown()


app = FastAPI(title="Telegram Group Post", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=CODE_DIR / "app" / "static"), name="static")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
templates.env.globals["account"] = lambda: tg.info


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    log.exception("Kutilmagan xato: %s %s", request.method, request.url.path)
    return page(request, "error.html", status_code=500, detail=f"{type(exc).__name__}: {exc}")


# ---------------- yordamchilar ----------------
async def save_upload(f: UploadFile):
    ext = Path(f.filename).suffix.lower()
    kind = "image" if ext in IMG_EXT else "video" if ext in VID_EXT else None
    if not kind:
        raise ValueError("Faqat rasm (jpg, png, webp, gif) yoki video (mp4, mov, mkv, webm) yuklash mumkin")
    name = uuid.uuid4().hex + ext
    size, limit = 0, MAX_UPLOAD_MB * 1024 * 1024
    with open(MEDIA_DIR / name, "wb") as out:
        while chunk := await f.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                (MEDIA_DIR / name).unlink(missing_ok=True)
                raise ValueError(f"Fayl juda katta (maksimum {MAX_UPLOAD_MB} MB)")
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


def batch_counts(bid: int) -> dict:
    c = {r["status"]: r["c"] for r in db.q(
        "SELECT status, COUNT(*) c FROM join_targets WHERE batch_id=? GROUP BY status", (bid,))}
    total = sum(c.values())
    pending = c.get("pending", 0)
    ok = c.get("joined", 0) + c.get("already", 0)
    req = c.get("requested", 0)
    return {"total": total, "pending": pending, "joined": c.get("joined", 0), "already": c.get("already", 0),
            "requested": req, "ok": ok, "failed": total - pending - ok - req, "by": c}


# ---------------- bosh sahifa ----------------
@app.get("/")
async def dashboard(request: Request):
    st = await tg.status()
    stats = {
        "groups": db.one("SELECT COUNT(*) c FROM groups")["c"],
        "sent": db.one("SELECT COALESCE(SUM(done),0) c FROM jobs")["c"],
        "jobs": db.one("SELECT COUNT(*) c FROM jobs WHERE status!='draft'")["c"],
        "joined": db.one("SELECT COUNT(*) c FROM join_targets WHERE status IN ('joined','already')")["c"],
    }
    jobs = db.q("SELECT * FROM jobs WHERE status!='draft' ORDER BY id DESC LIMIT 5")
    batches = db.q("SELECT * FROM join_batches WHERE status!='draft' ORDER BY id DESC LIMIT 5")
    return page(request, "dashboard.html", st=st, stats=stats, jobs=jobs,
                batches=[dict(b, **batch_counts(b["id"])) for b in batches])


# ---------------- sozlamalar / Telegram kirish ----------------
@app.get("/settings")
async def settings_page(request: Request):
    st = await tg.status()
    return page(request, "settings.html", st=st, step=request.query_params.get("step", ""),
                api_id=db.get_setting("api_id", ""), has_hash=bool(db.get_setting("api_hash")),
                default_min=db.get_setting("default_min", 20), default_max=db.get_setting("default_max", 60))


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
    await tg.status()
    return go("/settings", msg="Telegram akkaunt ulandi")


@app.post("/settings/login/password")
async def login_password(password: str = Form(...)):
    try:
        await tg.sign_in_password(password)
    except Exception as e:
        return go("/settings?step=password", err=f"Parol noto'g'ri: {e}")
    await tg.status()
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


# ---------------- guruhlar ----------------
@app.get("/groups")
async def groups_page(request: Request):
    groups, lists = groups_context()
    return page(request, "groups.html", groups=groups, lists=lists, st=await tg.status())


@app.post("/groups/sync")
async def groups_sync():
    try:
        n = await tg.fetch_groups()
    except Exception as e:
        log.exception("Guruhlarni yangilash")
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


# ---------------- post yaratish ----------------
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
    return page(request, "compose.html", groups=groups, lists=lists, form=form, st=await tg.status(),
                caption_limit=CAPTION_LIMIT)


@app.post("/compose/preview")
async def compose_preview(
    request: Request,
    text: str = Form(""), parse_mode: str = Form("none"),
    media: UploadFile | None = File(None),
    keep_media: str = Form(""), keep_media_type: str = Form(""), remove_media: str = Form(""),
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
        elif keep_media and not remove_media and (MEDIA_DIR / Path(keep_media).name).exists():
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


@app.get("/preview/{job_id}")
async def preview(request: Request, job_id: int):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        return go("/compose", err="Post topilmadi")
    if job["status"] != "draft":
        return go(f"/jobs/{job_id}")
    targets = db.q("SELECT * FROM job_targets WHERE job_id=? ORDER BY id", (job_id,))
    n = len(targets)
    est_sec = int((job["min_delay"] + job["max_delay"]) / 2 * max(0, n - 1))
    warnings = []
    if job["media_path"] and job["text"] and len(job["text"]) > CAPTION_LIMIT:
        warnings.append(f"Matn {CAPTION_LIMIT} belgidan uzun: rasm/video va matn alohida ikki xabar bo'lib ketadi.")
    if job["media_type"] == "video" and not job["media_path"].lower().endswith((".mp4", ".m4v")):
        warnings.append("Video mp4 (H.264) bo'lmasa, Telegram uni fayl sifatida ko'rsatishi mumkin. mp4 tavsiya etiladi.")
    if n > 30:
        warnings.append("Guruhlar soni ko'p. Spamga tushmaslik uchun intervalni kattaroq qiling (masalan 45-120s).")
    if job["min_delay"] < 10:
        warnings.append("Interval juda qisqa, akkaunt cheklanishi (FloodWait/ban) xavfi bor.")
    return page(request, "preview.html", job=job, targets=targets, est_sec=est_sec, warnings=warnings,
                st=await tg.status(), when=(job["scheduled_at"] or "").replace(" ", "T")[:16])


@app.post("/preview/{job_id}/confirm")
async def confirm(job_id: int, when: str = Form("")):
    job = db.one("SELECT * FROM jobs WHERE id=? AND status='draft'", (job_id,))
    if not job:
        return go("/jobs", err="Post topilmadi yoki allaqachon tasdiqlangan")
    if not (await tg.status()).get("authorized"):
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
        return go(f"/jobs/{job_id}", msg=f"Yuborish {sched[:16]} ga rejalashtirildi")
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


# ---------------- yuborishlar tarixi ----------------
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
            "pending": sum(1 for t in targets if t["status"] in ("pending", "sending"))}


@app.post("/jobs/{job_id}/cancel")
async def job_cancel(job_id: int):
    db.ex("UPDATE jobs SET status='cancelled' WHERE id=? AND status IN ('queued','scheduled','running')", (job_id,))
    return go(f"/jobs/{job_id}", msg="To'xtatildi")


@app.post("/jobs/{job_id}/resume")
async def job_resume(job_id: int):
    j = db.one("SELECT * FROM jobs WHERE id=? AND status IN ('cancelled','interrupted','failed')", (job_id,))
    if not j:
        return go(f"/jobs/{job_id}", err="Davom ettirib bo'lmaydi")
    if not (await tg.status()).get("authorized"):
        return go("/settings", err="Avval Telegram akkauntni ulang")
    db.ex("UPDATE jobs SET status='queued', finished_at=NULL WHERE id=?", (job_id,))
    sender.start(job_id)
    return go(f"/jobs/{job_id}", msg="Davom ettirilmoqda")


@app.post("/jobs/{job_id}/retry")
async def job_retry(job_id: int):
    j = db.one("SELECT * FROM jobs WHERE id=? AND status NOT IN ('running','queued','draft')", (job_id,))
    if not j:
        return go(f"/jobs/{job_id}", err="Hozir qayta urinib bo'lmaydi")
    if not (await tg.status()).get("authorized"):
        return go("/settings", err="Avval Telegram akkauntni ulang")
    db.ex("UPDATE job_targets SET status='pending', error=NULL WHERE job_id=? AND status='failed'", (job_id,))
    db.ex("UPDATE jobs SET status='queued', finished_at=NULL WHERE id=?", (job_id,))
    sender.start(job_id)
    return go(f"/jobs/{job_id}", msg="Xato bo'lgan guruhlarga qayta yuborilmoqda")


@app.post("/jobs/{job_id}/delete")
async def job_delete(job_id: int):
    j = db.one("SELECT * FROM jobs WHERE id=? AND status NOT IN ('running','queued')", (job_id,))
    if j:
        db.ex("DELETE FROM jobs WHERE id=?", (job_id,))
        drop_media_if_unused(j["media_path"])
    return go("/jobs", msg="O'chirildi")


# ---------------- shablonlar ----------------
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


# ---------------- Excel orqali guruhlarga a'zo bo'lish ----------------
@app.get("/join")
async def join_page(request: Request):
    batches = db.q("SELECT * FROM join_batches WHERE status!='draft' ORDER BY id DESC LIMIT 100")
    return page(request, "join.html", st=await tg.status(),
                batches=[dict(b, **batch_counts(b["id"])) for b in batches],
                default_min=db.get_setting("join_min", 60), default_max=db.get_setting("join_max", 180),
                default_daily=db.get_setting("join_daily", 40))


@app.post("/join/upload")
async def join_upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUPPORTED:
        return go("/join", err="Faqat .xlsx, .csv yoki .txt fayl yuklang (eski .xls ni .xlsx qilib saqlang)")
    name = f"{uuid.uuid4().hex}{ext}"
    dest = IMPORT_DIR / name
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
    try:
        columns = analyze(dest)
    except Exception as e:
        log.exception("Excel o'qish xatosi")
        return go("/join", err=f"Faylni o'qib bo'lmadi: {e}")
    if not columns:
        return go("/join", err="Faylda username yoki t.me havolasi topilmadi")
    bid = db.ex("INSERT INTO join_batches(filename,status,created_at,raw) VALUES(?,?,?,?)",
                (file.filename, "draft", db.now(), json.dumps(columns, ensure_ascii=False)))
    return go(f"/join/{bid}/setup")


@app.get("/join/{bid}/setup")
async def join_setup(request: Request, bid: int):
    b = db.one("SELECT * FROM join_batches WHERE id=?", (bid,))
    if not b:
        return go("/join", err="Topilmadi")
    if b["status"] != "draft":
        return go(f"/join/{bid}")
    columns = json.loads(b["raw"])
    best = max(range(len(columns)), key=lambda i: len(columns[i]["items"]))
    return page(request, "join_setup.html", b=b, columns=columns, best=best, st=await tg.status(),
                min_delay=db.get_setting("join_min", 60), max_delay=db.get_setting("join_max", 180),
                daily=db.get_setting("join_daily", 40), floor=JOIN_MIN_FLOOR)


@app.post("/join/{bid}/start")
async def join_start(bid: int, cols: list[int] = Form(default=[]), min_delay: int = Form(60),
                     max_delay: int = Form(180), daily_limit: int = Form(40)):
    b = db.one("SELECT * FROM join_batches WHERE id=? AND status='draft'", (bid,))
    if not b:
        return go("/join", err="Paket topilmadi yoki allaqachon boshlangan")
    if not (await tg.status()).get("authorized"):
        return go("/settings", err="Avval Telegram akkauntni ulang")
    if min_delay < JOIN_MIN_FLOOR or max_delay < min_delay:
        return go(f"/join/{bid}/setup", err=f"Interval noto'g'ri: min kamida {JOIN_MIN_FLOOR}s, max >= min")
    if not 1 <= daily_limit <= 500:
        return go(f"/join/{bid}/setup", err="Kunlik limit 1 dan 500 gacha bo'lsin")
    columns = json.loads(b["raw"])
    seen, rows = set(), []
    for ci in cols:
        if 0 <= ci < len(columns):
            for it in columns[ci]["items"]:
                k = (it["kind"], it["key"].lower() if it["kind"] == "username" else it["key"])
                if k not in seen:
                    seen.add(k)
                    rows.append((bid, it["ref"], it["kind"], it["key"]))
    if not rows:
        return go(f"/join/{bid}/setup", err="Kamida bitta ustunni tanlang")
    db.many("INSERT INTO join_targets(batch_id,ref,kind,key) VALUES(?,?,?,?)", rows)
    db.ex("UPDATE join_batches SET status='queued', total=?, min_delay=?, max_delay=?, daily_limit=?, raw=NULL "
          "WHERE id=?", (len(rows), min_delay, max_delay, daily_limit, bid))
    db.set_setting("join_min", min_delay)
    db.set_setting("join_max", max_delay)
    db.set_setting("join_daily", daily_limit)
    joiner.start(bid)
    return go(f"/join/{bid}", msg="A'zo bo'lish boshlandi")


@app.get("/join/{bid}")
async def join_report(request: Request, bid: int):
    b = db.one("SELECT * FROM join_batches WHERE id=?", (bid,))
    if not b:
        return go("/join", err="Topilmadi")
    if b["status"] == "draft":
        return go(f"/join/{bid}/setup")
    return page(request, "join_report.html", b=b)


@app.get("/api/join/{bid}")
async def join_api(bid: int, rev: str = ""):
    b = db.one("SELECT * FROM join_batches WHERE id=?", (bid,))
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    counts = batch_counts(bid)
    new_rev = f"{counts['total'] - counts['pending']}:{b['status']}"
    data = {"b": {k: b[k] for k in ("id", "filename", "status", "next_at", "resume_at", "error", "daily_limit",
                                    "min_delay", "max_delay", "created_at")},
            "status_label": BATCH_LABELS.get(b["status"], b["status"]), "counts": counts,
            "server_now": db.now(), "rev": new_rev}
    if rev != new_rev:
        data["targets"] = [dict(t) for t in db.q(
            "SELECT id,ref,kind,status,detail,title,tried_at FROM join_targets WHERE batch_id=? ORDER BY id", (bid,))]
    return data


@app.post("/join/{bid}/cancel")
async def join_cancel(bid: int):
    db.ex("UPDATE join_batches SET status='cancelled', next_at=NULL WHERE id=? "
          "AND status IN ('queued','running','waiting')", (bid,))
    return go(f"/join/{bid}", msg="To'xtatildi")


@app.post("/join/{bid}/resume")
async def join_resume(bid: int):
    b = db.one("SELECT * FROM join_batches WHERE id=? AND status IN ('cancelled','interrupted','failed','waiting')", (bid,))
    if not b:
        return go(f"/join/{bid}", err="Davom ettirib bo'lmaydi")
    if not (await tg.status()).get("authorized"):
        return go("/settings", err="Avval Telegram akkauntni ulang")
    db.ex("UPDATE join_batches SET status='queued', finished_at=NULL, error=NULL WHERE id=?", (bid,))
    joiner.start(bid)
    return go(f"/join/{bid}", msg="Davom ettirilmoqda")


@app.post("/join/{bid}/retry")
async def join_retry(bid: int):
    b = db.one("SELECT * FROM join_batches WHERE id=? AND status NOT IN ('running','queued','draft')", (bid,))
    if not b:
        return go(f"/join/{bid}", err="Hozir qayta urinib bo'lmaydi (jarayon ishlayapti)")
    if not (await tg.status()).get("authorized"):
        return go("/settings", err="Avval Telegram akkauntni ulang")
    n = db.q("SELECT COUNT(*) c FROM join_targets WHERE batch_id=? AND status NOT IN "
             "('joined','already','requested','pending')", (bid,))[0]["c"]
    if not n:
        return go(f"/join/{bid}", err="Qayta urinadigan xatolar yo'q")
    db.ex("UPDATE join_targets SET status='pending', detail=NULL, tried_at=NULL WHERE batch_id=? AND status NOT IN "
          "('joined','already','requested','pending')", (bid,))
    db.ex("UPDATE join_batches SET status='queued', finished_at=NULL, error=NULL WHERE id=?", (bid,))
    joiner.start(bid)
    return go(f"/join/{bid}", msg=f"{n} ta ulanmagan guruhga qayta urinish boshlandi")


@app.post("/join/{bid}/delete")
async def join_delete(bid: int):
    db.ex("DELETE FROM join_batches WHERE id=? AND status NOT IN ('running','queued')", (bid,))
    return go("/join", msg="O'chirildi")


@app.get("/join/{bid}/export.xlsx")
async def join_export(bid: int):
    b = db.one("SELECT * FROM join_batches WHERE id=?", (bid,))
    if not b:
        return go("/join", err="Topilmadi")
    targets = db.q("SELECT * FROM join_targets WHERE batch_id=? ORDER BY id", (bid,))
    data = export_report(b, targets, JOIN_LABELS)
    fname = f"azo_bolish_hisobot_{bid}.xlsx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})
