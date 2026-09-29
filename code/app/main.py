"""Telegram Group Post - web platforma (FastAPI)."""
import asyncio
import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, jobops
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
                db.ex("INSERT OR IGNORE INTO media_items(name,original,kind,size,created_at) VALUES(?,?,?,?,?)",
                      (f.name, f.name, kind, f.stat().st_size, db.now()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    scan_media()
    db.ex("UPDATE jobs SET status='interrupted', next_at=NULL WHERE status IN ('running','queued')")
    db.ex("UPDATE job_targets SET status='pending' WHERE status='sending'")
    db.ex("UPDATE join_batches SET status='interrupted', next_at=NULL WHERE status IN ('running','queued')")
    sender.begin_scheduler()
    joiner.begin_scheduler()
    bg = [asyncio.create_task(manager.start_all()), asyncio.create_task(jobops.auto_refresh_loop())]
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


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if db.get_setting("password_hash") and not path.startswith("/static") and path != "/login":
        if not hmac.compare_digest(request.cookies.get("tgp_auth", ""), auth_token()):
            return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
    ids = [r["id"] for r in db.q("SELECT id FROM accounts ORDER BY id")]
    try:
        cid = int(request.cookies.get("acc", "0"))
    except ValueError:
        cid = 0
    request.state.account_id = cid if cid in ids else (ids[0] if ids else None)
    return await call_next(request)


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    log.exception("Kutilmagan xato: %s %s", request.method, request.url.path)
    return page(request, "error.html", status_code=500, detail=f"{type(exc).__name__}: {exc}")


from . import r_account, r_groups, r_inbox, r_join, r_posts, r_stats  # noqa: E402

for r in (r_account, r_posts, r_groups, r_join, r_stats, r_inbox):
    app.include_router(r.router)
