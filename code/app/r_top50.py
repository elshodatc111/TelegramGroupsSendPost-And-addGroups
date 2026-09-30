"""Top 50 guruhlar sahifasi."""
import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from . import db, top50
from .core import manager
from .web import acc_id, go, need_account, page

router = APIRouter()


def _connected(aid):
    s = manager.services.get(aid)
    return bool(s and s.info)


@router.get("/top50")
async def top50_page(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    cat = request.query_params.get("c", "")
    cat = cat if cat in top50.CATEGORIES else ""
    rows = top50.top(aid, cat)
    allrows = top50.top(aid, "", 500)
    cnt = {c: sum(1 for r in allrows if r["category"] == c) for c in top50.CATEGORIES}
    meta = top50._meta(aid)
    rej = db.one("SELECT COUNT(*) c FROM top_groups WHERE account_id=? AND status='rejected'", (aid,))["c"]
    joined = db.one("SELECT COUNT(*) c FROM top_groups WHERE account_id=? AND status='joined'", (aid,))["c"]
    queued = sum(1 for r in allrows if r["status"] == "queued")
    from .auditor import get_cfg
    return page(request, "top50.html", rows=rows, cat=cat, cats=list(top50.CATEGORIES), cnt=cnt, total_pool=len(allrows), meta=meta,
                rej=rej, joined=joined, queued=queued, sum_members=sum((r["members"] or 0) for r in rows),
                maxm=max([r["members"] or 0 for r in rows] or [1]), min_members=get_cfg(aid)["min_members"],
                running=top50.progress.get(aid, {}).get("running", False), connected=_connected(aid))


@router.post("/top50/search")
async def top50_search(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    if not _connected(aid):
        return go("/top50", err="Akkaunt ulanmagan")
    if top50.progress.get(aid, {}).get("running"):
        return go("/top50", err="Qidiruv allaqachon ketmoqda")
    asyncio.create_task(top50.search(aid))
    return go("/top50", msg="Qidiruv boshlandi (2–4 daqiqa). Tugagach ro'yxat avtomatik yangilanadi")


@router.post("/top50/queue")
async def top50_queue(request: Request, usernames: list[str] = Form(default=[]), mode: str = Form("now")):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    if not usernames:
        return go("/top50", err="Kamida bitta guruhni belgilang")
    bid = top50.enqueue(aid, usernames, start=(mode != "setup"))
    if not bid:
        return go("/top50", err="Tanlangan guruhlar navbatga qo'shilmadi")
    if mode == "setup":
        return go(f"/join/{bid}/setup")
    return go("/top50", msg=f"{len(usernames)} ta guruh a'zo bo'lish navbatiga qo'shildi (sozlangan interval bilan)")


@router.get("/api/top50/progress")
async def top50_progress(request: Request):
    return JSONResponse(top50.progress.get(acc_id(request), {"running": False}))
