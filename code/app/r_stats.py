"""Statistika, guruhlar reytingi va hisobotlar eksporti."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import Response

from . import analytics, db, jobops
from .reports import export_pdf, export_xlsx, stats_data
from .web import acc_id, go, need_account, page

router = APIRouter()


def _days(v) -> int:
    try:
        return max(1, min(365, int(v)))
    except (TypeError, ValueError):
        return 30


@router.get("/stats")
async def stats_page(request: Request, days: int = 30):
    aid = acc_id(request)
    if not aid:
        return page(request, "stats.html", d=None, days=30)
    return page(request, "stats.html", d=stats_data(aid, _days(days)), days=_days(days), bt=analytics.best_time(aid))


@router.post("/stats/refresh")
async def stats_refresh(request: Request):
    if (r := need_account(request)):
        return r
    aid = acc_id(request)
    jobs = db.q("SELECT id FROM jobs WHERE account_id=? AND status IN ('done','running') "
                "AND started_at>=datetime('now','localtime','-14 day') ORDER BY id DESC LIMIT 30", (aid,))

    async def run():
        for j in jobs:
            await jobops.refresh_stats(j["id"])
    asyncio.create_task(run())
    return go("/stats", msg=f"So'nggi {len(jobs)} ta yuborish statistikasi yangilanmoqda (bir necha daqiqa ketadi)")


def _acc_name(aid):
    a = db.one("SELECT name FROM accounts WHERE id=?", (aid,))
    return a["name"] if a else "-"


@router.get("/stats/export.xlsx")
async def export_x(request: Request, days: int = 30):
    aid = acc_id(request)
    data = export_xlsx(stats_data(aid, _days(days)), _acc_name(aid))
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="reklama_hisobot.xlsx"'})


@router.get("/stats/export.pdf")
async def export_p(request: Request, days: int = 30):
    aid = acc_id(request)
    data = export_pdf(stats_data(aid, _days(days)), _acc_name(aid))
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="reklama_hisobot.pdf"'})
