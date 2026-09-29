"""Guruhlar: ro'yxat, teglar, qora ro'yxat, qoidalarni tekshirish, yangi guruh qidirish."""
import asyncio
import json
import uuid

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from . import db
from .config import log
from .core import manager
from .limits import blacklist_add
from .r_posts import groups_context
from .web import acc_id, go, need_account, page

router = APIRouter()


def _ids(v):
    return [int(x) for x in v if str(x).lstrip("-").isdigit()]


@router.get("/groups")
async def groups_page(request: Request):
    aid = acc_id(request)
    if not aid:
        return page(request, "groups.html", groups=[], lists=[], tags={}, bl=[], st={}, prog=None, deltas={})
    groups, lists, tags = groups_context(aid)
    week = db.q("SELECT tg_id, MIN(day) d FROM group_members_log WHERE account_id=? AND day>=date('now','-7 day') GROUP BY tg_id", (aid,))
    deltas = {}
    for w in week:
        old = db.one("SELECT members FROM group_members_log WHERE account_id=? AND tg_id=? AND day=?", (aid, w["tg_id"], w["d"]))
        cur = next((g["members"] for g in groups if g["tg_id"] == w["tg_id"]), None)
        if old and old["members"] is not None and cur is not None:
            deltas[w["tg_id"]] = cur - old["members"]
    bl = db.q("SELECT * FROM blacklist WHERE account_id=? ORDER BY created_at DESC", (aid,))
    st = await manager.get(aid).status()
    return page(request, "groups.html", groups=groups, lists=lists, tags=tags, bl=bl, st=st,
                prog=manager.rules_progress.get(aid), deltas=deltas)


@router.post("/groups/sync")
async def groups_sync(request: Request):
    if (r := need_account(request)):
        return r
    try:
        n = await manager.get(acc_id(request)).fetch_groups()
    except Exception as e:
        log.exception("Guruhlarni yangilash")
        return go("/groups", err=f"Guruhlarni olib bo'lmadi: {e}")
    return go("/groups", msg=f"{n} ta guruh/kanal yangilandi")


@router.post("/groups/lists")
async def list_save(request: Request, name: str = Form(...), tg_ids: list[int] = Form(default=[])):
    aid = acc_id(request)
    name = name.strip()
    if not aid or not name or not tg_ids:
        return go("/groups", err="Ro'yxat nomini kiriting va kamida bitta guruh tanlang")
    ex = db.one("SELECT id FROM group_lists WHERE account_id=? AND name=?", (aid, name))
    lid = ex["id"] if ex else db.ex("INSERT INTO group_lists(account_id,name) VALUES(?,?)", (aid, name))
    db.ex("DELETE FROM group_list_items WHERE list_id=?", (lid,))
    db.many("INSERT OR IGNORE INTO group_list_items(list_id,tg_id) VALUES(?,?)", [(lid, i) for i in tg_ids])
    return go("/groups", msg=f"'{name}' ro'yxati saqlandi ({len(tg_ids)} ta guruh)")


@router.post("/groups/lists/{lid}/delete")
async def list_delete(lid: int):
    db.ex("DELETE FROM group_lists WHERE id=?", (lid,))
    return go("/groups", msg="Ro'yxat o'chirildi")


@router.post("/groups/tags")
async def tags_edit(request: Request, tg_ids: list[int] = Form(default=[]), tags: str = Form(""), mode: str = Form("add")):
    aid = acc_id(request)
    names = [t.strip().lower().lstrip("#") for t in tags.replace(";", ",").split(",") if t.strip()]
    if not aid or not tg_ids:
        return go("/groups", err="Guruhlarni tanlang")
    if mode == "clear":
        db.many("DELETE FROM group_tags WHERE account_id=? AND tg_id=?", [(aid, i) for i in tg_ids])
        return go("/groups", msg="Teglar tozalandi")
    if not names:
        return go("/groups", err="Teg nomini kiriting (masalan: toshkent, savdo)")
    if mode == "remove":
        db.many("DELETE FROM group_tags WHERE account_id=? AND tg_id=? AND tag=?", [(aid, i, t) for i in tg_ids for t in names])
        return go("/groups", msg="Teglar olib tashlandi")
    if mode == "set":
        db.many("DELETE FROM group_tags WHERE account_id=? AND tg_id=?", [(aid, i) for i in tg_ids])
    db.many("INSERT OR IGNORE INTO group_tags(account_id,tg_id,tag) VALUES(?,?,?)", [(aid, i, t) for i in tg_ids for t in names])
    return go("/groups", msg=f"{len(tg_ids)} ta guruhga teg qo'shildi")


@router.post("/groups/blacklist")
async def bl_add(request: Request, tg_ids: list[int] = Form(default=[]), reason: str = Form("Qo'lda qo'shildi")):
    aid = acc_id(request)
    if not aid or not tg_ids:
        return go("/groups", err="Guruhlarni tanlang")
    titles = {g["tg_id"]: g["title"] for g in db.q("SELECT tg_id,title FROM groups WHERE account_id=?", (aid,))}
    for i in tg_ids:
        blacklist_add(aid, i, titles.get(i, ""), reason or "Qo'lda qo'shildi")
    return go("/groups", msg=f"{len(tg_ids)} ta guruh qora ro'yxatga qo'shildi")


@router.post("/groups/blacklist/remove")
async def bl_remove(request: Request, tg_ids: list[int] = Form(default=[])):
    aid = acc_id(request)
    if aid and tg_ids:
        db.many("DELETE FROM blacklist WHERE account_id=? AND tg_id=?", [(aid, i) for i in tg_ids])
    return go("/groups", msg="Qora ro'yxatdan chiqarildi")


@router.post("/groups/ads-ok")
async def ads_ok(request: Request, tg_ids: list[int] = Form(default=[]), value: int = Form(1)):
    aid = acc_id(request)
    if aid and tg_ids:
        db.many("UPDATE groups SET ads_ok=? WHERE account_id=? AND tg_id=?", [(int(bool(value)), aid, i) for i in tg_ids])
    return go("/groups", msg="Saqlandi")


# ---------------- qoidalarni tekshirish (fon jarayoni) ----------------
async def _check_rules(aid: int):
    prog = manager.rules_progress[aid]
    svc = manager.get(aid)
    rows = db.q("SELECT tg_id FROM groups WHERE account_id=?", (aid,))
    prog.update(total=len(rows), done=0, fail=0, running=True)
    try:
        for r in rows:
            try:
                info = await svc.group_info(r["tg_id"])
                db.ex("UPDATE groups SET about=?, slowmode=?, no_media=?, no_links=?, ads_flag=?, checked_at=? "
                      "WHERE account_id=? AND tg_id=?", (info["about"], info["slowmode"], info["no_media"],
                                                         info["no_links"], info["ads_flag"], db.now(), aid, r["tg_id"]))
            except Exception as e:
                prog["fail"] += 1
                name = type(e).__name__
                if name == "FloodWaitError":
                    await asyncio.sleep(min(getattr(e, "seconds", 30) + 1, 120))
                log.info("Qoida tekshirish xatosi %s: %s", r["tg_id"], name)
            prog["done"] += 1
            await asyncio.sleep(1.2)
    finally:
        prog["running"] = False


@router.post("/groups/rules/start")
async def rules_start(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    if manager.rules_progress.get(aid, {}).get("running"):
        return go("/groups", err="Tekshiruv allaqachon ketmoqda")
    if not (await manager.get(aid).status()).get("authorized"):
        return go("/accounts", err="Akkaunt ulanmagan")
    manager.rules_progress[aid] = {"running": True, "total": 0, "done": 0, "fail": 0}
    asyncio.create_task(_check_rules(aid))
    return go("/groups", msg="Guruh qoidalari tekshirilmoqda (bir necha daqiqa ketishi mumkin)")


@router.get("/api/groups/rules-progress")
async def rules_progress(request: Request):
    return manager.rules_progress.get(acc_id(request)) or {"running": False, "total": 0, "done": 0, "fail": 0}


# ---------------- yangi guruhlarni topish ----------------
@router.get("/discover")
async def discover(request: Request, q: str = ""):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    results, error = [], None
    if q.strip():
        try:
            results = await manager.get(aid).search_public(q.strip())
        except Exception as e:
            error = f"Qidirishda xato: {e}"
    return page(request, "discover.html", q=q, results=results, error=error)


@router.post("/discover/join")
async def discover_join(request: Request, usernames: list[str] = Form(default=[]), q: str = Form("")):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    items = [{"kind": "username", "key": u, "ref": "@" + u} for u in usernames if u]
    if not items:
        return go("/discover?q=" + q, err="Kamida bitta guruhni belgilang")
    cols = [{"label": f"Qidiruv: {q}", "items": items}]
    bid = db.ex("INSERT INTO join_batches(account_id,filename,status,created_at,raw) VALUES(?,?,?,?,?)",
                (aid, f"Qidiruv: {q}", "draft", db.now(), json.dumps(cols, ensure_ascii=False)))
    return go(f"/join/{bid}/setup")
