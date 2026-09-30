"""Kalendar ko'rinishi va kunlik hisobotlar."""
import calendar as cal
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, Response

from . import dailyreport, db
from .web import acc_id, go, need_account, page

router = APIRouter()
MONTHS = ["Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun", "Iyul", "Avgust", "Sentyabr", "Oktyabr", "Noyabr", "Dekabr"]


def _month(m: str):
    try:
        d = datetime.strptime(m, "%Y-%m")
    except ValueError:
        d = datetime.now().replace(day=1)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@router.get("/calendar")
async def calendar_page(request: Request, month: str = ""):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    first = _month(month)
    nxt = (first + timedelta(days=32)).replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    ev: dict[str, list] = {}

    def add(day, item):
        ev.setdefault(day, []).append(item)

    for j in db.q("SELECT id,name,status,scheduled_at,started_at,created_at,done,total FROM jobs WHERE account_id=? AND status!='draft'", (aid,)):
        when = j["scheduled_at"] if j["status"] == "scheduled" else (j["started_at"] or j["scheduled_at"] or j["created_at"])
        if when and when[:7] == first.strftime("%Y-%m"):
            add(when[:10], {"kind": "job", "id": j["id"], "title": j["name"] or f"Yuborish #{j['id']}", "time": when[11:16],
                            "status": j["status"], "info": f"{j['done']}/{j['total']}", "drag": j["status"] == "scheduled"})
    # takrorlanuvchi kampaniyalar (kelajakdagi ishga tushishlar)
    now = datetime.now()
    for c in db.q("SELECT * FROM campaigns WHERE account_id=? AND rec_active=1 AND recurrence!='none'", (aid,)):
        days = {int(x) for x in (c["rec_days"] or "").split(",") if x.strip().isdigit()} if c["recurrence"] == "weekly" else set(range(7))
        try:
            h, m = (int(x) for x in (c["rec_time"] or "10:00").split(":"))
        except ValueError:
            h, m = 10, 0
        d = first
        while d < nxt:
            cand = d.replace(hour=h, minute=m)
            if cand > now and d.weekday() in days:
                add(cand.strftime("%Y-%m-%d"), {"kind": "camp", "id": c["id"], "title": c["name"], "time": f"{h:02d}:{m:02d}",
                                                "status": "scheduled", "info": "takrorlanadi", "drag": False})
            d += timedelta(days=1)
    weeks = []
    for w in cal.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
        weeks.append([{"date": d.strftime("%Y-%m-%d"), "day": d.day, "in": d.month == first.month,
                       "today": d == now.date(), "events": sorted(ev.get(d.strftime("%Y-%m-%d"), []), key=lambda e: e["time"])} for d in w])
    return page(request, "calendar.html", weeks=weeks, title=f"{MONTHS[first.month - 1]} {first.year}",
                prev=prev.strftime("%Y-%m"), next=nxt.strftime("%Y-%m"), today=now.strftime("%Y-%m-%d"))


@router.post("/jobs/{job_id}/reschedule")
async def reschedule(job_id: int, date: str = Form(...)):
    j = db.one("SELECT * FROM jobs WHERE id=? AND status='scheduled'", (job_id,))
    if not j:
        return Response(json.dumps({"ok": False, "error": "Faqat rejalashtirilgan yuborishni ko'chirish mumkin"}), status_code=400,
                        media_type="application/json")
    try:
        new_day = datetime.strptime(date, "%Y-%m-%d")
        old = datetime.strptime(j["scheduled_at"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return Response(json.dumps({"ok": False, "error": "Sana noto'g'ri"}), status_code=400, media_type="application/json")
    new = new_day.replace(hour=old.hour, minute=old.minute)
    if new < datetime.now():
        return Response(json.dumps({"ok": False, "error": "O'tgan vaqtga ko'chirib bo'lmaydi"}), status_code=400, media_type="application/json")
    db.ex("UPDATE jobs SET scheduled_at=? WHERE id=?", (new.strftime("%Y-%m-%d %H:%M:%S"), job_id))
    return Response(json.dumps({"ok": True}), media_type="application/json")


# ---------------- kunlik hisobot ----------------
@router.get("/reports")
async def reports_page(request: Request):
    if (r := need_account(request)):
        return r
    return page(request, "reports.html", files=dailyreport.files(acc_id(request)),
                today=datetime.now().strftime("%Y-%m-%d"))


@router.get("/reports/daily.xlsx")
async def report_daily(request: Request, date: str = ""):
    if (r := need_account(request)):
        return r
    try:
        day = datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        day = datetime.now().strftime("%Y-%m-%d")
    return Response(dailyreport.build(acc_id(request), day),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="kunlik_{day}.xlsx"'})


@router.get("/reports/file/{name}")
async def report_file(request: Request, name: str):
    if (r := need_account(request)):
        return r
    if name not in dailyreport.files(acc_id(request)):
        return go("/reports", err="Fayl topilmadi")
    return FileResponse(dailyreport.REPORT_DIR / str(acc_id(request)) / name, filename=name)
