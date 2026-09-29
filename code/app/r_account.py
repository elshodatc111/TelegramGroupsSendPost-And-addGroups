"""Kirish (parol), akkauntlar va umumiy sozlamalar."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from . import db
from .config import MIN_DELAY_FLOOR, log
from .core import manager
from .web import acc_id, go, page

router = APIRouter()


def _main():
    from . import main
    return main


# ---------------- parol bilan kirish ----------------
@router.get("/login")
async def login_page(request: Request):
    if not db.get_setting("password_hash"):
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html", next=request.query_params.get("next", "/"))


@router.post("/login")
async def login(request: Request, password: str = Form(...), next: str = Form("/")):
    m = _main()
    if not m.check_password(password):
        return page(request, "login.html", next=next, err="Parol noto'g'ri", status_code=401)
    resp = RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)
    resp.set_cookie("tgp_auth", m.auth_token(), max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
    return resp


@router.post("/logout")
async def logout_app():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("tgp_auth")
    return resp


# ---------------- akkauntlar ----------------
@router.get("/accounts")
async def accounts_page(request: Request):
    login_id = int(request.query_params.get("login", 0) or 0)
    return page(request, "accounts.html", login_id=login_id, step=request.query_params.get("step", ""),
                api_ok=bool(db.get_setting("api_id") and db.get_setting("api_hash")))


@router.post("/accounts/switch")
async def account_switch(id: int = Form(...), next: str = Form("/")):
    resp = RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)
    resp.set_cookie("acc", str(id), max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@router.post("/accounts/add")
async def account_add(name: str = Form(...), phone: str = Form(...)):
    if not (db.get_setting("api_id") and db.get_setting("api_hash")):
        return go("/settings", err="Avval API ID va API HASH ni kiriting")
    aid = manager.create(name.strip() or "Akkaunt")
    try:
        await manager.get(aid).send_code(phone.strip())
    except Exception as e:
        await manager.delete(aid)
        log.warning("Kod yuborilmadi", exc_info=True)
        return go("/accounts", err=f"Kod yuborilmadi: {e}")
    resp = go(f"/accounts?login={aid}&step=code", msg="Telegram'ga kelgan kodni kiriting")
    resp.set_cookie("acc", str(aid), max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@router.post("/accounts/{aid}/reconnect")
async def account_reconnect(aid: int, phone: str = Form(...)):
    try:
        await manager.get(aid).send_code(phone.strip())
    except Exception as e:
        return go("/accounts", err=f"Kod yuborilmadi: {e}")
    return go(f"/accounts?login={aid}&step=code", msg="Telegram'ga kelgan kodni kiriting")


@router.post("/accounts/{aid}/code")
async def account_code(aid: int, code: str = Form(...)):
    try:
        res = await manager.get(aid).sign_in_code(code.strip().replace(" ", ""))
    except Exception as e:
        return go(f"/accounts?login={aid}&step=code", err=f"Kod noto'g'ri yoki eskirgan: {e}")
    if res == "password":
        return go(f"/accounts?login={aid}&step=password", msg="Akkauntda 2 bosqichli parol bor, uni kiriting")
    await manager.get(aid).status()
    return go("/accounts", msg="Akkaunt ulandi")


@router.post("/accounts/{aid}/password")
async def account_password(aid: int, password: str = Form(...)):
    try:
        await manager.get(aid).sign_in_password(password)
    except Exception as e:
        return go(f"/accounts?login={aid}&step=password", err=f"Parol noto'g'ri: {e}")
    await manager.get(aid).status()
    return go("/accounts", msg="Akkaunt ulandi")


@router.post("/accounts/{aid}/settings")
async def account_settings(aid: int, name: str = Form(...), daily_limit: int = Form(150),
                           work_start: str = Form(""), work_end: str = Form("")):
    if not db.one("SELECT 1 FROM accounts WHERE id=?", (aid,)):
        return go("/accounts", err="Akkaunt topilmadi")
    if bool(work_start) != bool(work_end):
        return go("/accounts", err="Ish vaqtining boshlanishi va tugashini ikkalasini ham kiriting (yoki ikkalasini bo'sh qoldiring)")
    db.ex("UPDATE accounts SET name=?, daily_limit=?, work_start=?, work_end=? WHERE id=?",
          (name.strip() or "Akkaunt", max(0, min(daily_limit, 2000)), work_start, work_end, aid))
    return go("/accounts", msg="Akkaunt sozlamalari saqlandi")


@router.post("/accounts/{aid}/logout")
async def account_logout(aid: int):
    try:
        await manager.get(aid).logout()
    except Exception as e:
        return go("/accounts", err=str(e))
    return go("/accounts", msg="Akkauntdan chiqildi (qayta ulash mumkin)")


@router.post("/accounts/{aid}/delete")
async def account_delete(aid: int):
    await manager.delete(aid)
    resp = go("/accounts", msg="Akkaunt va uning barcha ma'lumotlari o'chirildi")
    resp.delete_cookie("acc")
    return resp


# ---------------- sozlamalar ----------------
@router.get("/settings")
async def settings_page(request: Request):
    return page(request, "settings.html", api_id=db.get_setting("api_id", ""),
                has_hash=bool(db.get_setting("api_hash")),
                default_min=db.get_setting("default_min", 20), default_max=db.get_setting("default_max", 60),
                has_password=bool(db.get_setting("password_hash")))


@router.post("/settings/api")
async def settings_api(api_id: str = Form(...), api_hash: str = Form("")):
    api_id = api_id.strip()
    if not api_id.isdigit():
        return go("/settings", err="API ID faqat raqamlardan iborat bo'lishi kerak")
    db.set_setting("api_id", api_id)
    if api_hash.strip():
        db.set_setting("api_hash", api_hash.strip())
    if not db.get_setting("api_hash"):
        return go("/settings", err="API HASH ni kiriting")
    return go("/settings", msg="API ma'lumotlari saqlandi. Endi Akkauntlar bo'limida akkaunt qo'shing.")


@router.post("/settings/defaults")
async def defaults(min_delay: int = Form(...), max_delay: int = Form(...)):
    if min_delay < MIN_DELAY_FLOOR or max_delay < min_delay:
        return go("/settings", err=f"Interval noto'g'ri (eng kami {MIN_DELAY_FLOOR}s, max >= min)")
    db.set_setting("default_min", min_delay)
    db.set_setting("default_max", max_delay)
    return go("/settings", msg="Standart interval saqlandi")


@router.post("/settings/password")
async def set_password(request: Request, password: str = Form(""), password2: str = Form(""), current: str = Form("")):
    m = _main()
    if db.get_setting("password_hash") and not m.check_password(current):
        return go("/settings", err="Joriy parol noto'g'ri")
    if not password:
        db.del_setting("password_hash")
        resp = go("/settings", msg="Parol himoyasi o'chirildi")
        resp.delete_cookie("tgp_auth")
        return resp
    if len(password) < 4:
        return go("/settings", err="Parol kamida 4 belgi bo'lsin")
    if password != password2:
        return go("/settings", err="Parollar bir xil emas")
    db.set_setting("password_hash", m.hash_password(password))
    resp = go("/settings", msg="Parol o'rnatildi")
    resp.set_cookie("tgp_auth", m.auth_token(), max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
    return resp
