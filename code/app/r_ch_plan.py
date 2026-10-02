"""Telegram SMM: kontent reja (qoralama, rejalashtirilgan, yuborilgan postlar)."""
import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Form, Request

from . import ai, ch_data, ch_plan, ch_track, db
from .web import go, need_channel, page

router = APIRouter()
STATUS = {"draft": "Qoralama", "scheduled": "Rejalashtirilgan", "sent": "Yuborildi", "failed": "Xato",
          "cancelled": "Bekor qilindi", "missed": "O'tkazib yuborildi"}
BADGE = {"draft": "waiting", "scheduled": "scheduled", "sent": "sent", "failed": "failed", "cancelled": "waiting", "missed": "failed"}


def _when(v: str | None):
    v = (v or "").strip().replace("T", " ")
    if not v:
        return None
    return v + ":00" if len(v) == 16 else v


@router.get("/ch/plan")
async def ch_plan_page(request: Request):
    if (r := need_channel(request)):
        return r
    ch = request.state.channel
    flt = request.query_params.get("s", "")
    sql, args = "SELECT * FROM ch_plan WHERE channel_id=?", [ch["id"]]
    if flt in STATUS:
        sql += " AND status=?"
        args.append(flt)
    items = db.q(sql + " ORDER BY COALESCE(scheduled_at, created_at) DESC LIMIT 200", args)
    counts = {r["status"]: r["c"] for r in db.q("SELECT status, COUNT(*) c FROM ch_plan WHERE channel_id=? GROUP BY status", (ch["id"],))}
    edit = None
    if request.query_params.get("edit", "").isdigit():
        edit = db.one("SELECT * FROM ch_plan WHERE id=? AND channel_id=?", (int(request.query_params["edit"]), ch["id"]))
    links = {it["id"]: ch_track.plan_link(it, ch) for it in items}
    clicks = ch_track.clicks_by_code(ch["id"])
    return page(request, "ch_plan.html", ch=ch, ai_ok=ai.configured(), items=items, links=links, clicks=clicks, track_ok=ch_track.enabled(), counts=counts, flt=flt, STATUS_L=STATUS, BADGE=BADGE, edit=edit,
                media=ch_plan.media_list, footer=ch_data.lead_footer(ch), now=datetime.now().strftime("%Y-%m-%dT%H:%M"))


@router.post("/ch/plan/save")
async def ch_plan_save(request: Request):
    ch = request.state.channel
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    form = await request.form()
    text = (form.get("text") or "").strip()
    pid = int(form.get("id") or 0)
    names, mtype = await ch_data.save_uploads(ch["id"], form.getlist("files"))
    if not text and not names:
        return go("/ch/plan", err="Matn yoki media kiriting")
    if form.get("add_footer") and ch_data.lead_footer(ch) and ch_data.lead_footer(ch) not in text:
        text = text.rstrip() + "\n\n" + ch_data.lead_footer(ch)
    when = _when(form.get("when"))
    status = "scheduled" if when else "draft"
    pm = "html" if form.get("parse_mode") == "html" else "none"
    title = (form.get("title") or text[:60] or "Post").strip()[:250]
    if pid:
        old = db.one("SELECT * FROM ch_plan WHERE id=? AND channel_id=?", (pid, ch["id"]))
        if not old or old["status"] == "sent":
            return go("/ch/plan", err="Bu postni tahrirlab bo'lmaydi")
        if not names:
            names, mtype = ch_plan.media_list(old), old["media_type"]
        db.ex("UPDATE ch_plan SET title=?, text=?, media_json=?, media_type=?, parse_mode=?, scheduled_at=?, status=?, error=NULL WHERE id=?",
              (title, text, json.dumps(names), mtype, pm, when, status, pid))
    else:
        pid = db.ex("INSERT INTO ch_plan(channel_id,title,text,media_json,media_type,parse_mode,scheduled_at,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (ch["id"], title, text, json.dumps(names), mtype, pm, when, status, db.now()))
    ch_track.ensure_plan_code(pid, ch["id"], title)
    return go("/ch/plan", msg="Rejalashtirildi" if when else "Qoralama saqlandi")


@router.post("/ch/plan/{pid}/send")
async def ch_plan_send(request: Request, pid: int):
    ch = request.state.channel
    it = db.one("SELECT * FROM ch_plan WHERE id=?", (pid,))
    if not ch or not it or it["channel_id"] != ch["id"]:
        return go("/ch/plan", err="Post topilmadi")
    ok = await ch_plan.send_item(pid)
    if not ok:
        row = db.one("SELECT error FROM ch_plan WHERE id=?", (pid,))
        return go("/ch/plan", err="Yuborilmadi: " + ((row["error"] if row else None) or "noma'lum xato"))
    return go("/ch/plan", msg="Kanalga yuborildi")


@router.post("/ch/plan/{pid}/cancel")
async def ch_plan_cancel(request: Request, pid: int):
    ch = request.state.channel
    if ch:
        db.ex("UPDATE ch_plan SET status='cancelled' WHERE id=? AND channel_id=? AND status IN ('draft','scheduled','failed','missed')", (pid, ch["id"]))
    return go("/ch/plan", msg="Bekor qilindi")


@router.post("/ch/plan/{pid}/delete")
async def ch_plan_delete(request: Request, pid: int):
    ch = request.state.channel
    if ch:
        db.ex("DELETE FROM ch_plan WHERE id=? AND channel_id=? AND status!='sent'", (pid, ch["id"]))
    return go("/ch/plan", msg="O'chirildi")


@router.post("/ch/plan/{pid}/approve")
async def ch_plan_approve(request: Request, pid: int):
    """Qoralamani (vaqti belgilangan) tasdiqlab, rejalashtirilganga o'tkazadi."""
    ch = request.state.channel
    it = db.one("SELECT * FROM ch_plan WHERE id=?", (pid,))
    if not ch or not it or it["channel_id"] != ch["id"] or it["status"] != "draft":
        return go("/ch/plan", err="Post topilmadi")
    if not it["scheduled_at"] or it["scheduled_at"] <= datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
        return go(f"/ch/plan?edit={pid}", err="Kelajakdagi yuborish vaqtini belgilang")
    if (it["text"] or "").startswith("[Qoralama: matn yozilmagan]"):
        return go(f"/ch/plan?edit={pid}", err="Avval post matnini yozing")
    db.ex("UPDATE ch_plan SET status='scheduled' WHERE id=?", (pid,))
    return go("/ch/plan", msg="Rejalashtirildi")
