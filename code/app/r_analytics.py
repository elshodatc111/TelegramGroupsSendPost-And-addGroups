"""Guruhlar tahlili (ko'rish, a'zolar, ball) va guruhdan chiqish."""
import asyncio

from fastapi import APIRouter, Form, Request

from . import analytics, db
from .leaver import leave_group
from .web import acc_id, go, need_account, page

router = APIRouter()


@router.get("/group-stats")
async def group_stats(request: Request, sort: str = "score"):
    if (r := need_account(request)):
        return r
    rows = analytics.group_scores(acc_id(request))
    keys = {"score": lambda g: -(g["score"] if g["score"] is not None else -1), "views": lambda g: -(g["avg_views"] or 0),
            "members": lambda g: -(g["members"] or 0), "title": lambda g: (g["title"] or "").lower()}
    rows.sort(key=keys.get(sort, keys["score"]))
    tot = sum(g["members"] or 0 for g in rows)
    return page(request, "group_stats.html", rows=rows, sort=sort, total_members=tot)


@router.get("/group-stats/{tg_id}")
async def group_stat_detail(request: Request, tg_id: int):
    if (r := need_account(request)):
        return r
    d = analytics.group_detail(acc_id(request), tg_id)
    if not d:
        return go("/group-stats", err="Guruh topilmadi")
    score = next((g for g in analytics.group_scores(acc_id(request)) if g["tg_id"] == tg_id), None)
    return page(request, "group_detail.html", d=d, sc=score)


@router.get("/groups/leave")
async def leave_page(request: Request):
    if (r := need_account(request)):
        return r
    aid, uid = acc_id(request), request.state.user["id"]
    return page(request, "leave.html", cfg=analytics.leave_settings(uid), cands=analytics.leave_candidates(aid, uid),
                logs=db.q("SELECT * FROM leave_log WHERE account_id=? ORDER BY id DESC LIMIT 100", (aid,)))


@router.post("/groups/leave/settings")
async def leave_settings(request: Request):
    f = await request.form()
    for k in ("on", "bad", "low"):
        db.set_setting("leave_" + k, "1" if f.get(k) else "0")
    for k, d in (("min_posts", 5), ("min_views", 20)):
        try:
            v = max(0, int(f.get(k) or d))
        except ValueError:
            v = d
        db.set_setting("leave_" + k, v)
    return go("/groups/leave", msg="Sozlamalar saqlandi")


@router.post("/groups/leave/run")
async def leave_run(request: Request, tg_ids: list[int] = Form(default=[])):
    if (r := need_account(request)):
        return r
    aid, uid = acc_id(request), request.state.user["id"]
    if not tg_ids:
        return go("/groups/leave", err="Guruhlarni belgilang")
    cands = {c["tg_id"]: c for c in analytics.leave_candidates(aid, uid)}
    ok = fail = 0
    for t in tg_ids[:20]:
        g = db.one("SELECT title FROM groups WHERE account_id=? AND tg_id=?", (aid, t))
        if not g:
            continue
        try:
            await leave_group(aid, t, g["title"], cands.get(t, {}).get("reason", "Qo'lda"), False)
            ok += 1
            await asyncio.sleep(1.5)
        except Exception:
            fail += 1
    return go("/groups/leave", msg=f"{ok} ta guruhdan chiqildi" + (f", {fail} tasida xato" if fail else "") +
              (" (bir vaqtda 20 tagacha)" if len(tg_ids) > 20 else ""))
