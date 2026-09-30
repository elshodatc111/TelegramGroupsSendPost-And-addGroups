"""Guruh auditi sahifasi: tahlil, avtomatik chiqish, bildirishnomalarni o'chirish (mute)."""
import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from . import auditor, db
from .core import manager
from .web import acc_id, go, need_account, page

router = APIRouter()


def _int(v, d, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return d


def _connected(aid):
    s = manager.services.get(aid)
    return bool(s and s.info)


@router.get("/audit")
async def audit_page(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    cfg = auditor.get_cfg(aid)
    prot = auditor.protected_ids(aid)
    v = request.query_params.get("v", "")
    allg = db.q("SELECT * FROM groups WHERE account_id=? ORDER BY (verdict='leave') DESC, members", (aid,))
    n = {"total": len(allg), "leave": 0, "keep": 0, "unknown": 0, "protected": len(prot & {g["tg_id"] for g in allg}),
         "muted": sum(1 for g in allg if g["muted"]), "unaudited": sum(1 for g in allg if not g["verdict"])}
    for g in allg:
        if g["verdict"] in ("leave", "keep", "unknown"):
            n[g["verdict"]] += 1
    def pick(g):
        if v == "leave":
            return g["verdict"] == "leave" and g["tg_id"] not in prot
        if v == "keep":
            return g["verdict"] == "keep"
        if v == "unknown":
            return g["verdict"] == "unknown" or not g["verdict"]
        if v == "protected":
            return g["tg_id"] in prot
        if v == "unmuted":
            return not g["muted"]
        return True
    rows = [g for g in allg if pick(g)][:500]
    logs = db.q("SELECT * FROM leave_log WHERE account_id=? AND reason LIKE 'Audit:%' ORDER BY id DESC LIMIT 30", (aid,))
    return page(request, "audit.html", cfg=cfg, n=n, rows=rows, prot=prot, v=v, logs=logs, running=auditor.progress.get(aid, {}).get("running", False),
                connected=_connected(aid), left_today=auditor.left_today(aid), pending=len(auditor.leave_candidates(aid)))


@router.post("/audit/toggle")
async def audit_toggle(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    cfg = auditor.get_cfg(aid)
    db.ex("UPDATE audit_cfg SET auto=? WHERE account_id=?", (0 if cfg["auto"] else 1, aid))
    return go("/audit", msg="Avtomatik audit " + ("o'chirildi" if cfg["auto"] else "yoqildi"))


@router.post("/audit/save")
async def audit_save(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    auditor.get_cfg(aid)
    f = await request.form()
    dmin = _int(f.get("min_delay"), 25, 10, 600)
    db.ex("UPDATE audit_cfg SET min_members=?, check_ads=?, check_post=?, mute_all=?, max_leave=?, min_delay=?, max_delay=? WHERE account_id=?",
          (_int(f.get("min_members"), 100, 0, 10_000_000), 1 if f.get("check_ads") else 0, 1 if f.get("check_post") else 0,
           1 if f.get("mute_all") else 0, _int(f.get("max_leave"), 10, 1, 200), dmin, max(dmin, _int(f.get("max_delay"), 60, 10, 1200)), aid))
    if db.one("SELECT 1 FROM groups WHERE account_id=? AND verdict IS NOT NULL LIMIT 1", (aid,)):
        auditor.evaluate(aid)
    return go("/audit", msg="Sozlamalar saqlandi, hukmlar qayta hisoblandi")


def _bg(coro):
    t = asyncio.create_task(coro)
    t.add_done_callback(lambda x: x.exception() and None)


@router.post("/audit/scan")
async def audit_scan(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    if not _connected(aid):
        return go("/audit", err="Akkaunt ulanmagan")
    if auditor.progress.get(aid, {}).get("running"):
        return go("/audit", err="Boshqa amal ketmoqda")
    f = await request.form()
    _bg(auditor.scan(aid, refresh_all=bool(f.get("full"))))
    return go("/audit", msg="Tahlil boshlandi")


@router.post("/audit/leave")
async def audit_leave(request: Request, tg_ids: list[int] = Form(default=[]), sel: str = Form(default="")):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    if not _connected(aid):
        return go("/audit", err="Akkaunt ulanmagan")
    if auditor.progress.get(aid, {}).get("running"):
        return go("/audit", err="Boshqa amal ketmoqda")
    if sel and not tg_ids:
        return go("/audit", err="Avval guruhlarni belgilang")
    if not auditor.leave_candidates(aid):
        return go("/audit", err="Chiqiladigan guruh yo'q. Avval «Tahlil qilish» ni bosing")
    _bg(auditor.run_leave(aid, only=tg_ids or None, ignore_cap=bool(tg_ids)))
    return go("/audit", msg="Guruhlardan chiqish boshlandi (xavfsizlik uchun kechikish bilan)")


@router.post("/audit/mute")
async def audit_mute(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    if not _connected(aid):
        return go("/audit", err="Akkaunt ulanmagan")
    if auditor.progress.get(aid, {}).get("running"):
        return go("/audit", err="Boshqa amal ketmoqda")
    _bg(auditor.mute_pending(aid))
    return go("/audit", msg="Bildirishnomalarni o'chirish boshlandi")


@router.post("/audit/protect/{tg_id}")
async def audit_protect(request: Request, tg_id: int):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    if db.one("SELECT 1 FROM group_tags WHERE account_id=? AND tg_id=? AND tag=?", (aid, tg_id, auditor.PROTECT_TAG)):
        db.ex("DELETE FROM group_tags WHERE account_id=? AND tg_id=? AND tag=?", (aid, tg_id, auditor.PROTECT_TAG))
    else:
        db.ex("INSERT OR IGNORE INTO group_tags(account_id,tg_id,tag) VALUES(?,?,?)", (aid, tg_id, auditor.PROTECT_TAG))
    auditor.evaluate(aid)
    v = request.query_params.get("v")
    return go("/audit" + (f"?v={v}" if v else ""))


@router.get("/api/audit/progress")
async def audit_progress(request: Request):
    return JSONResponse(auditor.progress.get(acc_id(request), {"running": False}))
