"""Avto-topish bo'limi: o'zbek auditoriyali guruhlarni topish, filtrlash, kunlik a'zo bo'lish."""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import db, discovery
from .core import manager
from .limits import warm_today
from .web import acc_id, go, need_account, page

router = APIRouter()

STAT_LABEL = {"ready": "Tayyor", "review": "Ko'rib chiqing", "approved": "Tasdiqlangan", "queued": "Navbatda", "joined": "A'zo bo'lindi",
              "requested": "So'rov yuborildi", "active": "Reklamaga tayyor", "noads": "Reklama mumkin emas", "blocked": "Taqiqlangan",
              "low": "Mos emas", "failed": "Xato", "rejected": "Rad etildi", "skip": "O'tkazildi"}
STAT_CLS = {"ready": "b-good", "approved": "b-good", "active": "b-good", "joined": "b-good", "review": "b-waiting", "queued": "b-running",
            "requested": "b-running", "blocked": "b-failed", "failed": "b-failed", "noads": "b-failed"}
TABS = ("overview", "candidates", "bans", "settings")


def _back(tab="overview", **kw):
    return "/autojoin?tab=" + tab


@router.get("/autojoin")
async def autojoin_page(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    cfg = discovery.get_cfg(aid)
    tab = request.query_params.get("tab", "overview")
    tab = tab if tab in TABS else "overview"
    flt = request.query_params.get("s", "")
    counts = {r["status"]: r["c"] for r in db.q("SELECT status, COUNT(*) c FROM disc_candidates WHERE account_id=? GROUP BY status", (aid,))}
    rows = []
    if tab == "candidates":
        where, args = "account_id=?", [aid]
        if flt:
            where += " AND status=?"
            args.append(flt)
        else:
            where += " AND status NOT IN ('skip','low','rejected')"
        rows = db.q(f"SELECT * FROM disc_candidates WHERE {where} ORDER BY CASE status WHEN 'review' THEN 0 WHEN 'ready' THEN 1 ELSE 2 END, score DESC LIMIT 400", args)
    recent = db.q("SELECT * FROM disc_candidates WHERE account_id=? AND status IN ('joined','requested','active','queued','review','ready') "
                  "ORDER BY id DESC LIMIT 6", (aid,)) if tab == "overview" else []
    today = discovery.joined_today(aid)
    w = warm_today(aid)
    limit = min(cfg["daily_target"], w["joins"]) if w else cfg["daily_target"]
    total = sum(counts.values())
    passed = total - sum(counts.get(k, 0) for k in ("blocked", "low", "skip", "rejected", "failed"))
    funnel = [("Topildi", total), ("Filtrdan o'tdi", passed),
              ("A'zo bo'lindi", sum(counts.get(k, 0) for k in ("joined", "requested", "active", "noads"))),
              ("Reklamaga tayyor", counts.get("active", 0))]
    bans = cfg["bans"]
    return page(request, "autojoin.html", cfg=cfg, rows=rows, counts=counts, today=today, limit=limit, warm=w, flt=flt, tab=tab,
                STAT_LABEL=STAT_LABEL, STAT_CLS=STAT_CLS, running=discovery.progress.get(aid, {}).get("running", False),
                funnel=funnel, recent=recent, total=total, ban_total=sum(len(v) for v in bans.values()) + len(cfg["block_extra"]),
                ban_text={c: "\n".join(v) for c, v in bans.items()},
                kw_text={c: "\n".join(v) for c, v in cfg["keywords"].items()}, defaults=list(discovery.DEFAULT_KEYWORDS),
                connected=bool((sv := manager.services.get(aid)) and sv.info))


def _int(v, d, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return d


@router.post("/autojoin/toggle")
async def autojoin_toggle(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    cfg = discovery.get_cfg(aid)
    db.ex("UPDATE autojoin SET active=? WHERE account_id=?", (0 if cfg["active"] else 1, aid))
    return go(request.query_params.get("next") or "/autojoin", msg="Avtomatik rejim " + ("o'chirildi" if cfg["active"] else "yoqildi"))


@router.post("/autojoin/save")
async def autojoin_save(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    discovery.get_cfg(aid)
    f = await request.form()
    kws = {}
    for c in discovery.DEFAULT_KEYWORDS:
        lst = [x.strip() for x in (f.get("kw_" + c) or "").splitlines() if x.strip()]
        if lst:
            kws[c] = lst[:40]
    dmin = _int(f.get("min_delay"), 120, 30, 3600)
    dmax = max(dmin, _int(f.get("max_delay"), 300, 30, 7200))
    db.ex("UPDATE autojoin SET daily_target=?, min_members=?, min_per_day=?, min_uz=?, ad_wait_days=?, min_delay=?, max_delay=?, "
          "manual_all=?, keywords_json=? WHERE account_id=?",
          (_int(f.get("daily_target"), 20, 1, 100), _int(f.get("min_members"), 500, 0, 10_000_000),
           _int(f.get("min_per_day"), 10, 0, 100000), _int(f.get("min_uz"), 40, 0, 100), _int(f.get("ad_wait_days"), 2, 0, 30),
           dmin, dmax, 1 if f.get("manual_all") else 0, json.dumps(kws, ensure_ascii=False) if kws else None, aid))
    return go("/autojoin?tab=settings", msg="Sozlamalar saqlandi")


@router.post("/autojoin/bans/save")
async def bans_save(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    discovery.get_cfg(aid)
    f = await request.form()
    if f.get("action") == "restore":
        db.ex("UPDATE autojoin SET ban_json=NULL WHERE account_id=?", (aid,))
        n = discovery.recheck(aid)
        return go("/autojoin?tab=bans", msg="Standart taqiq ro'yxati tiklandi")
    bans = {}
    for i, c in enumerate(discovery.DEFAULT_BANS):
        seen, lst = set(), []
        for x in (f.get(f"ban_{i}") or "").splitlines():
            x = x.strip().lower()
            if x and x not in seen and len(x) <= 60:
                seen.add(x)
                lst.append(x)
        bans[c] = lst[:600]
    db.ex("UPDATE autojoin SET ban_json=?, block_extra=? WHERE account_id=?",
          (json.dumps(bans, ensure_ascii=False), (f.get("block_extra") or "")[:5000], aid))
    n = discovery.recheck(aid)
    return go("/autojoin?tab=bans", msg="Taqiq ro'yxati saqlandi" + (f" · {n} ta nomzod taqiqlandi" if n else ""))


@router.post("/autojoin/bans/test")
async def bans_test(request: Request):
    aid = acc_id(request)
    f = await request.form()
    if not aid:
        return JSONResponse([])
    return JSONResponse(discovery.test_text(aid, f.get("text") or ""))


@router.post("/autojoin/search")
async def autojoin_search(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    svc = manager.services.get(aid)
    if not svc or not svc.info:
        return go("/autojoin", err="Akkaunt ulanmagan")
    if discovery.progress.get(aid, {}).get("running"):
        return go("/autojoin", err="Qidiruv allaqachon ketmoqda")
    asyncio.create_task(discovery.search(aid))
    return go("/autojoin", msg="Qidiruv boshlandi (bir necha daqiqa olishi mumkin)")


@router.post("/autojoin/join-now")
async def autojoin_join_now(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    discovery.sync(aid)
    bid = discovery.plan_join(aid)
    if not bid:
        return go("/autojoin", err="Hozir a'zo bo'ladigan guruh yo'q yoki bugungi limit to'lgan / paket ketmoqda")
    return go("/autojoin", msg="A'zo bo'lish boshlandi")


@router.post("/autojoin/c/{cid}/{action}")
async def autojoin_candidate(request: Request, cid: int, action: str):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    c = db.one("SELECT * FROM disc_candidates WHERE id=? AND account_id=?", (cid, aid))
    if c and c["status"] in ("review", "ready", "low", "rejected", "approved") and action in ("approve", "reject"):
        db.ex("UPDATE disc_candidates SET status=? WHERE id=?", ("approved" if action == "approve" else "rejected", cid))
    s = request.query_params.get("s")
    return go("/autojoin?tab=candidates" + (f"&s={s}" if s else ""))


@router.post("/autojoin/clear")
async def autojoin_clear(request: Request):
    if (r := need_account(request)):
        return r
    db.ex("DELETE FROM disc_candidates WHERE account_id=? AND status IN ('low','rejected','skip','failed')", (acc_id(request),))
    return go("/autojoin?tab=candidates", msg="Mos kelmagan nomzodlar tozalandi")


@router.get("/api/autojoin/progress")
async def autojoin_progress(request: Request):
    aid = acc_id(request)
    return JSONResponse(discovery.progress.get(aid, {"running": False}))
