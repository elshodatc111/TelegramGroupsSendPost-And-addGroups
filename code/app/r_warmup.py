"""Hisob isitish rejasi: kunlik post va a'zo bo'lish limitlari."""
import json
from datetime import datetime

from fastapi import APIRouter, Request

from . import db
from .limits import _today_start, default_plan, get_warmup, warm_today
from .web import acc_id, go, need_account, page

router = APIRouter()


def _usage(aid):
    t0 = _today_start()
    posts = db.one("SELECT COUNT(*) c FROM account_events WHERE account_id=? AND kind='sent' AND ts>=?", (aid, t0))["c"]
    joins = db.one("SELECT COUNT(*) c FROM join_targets jt JOIN join_batches jb ON jb.id=jt.batch_id "
                   "WHERE jb.account_id=? AND jt.status IN ('joined','requested') AND jt.tried_at>=?", (aid, t0))["c"]
    return posts, joins


@router.get("/warmup")
async def warmup_page(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    w = get_warmup(aid)
    plan = w["plan"] if w else default_plan()
    today = warm_today(aid)
    posts, joins = _usage(aid)
    day = None
    if w and w["start_date"]:
        day = (datetime.now().date() - datetime.strptime(w["start_date"], "%Y-%m-%d").date()).days + 1
    return page(request, "warmup.html", w=w, plan=plan, today=today, posts=posts, joins=joins, day=day,
                start_date=(w["start_date"] if w and w["start_date"] else datetime.now().strftime("%Y-%m-%d")))


def _read_plan(form):
    n = int(form.get("n") or 0)
    plan = []
    for d in range(1, n + 1):
        try:
            p, j = int(form.get(f"p{d}") or 0), int(form.get(f"j{d}") or 0)
        except ValueError:
            p = j = 0
        plan.append({"day": d, "posts": max(0, min(p, 1000)), "joins": max(0, min(j, 200))})
    return plan


@router.post("/warmup/save")
async def warmup_save(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    f = await request.form()
    plan = _read_plan(f)
    action = f.get("action", "save")
    if action == "add" and plan:
        last = plan[-1]
        plan.append({"day": len(plan) + 1, "posts": last["posts"] + 5, "joins": last["joins"] + 3})
    elif action == "remove" and len(plan) > 1:
        plan.pop()
    elif action == "reset":
        plan = default_plan()
    if not plan:
        plan = default_plan()
    start = f.get("start_date") or datetime.now().strftime("%Y-%m-%d")
    try:
        datetime.strptime(start, "%Y-%m-%d")
    except ValueError:
        return go("/warmup", err="Boshlanish sanasi noto'g'ri")
    active = 1 if action == "start" else (0 if action == "stop" else (get_warmup(aid) or {}).get("active", 0))
    db.ex("INSERT INTO warmup(account_id,active,start_date,plan_json) VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
          "active=excluded.active, start_date=excluded.start_date, plan_json=excluded.plan_json",
          (aid, active, start, json.dumps(plan)))
    msg = {"start": "Isitish rejasi yoqildi: limitlar har kuni oshib boradi", "stop": "Isitish rejasi to'xtatildi",
           "add": "Kun qo'shildi", "remove": "Oxirgi kun olib tashlandi", "reset": "Standart reja tiklandi"}.get(action, "Saqlandi")
    return go("/warmup", msg=msg)
