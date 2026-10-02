"""Telegram Group Post - web platforma (FastAPI)."""
import asyncio
import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auditor, backup, ch_ai, ch_collect, ch_data, ch_insight, ch_plan, ch_report, ch_track, dailyreport, db, discovery, ig_collect, ig_data, ig_plan, jobops, leaver, syscheck
from .config import CODE_DIR, MEDIA_DIR, log
from .core import joiner, manager, sender
from .web import acc_id, page

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VID_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def secret() -> str:
    s = db.get_setting("secret")
    if not s:
        s = secrets.token_hex(32)
        db.set_setting("secret", s)
    return s


def auth_token() -> str:
    return hmac.new(secret().encode(), b"tgp-auth", hashlib.sha256).hexdigest()


def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 150_000).hex()
    return f"{salt}${h}"


def check_password(pw: str) -> bool:
    stored = db.get_setting("password_hash")
    if not stored:
        return True
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_password(pw, salt), stored)


def scan_media():
    """Media papkasidagi va bazada yo'q fayllarni kutubxonaga qo'shadi."""
    known = {r["name"] for r in db.q("SELECT name FROM media_items")}
    for f in MEDIA_DIR.iterdir():
        if f.is_file() and f.name not in known:
            ext = f.suffix.lower()
            kind = "image" if ext in IMG_EXT else "video" if ext in VID_EXT else None
            if kind:
                db.ex("INSERT OR IGNORE INTO media_items(name,original,kind,size,created_at,user_id) VALUES(?,?,?,?,?,1)",
                      (f.name, f.name, kind, f.stat().st_size, db.now()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    from . import machine
    wiped = machine.check()
    db.init()
    if wiped:                         # fayl izi mos kelmadi: bazadagi ma'lumotlar ham tozalanadi
        db.wipe_all()
        db.set_setting("machine_fp", machine.fingerprint())
        db._seed()
    elif machine.check_db():
        wiped = True
        db._seed()
    if wiped:
        db.set_setting("machine_notice", "Bu boshqa kompyuter: avvalgi akkauntlar va ma'lumotlar tozalandi. Akkauntni qaytadan qo'shib faollashtiring.")
    scan_media()
    db.ex("UPDATE jobs SET status='interrupted', next_at=NULL WHERE status IN ('running','queued')")
    db.ex("UPDATE job_targets SET status='pending' WHERE status='sending'")
    db.ex("UPDATE join_batches SET status='interrupted', next_at=NULL WHERE status IN ('running','queued')")
    sender.begin_scheduler()
    joiner.begin_scheduler()
    sv = syscheck.supervise
    bg = [sv("manager", manager.start_all), sv("auto_refresh", jobops.auto_refresh_loop), sv("leaver", leaver.loop),
          sv("dailyreport", dailyreport.loop), sv("discovery", discovery.loop), sv("auditor", auditor.loop),
          sv("ch_collect", ch_collect.loop), sv("ch_plan", ch_plan.loop), sv("ch_learn", ch_ai.learn_loop), sv("backup", backup.loop),
          sv("ch_track", ch_track.loop), sv("ch_insight", ch_insight.loop), sv("ch_report", ch_report.loop),
          sv("ig_collect", ig_collect.loop), sv("ig_plan", ig_plan.loop)]
    log.info("Dastur ishga tushdi")
    yield
    for t in bg:
        t.cancel()
    await sender.stop()
    await joiner.stop()
    await manager.shutdown()


app = FastAPI(title="Telegram Group Post", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=CODE_DIR / "app" / "static"), name="static")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")


PUBLIC = ("/static", "/login", "/logout", "/ig-pub/")


def _is_ch(path: str) -> bool:
    return path == "/ch" or path.startswith("/ch/")


def _is_sys(path: str) -> bool:
    return path == "/sys" or path.startswith("/sys/")


def _is_ig(path: str) -> bool:
    return path == "/ig" or path.startswith("/ig/")


def _via_tunnel(request: Request) -> bool:
    """So'rov Cloudflare tunnel orqali (internetdan) keldimi?"""
    h = request.headers
    host = (h.get("host") or "").split(":")[0].lower()
    return bool(h.get("cf-connecting-ip") or h.get("cf-ray") or host.endswith("trycloudflare.com"))


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    # Instagram avto-joylash tunneli: internetdan faqat /ig-pub/<token>/<fayl> ochiladi, qolgan hamma narsa yopiq
    if _via_tunnel(request) and not path.startswith("/ig-pub/"):
        return Response("Not found", status_code=404)
    if path.startswith("/ig-pub/"):
        return await call_next(request)
    if db.get_setting("password_hash") and not path.startswith(PUBLIC):
        if not hmac.compare_digest(request.cookies.get("tgp_auth", ""), auth_token()):
            return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
    user = db.one("SELECT * FROM users WHERE id=1")
    request.state.user = user
    db.cur_uid.set(1)
    # ---- bo'lim: Group Post (posting) yoki Kanallarim (channels) ----
    if path == "/" and request.cookies.get("ws") == "channels":
        return RedirectResponse("/ch", status_code=303)
    if path == "/" and request.cookies.get("ws") == "system":
        return RedirectResponse("/sys", status_code=303)
    if path == "/" and request.cookies.get("ws") == "instagram":
        return RedirectResponse("/ig", status_code=303)
    request.state.ws = "channels" if _is_ch(path) else "system" if _is_sys(path) else "instagram" if _is_ig(path) else "posting"
    if not path.startswith("/static") and not path.startswith("/media"):
        # Group Post akkauntlari (Kanallarim akkaunti bu yerda ko'rinmaydi)
        ids = [r["id"] for r in db.q("SELECT id FROM accounts WHERE workspace='posting' ORDER BY id")]
        try:
            cid = int(request.cookies.get("acc", "0"))
        except ValueError:
            cid = 0
        request.state.account_id = cid if cid in ids else (ids[0] if ids else None)
        # Kanallarim: faol kanallar va tanlangani
        chs = ch_data.channels()
        request.state.channels = chs
        request.state.ch_account = ch_data.channel_account()
        raw = request.cookies.get("chid", "")
        sel = next((c for c in chs if str(c["id"]) == raw), None)
        request.state.ch_all = False
        if raw == "all":
            request.state.ch_all, sel = True, None
        elif sel is None and chs:
            sel = chs[0]
        request.state.channel = sel
        # Instagram: akkauntlar va tanlangani
        igs = ig_data.accounts()
        request.state.igs = igs
        request.state.ig = next((a for a in igs if str(a["id"]) == request.cookies.get("igid", "")), igs[0] if igs else None)
    return await call_next(request)


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    log.exception("Kutilmagan xato: %s %s", request.method, request.url.path)
    return page(request, "error.html", status_code=500, detail=f"{type(exc).__name__}: {exc}")


from . import r_account, r_groups, r_inbox, r_join, r_posts, r_stats, r_warmup, r_analytics, r_calendar, r_autojoin, r_audit, r_top50  # noqa: E402
from . import r_ch, r_ch_ai, r_ch_comp, r_ch_more, r_ch_plan, r_ig, r_sys  # noqa: E402

for r in (r_account, r_posts, r_groups, r_join, r_stats, r_inbox, r_warmup, r_analytics, r_calendar, r_autojoin, r_audit, r_top50,
          r_ch, r_ch_ai, r_ch_comp, r_ch_plan, r_ch_more, r_ig, r_sys):
    app.include_router(r.router)
