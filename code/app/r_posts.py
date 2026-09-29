"""Bosh sahifa, post yaratish (variantlar), oldindan ko'rish, yuborishlar, kampaniyalar, media kutubxonasi."""
import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from . import db, jobops, jobsvc
from .config import CAPTION_LIMIT, MAX_UPLOAD_MB, MEDIA_DIR, MIN_DELAY_FLOOR
from .core import manager, sender
from .limits import health
from .textutil import render_variant, slug, spin_variants_count
from .web import STATUS_LABELS, acc_id, go, need_account, page, render_text

router = APIRouter()

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VID_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
MAX_VARIANTS = 10
MAX_ALBUM = 10


# ---------------- yordamchilar ----------------
def kind_of(name: str):
    ext = Path(name).suffix.lower()
    return "image" if ext in IMG_EXT else "video" if ext in VID_EXT else None


async def save_upload(f) -> tuple[str, str]:
    ext = Path(f.filename).suffix.lower()
    kind = kind_of(f.filename)
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
    db.ex("INSERT OR IGNORE INTO media_items(name,original,kind,size,created_at) VALUES(?,?,?,?,?)",
          (name, f.filename, kind, size, db.now()))
    return name, kind


def groups_context(aid: int):
    groups = [dict(g) for g in db.q(
        "SELECT g.*, (SELECT 1 FROM blacklist b WHERE b.account_id=g.account_id AND b.tg_id=g.tg_id) bl "
        "FROM groups g WHERE g.account_id=? ORDER BY g.title COLLATE NOCASE", (aid,))]
    tags: dict[str, list[int]] = {}
    tag_of: dict[int, list[str]] = {}
    for r in db.q("SELECT tg_id, tag FROM group_tags WHERE account_id=? ORDER BY tag", (aid,)):
        tags.setdefault(r["tag"], []).append(r["tg_id"])
        tag_of.setdefault(r["tg_id"], []).append(r["tag"])
    for g in groups:
        g["tags"] = tag_of.get(g["tg_id"], [])
    lists = []
    for l in db.q("SELECT * FROM group_lists WHERE account_id=? ORDER BY name COLLATE NOCASE", (aid,)):
        ids = [r["tg_id"] for r in db.q("SELECT tg_id FROM group_list_items WHERE list_id=?", (l["id"],))]
        lists.append({"id": l["id"], "name": l["name"], "ids": ids})
    return groups, lists, tags


def default_form(**over):
    form = {"variants": [{"text": "", "parse_mode": "none", "media": []}], "selected": [],
            "min_delay": int(db.get_setting("default_min", 20)), "max_delay": int(db.get_setting("default_max", 60)),
            "scheduled_at": "", "name": "", "utm_on": 0, "utm_source": "telegram", "utm_campaign": "", "skip_ads": 1,
            "recurrence": "none", "rec_time": "10:00", "rec_days": [], "replace_job": 0}
    form.update(over)
    return form


def compose_ctx(request, aid, form, **extra):
    groups, lists, tags = groups_context(aid)
    return dict(groups=groups, lists=lists, tags=tags, form=form, caption_limit=CAPTION_LIMIT,
                max_variants=MAX_VARIANTS, **extra)


# ---------------- bosh sahifa ----------------
@router.get("/")
async def dashboard(request: Request):
    aid = acc_id(request)
    if not aid:
        return page(request, "dashboard.html", stats=None, jobs=[], campaigns=[], h=None, no_account=True)
    stats = {
        "groups": db.one("SELECT COUNT(*) c FROM groups WHERE account_id=?", (aid,))["c"],
        "sent": db.one("SELECT COALESCE(SUM(done),0) c FROM jobs WHERE account_id=?", (aid,))["c"],
        "jobs": db.one("SELECT COUNT(*) c FROM jobs WHERE account_id=? AND status!='draft'", (aid,))["c"],
        "joined": db.one("SELECT COUNT(*) c FROM join_targets jt JOIN join_batches b ON b.id=jt.batch_id "
                         "WHERE b.account_id=? AND jt.status IN ('joined','already')", (aid,))["c"],
        "views": db.one("SELECT COALESCE(SUM(jt.views),0) c FROM job_targets jt JOIN jobs j ON j.id=jt.job_id WHERE j.account_id=?", (aid,))["c"],
        "campaigns": db.one("SELECT COUNT(*) c FROM campaigns WHERE account_id=?", (aid,))["c"],
    }
    jobs = db.q("SELECT * FROM jobs WHERE account_id=? AND status!='draft' ORDER BY id DESC LIMIT 5", (aid,))
    camps = db.q("SELECT * FROM campaigns WHERE account_id=? AND rec_active=1 ORDER BY next_run LIMIT 5", (aid,))
    return page(request, "dashboard.html", stats=stats, jobs=jobs, campaigns=camps, h=health(aid), no_account=False)


# ---------------- post yaratish ----------------
@router.get("/compose")
async def compose_page(request: Request, job: int = 0, campaign: int = 0):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    form = default_form()
    if campaign:
        c = db.one("SELECT * FROM campaigns WHERE id=? AND account_id=?", (campaign, aid))
        if c:
            form.update(variants=jobsvc.campaign_variants(campaign) or form["variants"], name=c["name"],
                        selected=json.loads(c["target_json"] or "[]"), min_delay=c["min_delay"] or 20,
                        max_delay=c["max_delay"] or 60, utm_on=c["utm_on"], utm_source=c["utm_source"] or "telegram",
                        utm_campaign=c["utm_campaign"] or "", skip_ads=c["skip_ads"], recurrence=c["recurrence"],
                        rec_time=c["rec_time"], rec_days=[int(x) for x in (c["rec_days"] or "").split(",") if x.isdigit()])
    elif job:
        j = db.one("SELECT * FROM jobs WHERE id=? AND account_id=?", (job, aid))
        if j:
            vs = [{"text": v["text"] or "", "parse_mode": v["parse_mode"], "media": jobsvc.media_of(v)}
                  for v in jobsvc.variants_of(job)]
            form.update(variants=vs or form["variants"], name=j["name"] or "", min_delay=j["min_delay"], max_delay=j["max_delay"],
                        scheduled_at=(j["scheduled_at"] or "").replace(" ", "T")[:16],
                        selected=[t["tg_id"] for t in db.q("SELECT tg_id FROM job_targets WHERE job_id=?", (job,))],
                        utm_on=j["utm_on"], utm_source=j["utm_source"] or "telegram", utm_campaign=j["utm_campaign"] or "",
                        replace_job=job if j["status"] == "draft" else 0)
    return page(request, "compose.html", **compose_ctx(request, aid, form))


async def parse_variants(f) -> tuple[list[dict], str | None]:
    order = [int(x) for x in (f.get("v_order") or "").split(",") if x.strip().isdigit()][:MAX_VARIANTS]
    variants, err = [], None
    for n, i in enumerate(order, 1):
        text = (f.get(f"v{i}_text") or "").strip()
        mode = "html" if f.get(f"v{i}_mode") == "html" else "none"
        media = []
        for name in (f.get(f"v{i}_keep") or "").split(","):
            name = Path(name.strip()).name
            if name and (MEDIA_DIR / name).exists() and kind_of(name):
                media.append(name)
        for up in f.getlist(f"v{i}_files"):
            if getattr(up, "filename", ""):
                try:
                    name, _ = await save_upload(up)
                    media.append(name)
                except ValueError as e:
                    err = err or f"Variant {n}: {e}"
        kinds = {kind_of(m) for m in media}
        mtype = None
        if "video" in kinds:
            if len(media) > 1:
                err = err or f"Variant {n}: video bilan bir nechta fayl yuborib bo'lmaydi (video faqat bitta bo'lishi kerak)"
            mtype = "video"
        elif media:
            if len(media) > MAX_ALBUM:
                err = err or f"Variant {n}: albomda {MAX_ALBUM} tadan ortiq rasm bo'lmaydi"
                media = media[:MAX_ALBUM]
            mtype = "image"
        variants.append({"text": text, "parse_mode": mode, "media": media, "media_type": mtype})
    return variants, err


@router.post("/compose/preview")
async def compose_preview(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    f = await request.form()
    variants, err = await parse_variants(f)
    tg_ids = [int(x) for x in f.getlist("tg_ids") if str(x).lstrip("-").isdigit()]

    def geti(k, d):
        try:
            return int(f.get(k) or d)
        except ValueError:
            return d

    name = (f.get("name") or "").strip()
    rec = f.get("recurrence") if f.get("recurrence") in ("daily", "weekly") else "none"
    rec_days = [int(x) for x in f.getlist("rec_days") if str(x).isdigit()]
    form = default_form(variants=variants or default_form()["variants"], selected=tg_ids, min_delay=geti("min_delay", 20),
                        max_delay=geti("max_delay", 60), scheduled_at=f.get("scheduled_at") or "", name=name,
                        utm_on=int(bool(f.get("utm_on"))), utm_source=(f.get("utm_source") or "telegram").strip(),
                        utm_campaign=(f.get("utm_campaign") or "").strip(), skip_ads=int(bool(f.get("skip_ads"))),
                        recurrence=rec, rec_time=f.get("rec_time") or "10:00", rec_days=rec_days,
                        replace_job=geti("replace_job", 0))

    def fail(msg):
        return page(request, "compose.html", **compose_ctx(request, aid, form), err=msg)

    if err:
        return fail(err)
    if not variants:
        return fail("Kamida bitta variant kerak")
    for n, v in enumerate(variants, 1):
        if not v["text"] and not v["media"]:
            return fail(f"Variant {n} bo'sh: matn yoki rasm/video kiriting")
    if not tg_ids:
        return fail("Kamida bitta guruh tanlang")
    mn, mx = form["min_delay"], form["max_delay"]
    if mn < MIN_DELAY_FLOOR or mx < mn:
        return fail(f"Interval noto'g'ri: min kamida {MIN_DELAY_FLOOR}s, max >= min bo'lsin")
    sched = form["scheduled_at"]
    if sched:
        try:
            datetime.strptime(sched, "%Y-%m-%dT%H:%M")
        except ValueError:
            return fail("Rejalashtirish vaqti noto'g'ri")
    if rec != "none" and not name:
        return fail("Takrorlanuvchi yuborish uchun kampaniya nomini kiriting")
    if rec == "weekly" and not rec_days:
        return fail("Haftalik takrorlash uchun kamida bitta hafta kunini tanlang")

    utm_campaign = form["utm_campaign"] or slug(name or "reklama")
    cid = None
    if name:
        cid = jobsvc.save_campaign(aid, name, variants, tg_ids, min_delay=mn, max_delay=mx, utm_on=form["utm_on"],
                                   utm_source=form["utm_source"], utm_campaign=utm_campaign, skip_ads=form["skip_ads"],
                                   recurrence=rec, rec_time=form["rec_time"], rec_days=",".join(map(str, sorted(rec_days))))
    if form["replace_job"]:
        old = db.one("SELECT id FROM jobs WHERE id=? AND account_id=? AND status='draft'", (form["replace_job"], aid))
        if old:
            db.ex("DELETE FROM jobs WHERE id=?", (old["id"],))
    job_id, stats = jobsvc.create_draft(
        aid, variants, tg_ids, min_delay=mn, max_delay=mx, scheduled_at=(sched.replace("T", " ") + ":00") if sched else None,
        campaign_id=cid, name=name, utm_on=form["utm_on"], utm_source=form["utm_source"], utm_campaign=utm_campaign,
        skip_ads=form["skip_ads"])
    if not stats["total"]:
        db.ex("DELETE FROM jobs WHERE id=?", (job_id,))
        return fail("Yuboriladigan guruh qolmadi (hammasi qora ro'yxatda, reklama taqiqlangan yoki topilmadi)")
    return go(f"/preview/{job_id}")


# ---------------- oldindan ko'rish ----------------
@router.get("/preview/{job_id}")
async def preview(request: Request, job_id: int):
    if (r := need_account(request)):
        return r
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        return go("/compose", err="Post topilmadi")
    if job["status"] != "draft":
        return go(f"/jobs/{job_id}")
    vs = jobsvc.variants_of(job_id)
    targets = [dict(t) for t in db.q("SELECT * FROM job_targets WHERE job_id=? ORDER BY id", (job_id,))]
    plan = jobsvc.plan_assignment(job_id)
    k = 0
    dist = [0] * len(vs)
    for t in targets:
        if t["status"] == "pending":
            t["vn"] = plan[k] + 1
            dist[plan[k]] += 1
            k += 1
    n = sum(dist)
    est_sec = int((job["min_delay"] + job["max_delay"]) / 2 * max(0, n - 1))
    utm = {"source": job["utm_source"], "campaign": job["utm_campaign"]} if job["utm_on"] else None
    sample = {"title": "Namuna guruh", "username": "namuna_guruh", "tg_id": 1}
    previews = []
    for i, v in enumerate(vs):
        previews.append({"n": i + 1, "html": render_text(render_variant(v["text"] or "", sample, utm), v["parse_mode"]),
                         "media": jobsvc.media_of(v), "type": v["media_type"], "count": dist[i],
                         "long": bool(jobsvc.media_of(v)) and len(v["text"] or "") > CAPTION_LIMIT,
                         "spin": spin_variants_count(v["text"])})
    warns = []
    if any(p["long"] for p in previews):
        warns.append(f"Ba'zi variantlarda matn {CAPTION_LIMIT} belgidan uzun: rasm/video va matn alohida ikki xabar bo'lib ketadi.")
    for v in vs:
        if v["media_type"] == "video" and not (jobsvc.media_of(v)[0].lower().endswith((".mp4", ".m4v"))):
            warns.append("Video mp4 (H.264) bo'lmasa, Telegram uni fayl sifatida ko'rsatishi mumkin.")
            break
    if n > 30:
        warns.append("Guruhlar soni ko'p. Spamga tushmaslik uchun intervalni kattaroq qiling (masalan 45-120s).")
    if job["min_delay"] < 10:
        warns.append("Interval juda qisqa, akkaunt cheklanishi (FloodWait/ban) xavfi bor.")
    skipped = [t for t in targets if t["status"] == "skipped"]
    h = health(job["account_id"])
    if h["level"] != "good":
        warns.append(f"Akkaunt holati: {h['label']} (24 soatda {h['flood']} ta FloodWait). Yuborish avtomatik sekinlashtiriladi.")
    camp = db.one("SELECT * FROM campaigns WHERE id=?", (job["campaign_id"],)) if job["campaign_id"] else None
    ptr = jobsvc.get_ptr(jobsvc.job_ptr_key(job)) % max(1, len(vs))
    svc_st = await manager.get(job["account_id"]).status()
    return page(request, "preview.html", job=job, targets=targets, previews=previews, est_sec=est_sec, warns=warns,
                skipped=skipped, camp=camp, ptr=ptr + 1, nvars=len(vs), n=n, authorized=svc_st.get("authorized"),
                when=(job["scheduled_at"] or "").replace(" ", "T")[:16])


@router.post("/preview/{job_id}/confirm")
async def confirm(job_id: int, when: str = Form("")):
    job = db.one("SELECT * FROM jobs WHERE id=? AND status='draft'", (job_id,))
    if not job:
        return go("/jobs", err="Post topilmadi yoki allaqachon tasdiqlangan")
    if not (await manager.get(job["account_id"]).status()).get("authorized"):
        return go("/accounts", err="Bu akkaunt Telegram'ga ulanmagan. Avval ulang.")
    if when:
        try:
            datetime.strptime(when, "%Y-%m-%dT%H:%M")
        except ValueError:
            return go(f"/preview/{job_id}", err="Vaqt formati noto'g'ri")
    res = jobsvc.confirm_job(job_id, when)
    if job["campaign_id"]:
        jobsvc.activate_campaign(job["campaign_id"])
    if res == "scheduled":
        return go(f"/jobs/{job_id}", msg=f"Yuborish {when.replace('T', ' ')} ga rejalashtirildi")
    sender.start(job_id)
    return go(f"/jobs/{job_id}", msg="Yuborish boshlandi")


@router.post("/preview/{job_id}/discard")
async def discard(job_id: int):
    db.ex("DELETE FROM jobs WHERE id=? AND status='draft'", (job_id,))
    return go("/compose", msg="Qoralama bekor qilindi")


# ---------------- yuborishlar ----------------
@router.get("/jobs")
async def jobs_page(request: Request):
    aid = acc_id(request)
    jobs = db.q("SELECT j.*, (SELECT COUNT(*) FROM job_variants WHERE job_id=j.id) nv FROM jobs j "
                "WHERE j.account_id=? AND j.status!='draft' ORDER BY j.id DESC LIMIT 200", (aid,)) if aid else []
    return page(request, "jobs.html", jobs=jobs)


@router.get("/jobs/{job_id}")
async def job_page(request: Request, job_id: int):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        return go("/jobs", err="Topilmadi")
    if job["status"] == "draft":
        return go(f"/preview/{job_id}")
    vs = jobsvc.variants_of(job_id)
    return page(request, "job.html", job=job, variants=vs, medias={v["id"]: jobsvc.media_of(v) for v in vs})


@router.get("/api/jobs/{job_id}")
async def job_api(job_id: int):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    idx = {v["id"]: v["idx"] + 1 for v in jobsvc.variants_of(job_id)}
    rows = db.q("SELECT id,title,status,error,sent_at,variant_id,views,forwards,reactions,replies,deleted "
                "FROM job_targets WHERE job_id=? ORDER BY id", (job_id,))
    targets = []
    for t in rows:
        d = dict(t)
        d["vn"] = idx.get(t["variant_id"])
        targets.append(d)
    return {"job": dict(job), "status_label": STATUS_LABELS.get(job["status"], job["status"]), "server_now": db.now(),
            "targets": targets, "pending": sum(1 for t in targets if t["status"] in ("pending", "sending")),
            "skipped": sum(1 for t in targets if t["status"] == "skipped"), "op": jobops.ops.get(job_id)}


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(job_id: int):
    db.ex("UPDATE jobs SET status='cancelled' WHERE id=? AND status IN ('queued','scheduled','running','waiting')", (job_id,))
    return go(f"/jobs/{job_id}", msg="To'xtatildi")


@router.post("/jobs/{job_id}/resume")
async def job_resume(job_id: int):
    j = db.one("SELECT * FROM jobs WHERE id=? AND status IN ('cancelled','interrupted','failed','waiting')", (job_id,))
    if not j:
        return go(f"/jobs/{job_id}", err="Davom ettirib bo'lmaydi")
    if not (await manager.get(j["account_id"]).status()).get("authorized"):
        return go("/accounts", err="Bu akkaunt Telegram'ga ulanmagan")
    db.ex("UPDATE jobs SET status='queued', finished_at=NULL, error=NULL WHERE id=?", (job_id,))
    sender.start(job_id)
    return go(f"/jobs/{job_id}", msg="Davom ettirilmoqda")


@router.post("/jobs/{job_id}/retry")
async def job_retry(job_id: int):
    j = db.one("SELECT * FROM jobs WHERE id=? AND status NOT IN ('running','queued','draft')", (job_id,))
    if not j:
        return go(f"/jobs/{job_id}", err="Hozir qayta urinib bo'lmaydi")
    if not (await manager.get(j["account_id"]).status()).get("authorized"):
        return go("/accounts", err="Bu akkaunt Telegram'ga ulanmagan")
    db.ex("UPDATE job_targets SET status='pending', error=NULL WHERE job_id=? AND status='failed'", (job_id,))
    db.ex("UPDATE jobs SET status='queued', finished_at=NULL, error=NULL WHERE id=?", (job_id,))
    sender.start(job_id)
    return go(f"/jobs/{job_id}", msg="Xato bo'lgan guruhlarga qayta yuborilmoqda")


@router.post("/jobs/{job_id}/delete")
async def job_delete(job_id: int):
    db.ex("DELETE FROM jobs WHERE id=? AND status NOT IN ('running','queued')", (job_id,))
    return go("/jobs", msg="O'chirildi")


@router.post("/jobs/{job_id}/refresh-stats")
async def job_refresh(job_id: int):
    if jobops.is_busy(job_id):
        return go(f"/jobs/{job_id}", err="Boshqa amal bajarilmoqda")
    asyncio.create_task(jobops.refresh_stats(job_id))
    return go(f"/jobs/{job_id}", msg="Statistika yangilanmoqda...")


@router.post("/jobs/{job_id}/delete-messages")
async def job_delete_messages(job_id: int):
    j = db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
    if not j or j["status"] in ("running", "queued"):
        return go(f"/jobs/{job_id}", err="Yuborish tugagandan keyin o'chirish mumkin")
    if jobops.is_busy(job_id):
        return go(f"/jobs/{job_id}", err="Boshqa amal bajarilmoqda")
    asyncio.create_task(jobops.delete_messages(job_id))
    return go(f"/jobs/{job_id}", msg="Yuborilgan xabarlar guruhlardan o'chirilmoqda...")


@router.post("/jobs/{job_id}/edit")
async def job_edit(request: Request, job_id: int):
    f = await request.form()
    texts = {int(k[5:]): (v or "").strip() for k, v in f.items() if k.startswith("text_") and k[5:].isdigit()}
    if not texts or any(not t for t in texts.values()):
        return go(f"/jobs/{job_id}", err="Yangi matn bo'sh bo'lmasin")
    if jobops.is_busy(job_id):
        return go(f"/jobs/{job_id}", err="Boshqa amal bajarilmoqda")
    asyncio.create_task(jobops.edit_messages(job_id, texts))
    return go(f"/jobs/{job_id}", msg="Yuborilgan xabarlar tahrirlanmoqda...")


# ---------------- kampaniyalar ----------------
@router.get("/campaigns")
async def campaigns_page(request: Request):
    aid = acc_id(request)
    rows = []
    for c in (db.q("SELECT * FROM campaigns WHERE account_id=? ORDER BY id DESC", (aid,)) if aid else []):
        d = dict(c)
        d["nv"] = db.one("SELECT COUNT(*) c FROM campaign_variants WHERE campaign_id=?", (c["id"],))["c"]
        d["ng"] = len(json.loads(c["target_json"] or "[]"))
        d["runs"] = db.one("SELECT COUNT(*) c FROM jobs WHERE campaign_id=? AND status!='draft'", (c["id"],))["c"]
        d["preview"] = (db.one("SELECT text FROM campaign_variants WHERE campaign_id=? ORDER BY idx LIMIT 1", (c["id"],)) or {"text": ""})["text"]
        rows.append(d)
    return page(request, "campaigns.html", items=rows)


@router.post("/campaigns/{cid}/run")
async def campaign_run(cid: int):
    c = db.one("SELECT * FROM campaigns WHERE id=?", (cid,))
    if not c:
        return go("/campaigns", err="Kampaniya topilmadi")
    if not (await manager.get(c["account_id"]).status()).get("authorized"):
        return go("/accounts", err="Bu akkaunt Telegram'ga ulanmagan")
    jid = jobsvc.run_campaign(cid)
    if not jid:
        return go("/campaigns", err="Yuboradigan guruh yo'q (guruhlar o'chgan, qora ro'yxatda yoki reklama taqiqlangan)")
    sender.start(jid)
    return go(f"/jobs/{jid}", msg="Kampaniya ishga tushdi")


@router.post("/campaigns/{cid}/toggle")
async def campaign_toggle(cid: int):
    c = db.one("SELECT * FROM campaigns WHERE id=?", (cid,))
    if not c or c["recurrence"] == "none":
        return go("/campaigns", err="Bu kampaniyada takrorlash sozlanmagan")
    if c["rec_active"]:
        db.ex("UPDATE campaigns SET rec_active=0, next_run=NULL WHERE id=?", (cid,))
        return go("/campaigns", msg="Takrorlash to'xtatildi")
    jobsvc.activate_campaign(cid)
    return go("/campaigns", msg="Takrorlash yoqildi")


@router.post("/campaigns/{cid}/delete")
async def campaign_delete(cid: int):
    db.ex("UPDATE jobs SET campaign_id=NULL WHERE campaign_id=?", (cid,))
    db.ex("DELETE FROM campaigns WHERE id=?", (cid,))
    db.ex("DELETE FROM variant_ptr WHERE key=?", (f"c{cid}",))
    return go("/campaigns", msg="Kampaniya o'chirildi")


# ---------------- media kutubxonasi ----------------
@router.get("/media-library")
async def media_page(request: Request):
    items = []
    for m in db.q("SELECT * FROM media_items ORDER BY created_at DESC"):
        d = dict(m)
        d["used"] = bool(db.one("SELECT 1 FROM job_variants WHERE media_json LIKE ? UNION SELECT 1 FROM campaign_variants "
                                "WHERE media_json LIKE ?", (f'%"{m["name"]}"%', f'%"{m["name"]}"%')))
        items.append(d)
    return page(request, "media.html", items=items)


@router.get("/api/media")
async def media_api():
    return [{"name": m["name"], "kind": m["kind"], "original": m["original"], "size": m["size"]}
            for m in db.q("SELECT * FROM media_items ORDER BY created_at DESC LIMIT 300") if (MEDIA_DIR / m["name"]).exists()]


@router.post("/media-library/upload")
async def media_upload(files: list[UploadFile] = File(...)):
    n, err = 0, None
    for f in files:
        if not f.filename:
            continue
        try:
            await save_upload(f)
            n += 1
        except ValueError as e:
            err = str(e)
    if err and not n:
        return go("/media-library", err=err)
    return go("/media-library", msg=f"{n} ta fayl yuklandi" + (f" ({err})" if err else ""))


@router.post("/media-library/{name}/delete")
async def media_delete(name: str):
    name = Path(name).name
    used = db.one("SELECT 1 FROM job_variants WHERE media_json LIKE ? UNION SELECT 1 FROM campaign_variants "
                  "WHERE media_json LIKE ?", (f'%"{name}"%', f'%"{name}"%'))
    if used:
        return go("/media-library", err="Bu fayl kampaniya yoki yuborishda ishlatilgan, o'chirib bo'lmaydi")
    (MEDIA_DIR / name).unlink(missing_ok=True)
    db.ex("DELETE FROM media_items WHERE name=?", (name,))
    return go("/media-library", msg="Fayl o'chirildi")
