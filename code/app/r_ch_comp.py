"""Telegram SMM: raqobatchilar, sinxron, tahlil va o'z postlari."""
import asyncio
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from . import ai, ch_ai, ch_collect, ch_data, ch_stats, ch_tg, db, transcribe
from .config import log
from .web import go, need_channel, page

router = APIRouter()
_tasks: set = set()


def _bg(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# ---------------------------------------------------------------- sinxron
@router.post("/ch/sync")
async def ch_sync(request: Request, next: str = Form("/ch/analysis")):
    ch = request.state.channel
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    if (ch_collect.progress.get(ch["id"]) or {}).get("running"):
        return go(next, msg="Sinxron allaqachon ketmoqda")
    _bg(ch_collect.sync_channel(ch["id"]))
    return go(next, msg="Yangilash boshlandi (o'z kanal va raqobatchilar). Bir necha daqiqa olishi mumkin")


@router.get("/ch/sync/status")
async def ch_sync_status(request: Request):
    ch = request.state.channel
    p = ch_collect.progress.get(ch["id"]) if ch else None
    return JSONResponse(p or {"running": False})


# ---------------------------------------------------------------- raqobatchilar
@router.get("/ch/competitors")
async def ch_competitors(request: Request):
    if (r := need_channel(request)):
        return r
    ch = request.state.channel
    comps = ch_data.competitors(ch["id"], active_only=False)
    rows = []
    for c in comps:
        s = ch_stats.source_stats(ch["id"], c["id"], 30)
        rows.append({"c": c, "s": s, "g7": ch_stats.growth_delta(ch["id"], c["id"], 7)})
    found, scan_err, scanned = [], None, False
    if request.query_params.get("scan"):
        scanned = True
        try:
            _, member = await ch_tg.dialogs(ch["account_id"])
            have = {c["tg_id"] for c in comps}
            found = [m for m in member if m["tg_id"] not in have]
        except Exception as e:
            scan_err = str(e)
            log.warning("Raqobatchi ro'yxati olinmadi", exc_info=True)
    return page(request, "ch_competitors.html", ch=ch, rows=rows, found=found, scanned=scanned, scan_err=scan_err,
                prog=ch_collect.progress.get(ch["id"]))


@router.post("/ch/competitors/add")
async def ch_comp_add(request: Request):
    ch = request.state.channel
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    form = await request.form()
    wanted = {int(x) for x in form.getlist("tg_ids") if x.lstrip("-").isdigit()}
    if not wanted:
        return go("/ch/competitors?scan=1", err="Kamida bitta kanalni belgilang")
    try:
        _, member = await ch_tg.dialogs(ch["account_id"])
    except Exception as e:
        return go("/ch/competitors", err=str(e))
    n = 0
    for m in member:
        if m["tg_id"] in wanted:
            db.ex("INSERT OR IGNORE INTO ch_competitors(channel_id,tg_id,title,username,members,is_private,active,added_at) "
                  "VALUES(?,?,?,?,?,?,1,?)", (ch["id"], m["tg_id"], m["title"], m["username"], m["members"], int(m["private"]), db.now()))
            db.ex("UPDATE ch_competitors SET active=1 WHERE channel_id=? AND tg_id=?", (ch["id"], m["tg_id"]))
            n += 1
    if n:
        _bg(ch_collect.sync_channel(ch["id"]))
    return go("/ch/competitors", msg=f"{n} ta raqobatchi biriktirildi. Ma'lumotlar yig'ilmoqda")


@router.post("/ch/competitors/{comp_id}/remove")
async def ch_comp_remove(request: Request, comp_id: int):
    ch = request.state.channel
    c = db.one("SELECT * FROM ch_competitors WHERE id=?", (comp_id,))
    if ch and c and c["channel_id"] == ch["id"]:
        db.ex("DELETE FROM ch_posts WHERE channel_id=? AND comp_id=?", (ch["id"], comp_id))
        db.ex("DELETE FROM ch_members_log WHERE channel_id=? AND comp_id=?", (ch["id"], comp_id))
        db.ex("DELETE FROM ch_competitors WHERE id=?", (comp_id,))
    return go("/ch/competitors", msg="Raqobatchi olib tashlandi (akkaunt kanaldan chiqmaydi)")


@router.post("/ch/competitors/{comp_id}/toggle")
async def ch_comp_toggle(request: Request, comp_id: int):
    ch = request.state.channel
    c = db.one("SELECT * FROM ch_competitors WHERE id=?", (comp_id,))
    if ch and c and c["channel_id"] == ch["id"]:
        db.ex("UPDATE ch_competitors SET active=? WHERE id=?", (0 if c["active"] else 1, comp_id))
    return go("/ch/competitors")


@router.post("/ch/competitors/{comp_id}/notes")
async def ch_comp_notes(request: Request, comp_id: int, notes: str = Form("")):
    ch = request.state.channel
    c = db.one("SELECT * FROM ch_competitors WHERE id=?", (comp_id,))
    if ch and c and c["channel_id"] == ch["id"]:
        db.ex("UPDATE ch_competitors SET notes=? WHERE id=?", (notes.strip()[:2000], comp_id))
    return go("/ch/competitors", msg="Izoh saqlandi")


# ---------------------------------------------------------------- tahlil
@router.get("/ch/analysis")
async def ch_analysis(request: Request):
    if (r := need_channel(request)):
        return r
    ch = request.state.channel
    cid = ch["id"]
    days = 30 if request.query_params.get("days") not in ("7", "14", "60") else int(request.query_params["days"])
    comps = ch_data.competitors(cid)
    sel = request.query_params.get("comp", "0")
    comp_id = int(sel) if sel.isdigit() and (sel == "0" or any(str(c["id"]) == sel for c in comps)) else 0
    mem = ch_data.ensure_memory(cid)
    rep = db.one("SELECT * FROM ch_ideas WHERE channel_id=? AND kind='report' ORDER BY id DESC LIMIT 1", (cid,))
    series = {c["id"]: [m for _, m in ch_stats.growth(cid, c["id"], 60)] for c in comps}
    series[0] = [m for _, m in ch_stats.growth(cid, 0, 60)]
    return page(request, "ch_analysis.html", ch=ch, days=days, table=ch_stats.compare(cid, days), comps=comps, comp_id=comp_id,
                heat=ch_stats.heatmap(cid, comp_id), top=ch_stats.top_posts(cid, comp_id, days, 8), mem=mem,
                report=(json.loads(rep["body_json"]) if rep else None), report_at=(rep["created_at"] if rep else None),
                series=series, prog=ch_collect.progress.get(cid), ai_ok=ai.configured(), MEDIA=ch_stats.MEDIA_LABELS,
                stt=transcribe.mode())


@router.post("/ch/analysis/report")
async def ch_report(request: Request):
    ch = request.state.channel
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    try:
        await ch_ai.analyze_report(ch["id"])
    except ai.AIError as e:
        return go("/ch/analysis", err=str(e))
    return go("/ch/analysis#report", msg="AI tahlil hisoboti tayyor")


@router.post("/ch/analysis/learn")
async def ch_learn(request: Request):
    ch = request.state.channel
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    try:
        ok = await ch_ai.learn(ch["id"], force=True)
    except ai.AIError as e:
        return go("/ch/analysis", err=str(e))
    return go("/ch/analysis#memo", msg="Bilim xotirasi yangilandi" if ok else "Yangilash uchun raqobatchi ma'lumoti yo'q. Avval sinxronlang")


@router.post("/ch/analysis/video-format")
async def ch_video_format(request: Request):
    ch = request.state.channel
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    try:
        await ch_ai.learn_video_format(ch["id"])
    except (ai.AIError, RuntimeError) as e:
        return go("/ch/analysis", err=str(e))
    return go("/ch/analysis#memo", msg="Video format o'rganildi")


@router.post("/ch/analysis/comp-videos/{comp_id}")
async def ch_comp_videos(request: Request, comp_id: int):
    """Raqobatchining eng ko'p ko'rilgan 3 ta videosini matnga aylantiradi (keyingi g'oyalar shularga tayanadi)."""
    ch = request.state.channel
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    vids = db.q("SELECT * FROM ch_posts WHERE channel_id=? AND comp_id=? AND media='video' AND duration<=420 ORDER BY views DESC LIMIT 3",
                (ch["id"], comp_id))
    if not vids:
        return go(f"/ch/analysis?comp={comp_id}", err="Bu raqobatchida (qisqa) video topilmadi")
    done, errs = 0, []
    for v in vids:
        try:
            await transcribe.transcribe_post(ch, v)
            done += 1
        except Exception as e:
            errs.append(str(e))
    if not done:
        return go(f"/ch/analysis?comp={comp_id}", err="Transkripsiya bo'lmadi: " + (errs[0] if errs else ""))
    return go(f"/ch/analysis?comp={comp_id}", msg=f"{done} ta video matnga aylantirildi")


# ---------------------------------------------------------------- o'z postlari
@router.get("/ch/posts")
async def ch_posts(request: Request):
    if (r := need_channel(request)):
        return r
    ch = request.state.channel
    sort = request.query_params.get("sort", "date")
    rows = ch_stats.posts(ch["id"], 0, 60)
    mem = ch["members"] or 1
    rows = [dict(r, err=round((r["views"] or 0) / mem * 100, 1)) for r in rows]
    if sort == "views":
        rows.sort(key=lambda r: -(r["views"] or 0))
    elif sort == "err":
        rows.sort(key=lambda r: -r["err"])
    return page(request, "ch_posts.html", ch=ch, rows=rows[:150], sort=sort, MEDIA=ch_stats.MEDIA_LABELS,
                stats=ch_stats.source_stats(ch["id"], 0, 30), stt=transcribe.mode())


@router.post("/ch/posts/{pid}/transcribe")
async def ch_post_transcribe(request: Request, pid: int):
    ch = request.state.channel
    p = db.one("SELECT * FROM ch_posts WHERE id=?", (pid,))
    if not ch or not p or p["channel_id"] != ch["id"]:
        return go("/ch/posts", err="Post topilmadi")
    try:
        await transcribe.transcribe_post(ch, p)
    except Exception as e:
        return go("/ch/posts", err=str(e))
    return go("/ch/posts", msg="Video matnga aylantirildi")
