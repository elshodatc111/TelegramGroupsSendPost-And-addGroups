"""Telegram SMM: bo'lim almashtirish, bosh sahifa, yagona akkaunt, kanallarni biriktirish, kanal profili."""
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from . import ai, ch_collect, ch_data, ch_stats, ch_tg, db
from .config import log
from .core import manager
from .web import go, need_channel, page

router = APIRouter()
YEAR = 60 * 60 * 24 * 365


def _safe(next_: str, default="/ch") -> str:
    return next_ if next_.startswith("/") and not next_.startswith("//") else default


# ---------------------------------------------------------------- bo'lim va kanal tanlash
@router.post("/ws/switch")
async def ws_switch(ws: str = Form("posting")):
    ws = ws if ws in ("channels", "system", "instagram", "meet") else "posting"
    resp = RedirectResponse({"channels": "/ch", "system": "/sys", "instagram": "/ig", "meet": "/meet"}.get(ws, "/"), status_code=303)
    resp.set_cookie("ws", ws, max_age=YEAR, samesite="lax")
    return resp


@router.post("/ch/select")
async def ch_select(id: str = Form("all"), next: str = Form("/ch")):
    resp = RedirectResponse(_safe(next), status_code=303)
    resp.set_cookie("chid", id if id == "all" or id.isdigit() else "all", max_age=YEAR, samesite="lax")
    return resp


# ---------------------------------------------------------------- bosh sahifa
def _card(ch) -> dict:
    cid = ch["id"]
    s = ch_stats.source_stats(cid, 0, 30)
    mem = db.one("SELECT updated_at, version FROM ch_memory WHERE channel_id=?", (cid,))
    return {"ch": ch, "s": s, "g7": ch_stats.growth_delta(cid, 0, 7),
            "comps": len(ch_data.competitors(cid)),
            "plan": db.one("SELECT COUNT(*) c FROM ch_plan WHERE channel_id=? AND status='scheduled'", (cid,))["c"],
            "ideas": db.one("SELECT COUNT(*) c FROM ch_ideas WHERE channel_id=? AND kind IN ('manual','daily')", (cid,))["c"],
            "memo": mem, "cost": ai.month_cost(cid), "prog": ch_collect.progress.get(cid)}


@router.get("/ch")
async def ch_home(request: Request):
    acc = ch_data.channel_account()
    connected = bool(acc and manager.services.get(acc["id"]) and manager.services[acc["id"]].info)
    chans = request.state.channels
    cur = request.state.channel
    cards = [_card(c) for c in ([cur] if cur else chans)]
    return page(request, "ch_home.html", acc=acc, connected=connected, cards=cards, total=len(chans),
                ai_ok=ai.configured(), month=datetime.now().strftime("%Y-%m"),
                total_cost=sum(c["cost"] for c in cards) if cards else 0)


# ---------------------------------------------------------------- yagona akkaunt
def _acc(aid: int):
    a = db.one("SELECT * FROM accounts WHERE id=? AND workspace='channels'", (aid,))
    return a


@router.get("/ch/account")
async def ch_account(request: Request):
    acc = ch_data.channel_account()
    info = None
    if acc:
        svc = manager.get(acc["id"])
        info = svc.info
        if not info:
            try:
                await svc.status()
                info = svc.info
            except Exception:
                pass
    return page(request, "ch_account.html", acc=acc, info=info,
                login_id=int(request.query_params.get("login", 0) or 0), step=request.query_params.get("step", ""),
                api_ok=bool(db.get_setting("api_id") and db.get_setting("api_hash")),
                n_ch=len(ch_data.channels(None)) if acc else 0)


@router.post("/ch/account/add")
async def ch_account_add(request: Request, name: str = Form(""), phone: str = Form(...)):
    if ch_data.channel_account():
        return go("/ch/account", err="Telegram SMM uchun faqat bitta akkaunt ulanadi")
    if not (db.get_setting("api_id") and db.get_setting("api_hash")):
        return go("/ch/settings", err="Avval API ID va API HASH ni kiriting")
    phone = phone.strip()
    digits = "".join(c for c in phone if c.isdigit())
    clash = db.one("SELECT id FROM accounts WHERE workspace='posting' AND REPLACE(REPLACE(phone,'+',''),' ','')=?", (digits,))
    if clash:
        return go("/ch/account", err="Bu raqam Telegram Guruhlar bo'limida ishlatilmoqda. Telegram SMM uchun alohida raqam kerak "
                                     "(Telegram Guruhlar akkaunti cheklov olsa, kanal boshqaruvi ham yo'qolmasligi uchun).")
    aid = manager.create((name.strip() or "Kanal akkaunti"), request.state.user["id"], "channels")
    try:
        await manager.get(aid).send_code(phone)
    except Exception as e:
        await manager.delete(aid)
        log.warning("Kanal akkaunti: kod yuborilmadi", exc_info=True)
        return go("/ch/account", err=f"Kod yuborilmadi: {e}")
    return go(f"/ch/account?login={aid}&step=code", msg="Telegram'ga kelgan kodni kiriting")


async def _done(aid):
    await manager.get(aid).status()
    return go("/ch/channels?scan=1", msg="Akkaunt ulandi. Endi rivojlantiriladigan kanallarni tanlang")


@router.post("/ch/account/{aid}/code")
async def ch_account_code(aid: int, code: str = Form(...)):
    if not _acc(aid):
        return go("/ch/account", err="Akkaunt topilmadi")
    try:
        res = await manager.get(aid).sign_in_code(code.strip().replace(" ", ""))
    except Exception as e:
        return go(f"/ch/account?login={aid}&step=code", err=f"Kod noto'g'ri yoki eskirgan: {e}")
    if res == "password":
        return go(f"/ch/account?login={aid}&step=password", msg="Akkauntda 2 bosqichli parol bor, uni kiriting")
    return await _done(aid)


@router.post("/ch/account/{aid}/password")
async def ch_account_password(aid: int, password: str = Form(...)):
    if not _acc(aid):
        return go("/ch/account", err="Akkaunt topilmadi")
    try:
        await manager.get(aid).sign_in_password(password)
    except Exception as e:
        return go(f"/ch/account?login={aid}&step=password", err=f"Parol noto'g'ri: {e}")
    return await _done(aid)


@router.post("/ch/account/{aid}/reconnect")
async def ch_account_reconnect(aid: int, phone: str = Form(...)):
    if not _acc(aid):
        return go("/ch/account", err="Akkaunt topilmadi")
    try:
        await manager.get(aid).send_code(phone.strip())
    except Exception as e:
        return go("/ch/account", err=f"Kod yuborilmadi: {e}")
    return go(f"/ch/account?login={aid}&step=code", msg="Telegram'ga kelgan kodni kiriting")


@router.post("/ch/account/{aid}/logout")
async def ch_account_logout(aid: int):
    if not _acc(aid):
        return go("/ch/account", err="Akkaunt topilmadi")
    try:
        await manager.get(aid).logout()
    except Exception as e:
        return go("/ch/account", err=str(e))
    return go("/ch/account", msg="Akkauntdan chiqildi (qayta ulash mumkin)")


@router.post("/ch/account/{aid}/delete")
async def ch_account_delete(aid: int):
    if not _acc(aid):
        return go("/ch/account", err="Akkaunt topilmadi")
    await manager.delete(aid)
    resp = go("/ch/account", msg="Akkaunt va unga biriktirilgan kanallar ma'lumotlari o'chirildi (AI sarf hisobi saqlandi)")
    resp.delete_cookie("chid")
    return resp


# ---------------------------------------------------------------- kanallarni biriktirish
@router.get("/ch/channels")
async def ch_channels(request: Request):
    acc = ch_data.channel_account()
    attached = ch_data.channels(None)
    found, scan_err, scanned = [], None, False
    if acc and request.query_params.get("scan"):
        scanned = True
        try:
            admin, _ = await ch_tg.dialogs(acc["id"])
            have = {c["tg_id"] for c in attached if c["status"] == "active"}
            found = [a for a in admin if a["tg_id"] not in have]
        except Exception as e:
            scan_err = str(e)
            log.warning("Kanallar ro'yxati olinmadi", exc_info=True)
    return page(request, "ch_channels.html", acc=acc, attached=attached, found=found, scanned=scanned, scan_err=scan_err,
                cards={c["id"]: ch_stats.growth_delta(c["id"], 0, 7) for c in attached})


@router.post("/ch/channels/attach")
async def ch_attach(request: Request):
    acc = ch_data.channel_account()
    if not acc:
        return go("/ch/account", err="Avval akkaunt ulang")
    form = await request.form()
    wanted = {int(x) for x in form.getlist("tg_ids") if x.lstrip("-").isdigit()}
    if not wanted:
        return go("/ch/channels?scan=1", err="Kamida bitta kanalni belgilang")
    try:
        admin, _ = await ch_tg.dialogs(acc["id"])
    except Exception as e:
        return go("/ch/channels", err=str(e))
    first = None
    n = 0
    for a in admin:
        if a["tg_id"] in wanted:
            cid = ch_data.attach(acc["id"], a)
            ch_data.ensure_memory(cid)
            ch_data.log_event(cid, "attach", f"{a['title']} biriktirildi")
            first = first or cid
            n += 1
    if not n:
        return go("/ch/channels?scan=1", err="Tanlangan kanallarda admin huquqi topilmadi")
    resp = go(f"/ch/profile", msg=f"{n} ta kanal biriktirildi. Kanal profilini to'ldiring: AI shu ma'lumotlarga tayanadi")
    resp.set_cookie("chid", str(first), max_age=YEAR, samesite="lax")
    return resp


@router.post("/ch/channels/{cid}/archive")
async def ch_archive(cid: int):
    ch_data.archive(cid)
    return go("/ch/channels", msg="Kanal arxivlandi (ma'lumotlari saqlanadi)")


@router.post("/ch/channels/{cid}/restore")
async def ch_restore(cid: int):
    ch_data.restore(cid)
    return go("/ch/channels", msg="Kanal qaytarildi")


@router.post("/ch/channels/{cid}/delete")
async def ch_delete(cid: int, typed: str = Form("")):
    ch = ch_data.get(cid)
    if not ch:
        return go("/ch/channels", err="Kanal topilmadi")
    if typed.strip() != (ch["title"] or "").strip():
        return go("/ch/channels", err="Tasdiqlash uchun kanal nomini aynan yozing")
    ch_data.delete_channel(cid)
    resp = go("/ch/channels", msg="Kanal va uning ma'lumotlari o'chirildi (Telegramdagi kanalga tegilmadi)")
    resp.delete_cookie("chid")
    return resp


# ---------------------------------------------------------------- kanal profili
@router.get("/ch/profile")
async def ch_profile(request: Request):
    if (r := need_channel(request)):
        return r
    ch = request.state.channel
    return page(request, "ch_profile.html", ch=ch, types=ch_data.types(), langs=ch_data.LANGS, biz=ch_data.biz(ch),
                rub="\n".join(ch_data.rubrics(ch)), biz_fields=ch_data.BIZ_FIELDS, colors=ch_data.colors(ch))


@router.post("/ch/profile")
async def ch_profile_save(request: Request):
    ch = request.state.channel
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    fm = await request.form()
    form = dict(fm)
    old_type = ch["type_key"]
    ch_data.save_profile(ch["id"], form)
    await ch_data.save_logo(ch["id"], fm.get("logo_file"))
    if form.get("type_key") and (form["type_key"] != old_type or form.get("apply_type")):
        ch_data.apply_type_defaults(ch["id"], form["type_key"])
    msg = "Kanal profili saqlandi"
    from . import ch_track
    if ch_track.enabled():
        try:
            await ch_track.push_target(ch_data.get(ch["id"]))
        except ch_track.TrackError as e:
            msg += f" (Worker'ga havola yuborilmadi: {e})"
    return go("/ch/profile", msg=msg)
