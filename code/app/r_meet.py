"""Majlislar bo'limi route'lari (Google Meet darslari): bosh sahifa, guruhlar, jadval, darslar, davomat, hisobotlar,
o'qituvchilar, ogohlantirishlar, Google diagnostika, sozlamalar, akkaunt, yo'riqnoma."""
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from markupsafe import Markup

from . import db, meet_google as G, meet_reports as R, meet_sched as S, meet_track as T, web
from .config import log
from .core import manager
from .web import go, page

router = APIRouter()
YEAR = 60 * 60 * 24 * 365

# yangi ikonlar (web.ICONS ga qo'shiladi: base.html shu lug'atdan <symbol> yaratadi)
web.ICONS.update({k: Markup(v) for k, v in {
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>',
    "cap": '<path d="M22 10L12 5 2 10l10 5 10-5z"/><path d="M6 12v5c3 2 9 2 12 0v-5"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "book": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
}.items()})


def _safe(next_: str, default="/meet") -> str:
    return next_ if next_.startswith("/") and not next_.startswith("//") else default


def mpage(request: Request, name: str, **ctx):
    S.ready()
    ctx.update(meet_unseen=db.one("SELECT COUNT(*) c FROM mt_alerts WHERE seen=0 AND level<>'info'")["c"],
               g_ok=G.connected(), host_email=S.get("g_email", ""), WD=S.WEEKDAYS, WD_SHORT=S.WD_SHORT)
    return page(request, name, **ctx)


def _ym(request: Request) -> str:
    m = request.query_params.get("m", "")
    try:
        datetime.strptime(m, "%Y-%m")
        return m
    except ValueError:
        return S.now().strftime("%Y-%m")


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ================================================================ bo'lim almashtirish
@router.post("/meet/switch")
async def meet_switch():
    resp = RedirectResponse("/meet", status_code=303)
    resp.set_cookie("ws", "meet", max_age=YEAR, samesite="lax")
    return resp


# ================================================================ bosh sahifa + umumiy kalendar
def _week_days(offset: int):
    today = S.now().replace(hour=0, minute=0, second=0, microsecond=0)
    mon = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    return [mon + timedelta(days=i) for i in range(7)], today


@router.get("/meet")
async def meet_home(request: Request):
    S.ready()
    off = _int(request.query_params.get("w"), 0)
    days, today = _week_days(off)
    ls = S.lessons_between(S.fmt(days[0]), S.fmt(days[-1] + timedelta(days=1)))
    by_day = {d.strftime("%Y-%m-%d"): [] for d in days}
    for l in ls:
        by_day.setdefault(l["day"], []).append(l)
    live = S.lessons_between(S.fmt(today - timedelta(days=1)), S.fmt(today + timedelta(days=2)), statuses=["live"])
    today_l = by_day.get(today.strftime("%Y-%m-%d")) if off == 0 else S.lessons_between(S.fmt(today), S.fmt(today + timedelta(days=1)))
    nxt = [l for l in S.lessons_between(S.fmt(S.now()), S.fmt(S.now() + timedelta(days=3)), statuses=["planned"])][:6]
    alerts = [dict(r) for r in db.q("SELECT * FROM mt_alerts WHERE seen=0 ORDER BY id DESC LIMIT 6")]
    cals = [g for g in S.groups(True) if g.get("calendar_id")]
    embed = "&".join("src=" + g["calendar_id"] for g in cals)
    return mpage(request, "meet_home.html", days=days, by_day=by_day, today=today.strftime("%Y-%m-%d"), off=off, live=live,
                 today_l=today_l, nxt=nxt, alerts=alerts, embed=embed, n_groups=len(S.groups(True)), n_teachers=len(S.teachers(True)),
                 acc=S.meet_account(), now_hm=S.now().strftime("%H:%M"), week_label=f"{days[0]:%d.%m} – {days[-1]:%d.%m.%Y}")


@router.get("/meet/calendar")
async def meet_calendar(request: Request):
    ym = _ym(request)
    a, b = R.month_bounds(ym)
    ls = S.lessons_between(a, b)
    by_day = {}
    for l in ls:
        by_day.setdefault(l["day"], []).append(l)
    first = datetime.strptime(ym + "-01", "%Y-%m-%d")
    pad = first.weekday()
    ndays = (datetime.strptime(b, "%Y-%m-%d %H:%M:%S") - first).days
    cells = [None] * pad + [(first + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(ndays)]
    cells += [None] * (-len(cells) % 7)
    cals = [g for g in S.groups(True) if g.get("calendar_id")]
    return mpage(request, "meet_calendar.html", ym=ym, prev=R.shift_month(ym, -1), nxt=R.shift_month(ym, 1), cells=cells, by_day=by_day,
                 today=S.now().strftime("%Y-%m-%d"), groups=S.groups(True), embed="&".join("src=" + g["calendar_id"] for g in cals))


@router.get("/meet/api/live")
async def meet_api_live():
    S.ready()
    ls = S.lessons_between(S.fmt(S.now() - timedelta(days=1)), S.fmt(S.now() + timedelta(days=1)), statuses=["live"])
    return JSONResponse([{"id": l["id"], "group": l["group_title"], "elapsed": l["elapsed"], "real_start": l["real_start"], "peak": l["peak"]} for l in ls])


# ================================================================ o'qituvchilar
@router.get("/meet/teachers")
async def teachers_page(request: Request):
    S.ready()
    ts = S.teachers()
    for t in ts:
        t["groups"] = [g["title"] for g in S.groups() if g.get("teacher_id") == t["id"]]
    edit = S.teacher(_int(request.query_params.get("edit")))
    return mpage(request, "meet_teachers.html", teachers=ts, edit=edit)


@router.post("/meet/teachers/save")
async def teachers_save(request: Request):
    f = dict(await request.form())
    tid = _int(f.get("id"))
    err = S.save_teacher(f, tid)
    if err:
        return go("/meet/teachers" + (f"?edit={tid}" if tid else ""), err=err)
    return go("/meet/teachers", msg="O'qituvchi saqlandi")


@router.post("/meet/teachers/{tid}/delete")
async def teachers_delete(tid: int):
    S.delete_teacher(tid)
    return go("/meet/teachers", msg="O'qituvchi o'chirildi (guruhlardan ajratildi)")


# ================================================================ guruhlar
@router.get("/meet/groups")
async def groups_page(request: Request):
    S.ready()
    found, scan_err, scanned = [], None, False
    if request.query_params.get("scan"):
        scanned = True
        try:
            have = {g["tg_id"] for g in S.groups()}
            found = [x for x in await S.tg_groups() if x["tg_id"] not in have]
        except Exception as e:
            scan_err = str(e)
    gs = S.groups()
    for g in gs:
        g["n_sched"] = db.one("SELECT COUNT(*) c FROM mt_schedule WHERE group_id=? AND active=1", (g["id"],))["c"]
        nx = db.one("SELECT start_at FROM mt_lessons WHERE group_id=? AND status='planned' AND start_at>? ORDER BY start_at LIMIT 1", (g["id"], S.fmt(S.now())))
        g["next"] = nx["start_at"][:16] if nx else ""
    return mpage(request, "meet_groups.html", groups=gs, found=found, scanned=scanned, scan_err=scan_err, teachers=S.teachers(True), acc=S.meet_account())


@router.post("/meet/groups/attach")
async def groups_attach(request: Request):
    form = await request.form()
    wanted = {int(x) for x in form.getlist("tg_ids") if x.lstrip("-").isdigit()}
    if not wanted:
        return go("/meet/groups?scan=1", err="Kamida bitta guruhni belgilang")
    try:
        allg = await S.tg_groups()
    except Exception as e:
        return go("/meet/groups", err=str(e))
    n = 0
    for g in allg:
        if g["tg_id"] in wanted:
            S.add_group(g["tg_id"], g["title"], g["username"])
            n += 1
    return go("/meet/groups", msg=f"{n} ta guruh qo'shildi. Endi o'qituvchi va jadvalni belgilang")


@router.post("/meet/groups/add")
async def groups_add(ref: str = Form(...)):
    try:
        g = await S.tg_resolve(ref)
    except Exception as e:
        return go("/meet/groups", err=f"Guruh topilmadi: {e}")
    gid = S.add_group(g["tg_id"], g["title"], g["username"])
    return go(f"/meet/groups/{gid}", msg=f"«{g['title']}» qo'shildi")


@router.get("/meet/groups/{gid}")
async def group_detail(request: Request, gid: int):
    g = S.group(gid)
    if not g:
        return go("/meet/groups", err="Guruh topilmadi")
    now_s = S.fmt(S.now())
    up = S.lessons_between(now_s, S.fmt(S.now() + timedelta(days=60)), gid)
    past = [S.decorate(dict(r)) for r in db.q("SELECT * FROM mt_lessons WHERE group_id=? AND start_at<? ORDER BY start_at DESC LIMIT 25", (gid, now_s))]
    scheds = S.schedules(gid)
    return mpage(request, "meet_group.html", g=g, scheds=scheds, up=up, past=past, teachers=S.teachers(True),
                 roster=T.roster(gid), aliases=T.aliases(gid), gmail_limit=S.cfg("host_type") == "gmail")


@router.post("/meet/groups/{gid}/save")
async def group_save(gid: int, teacher_id: str = Form(""), note: str = Form(""), active: str = Form("")):
    db.ex("UPDATE mt_groups SET teacher_id=?, note=?, active=? WHERE id=?", (_int(teacher_id), note.strip(), 1 if active else 0, gid))
    return go(f"/meet/groups/{gid}", msg="Saqlandi")


@router.post("/meet/groups/{gid}/delete")
async def group_delete(gid: int):
    g = S.group(gid)
    S.delete_group(gid)
    return go("/meet/groups", msg=f"«{g['title'] if g else ''}» o'chirildi (Google Calendar'dagi kalendarga tegilmadi)")


@router.post("/meet/groups/{gid}/calendar")
async def group_calendar(gid: int):
    g = S.group(gid)
    try:
        await S.ensure_calendar(g)
    except G.GoogleError as e:
        return go(f"/meet/groups/{gid}", err=f"Kalendar yaratilmadi: {e.short()}")
    return go(f"/meet/groups/{gid}", msg="Guruh kalendari tayyor")


@router.post("/meet/groups/{gid}/generate")
async def group_generate(gid: int):
    n = await S.generate(gid)
    return go(f"/meet/groups/{gid}", msg=f"{n} ta yangi dars yaratildi")


@router.post("/meet/groups/{gid}/schedule")
async def schedule_add(request: Request, gid: int):
    f = await request.form()
    errs, warns = S.add_schedule(gid, f.getlist("weekday"), f.get("start_time", ""), f.get("duration", ""), _int(f.get("teacher_id")))
    if errs:
        return go(f"/meet/groups/{gid}", err=" · ".join(errs))
    n = await S.generate(gid)
    return go(f"/meet/groups/{gid}", msg=f"Jadval saqlandi, {n} ta dars yaratildi." + ("  Diqqat: " + " ".join(warns) if warns else ""))


@router.post("/meet/schedule/{sid}/toggle")
async def schedule_toggle(sid: int):
    r = db.one("SELECT * FROM mt_schedule WHERE id=?", (sid,))
    if r:
        db.ex("UPDATE mt_schedule SET active=? WHERE id=?", (0 if r["active"] else 1, sid))
    return go(f"/meet/groups/{r['group_id']}" if r else "/meet/groups")


@router.post("/meet/schedule/{sid}/delete")
async def schedule_delete(sid: int):
    r = db.one("SELECT * FROM mt_schedule WHERE id=?", (sid,))
    if r:
        db.ex("DELETE FROM mt_schedule WHERE id=?", (sid,))
    return go(f"/meet/groups/{r['group_id']}" if r else "/meet/groups", msg="Jadval qatori o'chirildi (yaratilgan darslar qoladi, kerak bo'lsa bekor qiling)")


@router.post("/meet/groups/{gid}/extra")
async def extra_lesson(gid: int, day: str = Form(...), time: str = Form(...), duration: str = Form("60"), teacher_id: str = Form("")):
    try:
        start = datetime.strptime(f"{day} {time}", "%Y-%m-%d %H:%M")
        dur = int(duration)
    except ValueError:
        return go(f"/meet/groups/{gid}", err="Sana, vaqt yoki davomiylik noto'g'ri")
    lid = S.add_extra_lesson(gid, start, dur, _int(teacher_id))
    ok, why = await S.provision(lid)
    warn = S.duration_warning(dur)
    return go(f"/meet/groups/{gid}", msg="Qo'shimcha dars yaratildi" + ("" if ok else f" (Meet havolasi keyin: {why})") + (f". Diqqat: {warn}" if warn else ""))


@router.post("/meet/groups/{gid}/roster")
async def roster_edit(gid: int, name: str = Form(""), remove: str = Form("")):
    if remove:
        T.remove_roster(gid, remove)
    elif name.strip():
        T.add_roster(gid, name)
    return go(f"/meet/groups/{gid}#roster")


@router.post("/meet/groups/{gid}/merge")
async def group_merge(gid: int, from_name: str = Form(...), to_name: str = Form(...), back: str = Form("")):
    n = T.merge_names(gid, from_name, to_name)
    return go(_safe(back, f"/meet/groups/{gid}"), msg=f"«{from_name}» → «{to_name}» birlashtirildi ({n} ta yozuv)")


@router.post("/meet/groups/{gid}/unmerge")
async def group_unmerge(gid: int, raw_name: str = Form(...), back: str = Form("")):
    T.unmerge(gid, raw_name)
    return go(_safe(back, f"/meet/groups/{gid}"), msg="Birlashtirish bekor qilindi")


# ================================================================ darslar
@router.get("/meet/lessons")
async def lessons_page(request: Request):
    ym = _ym(request)
    gid = _int(request.query_params.get("group"))
    st = request.query_params.get("status") or None
    a, b = R.month_bounds(ym)
    ls = S.lessons_between(a, b, gid, [st] if st else None)
    return mpage(request, "meet_lessons.html", lessons=ls, ym=ym, prev=R.shift_month(ym, -1), nxt=R.shift_month(ym, 1), gid=gid, st=st or "",
                 groups=S.groups(), statuses=S.STATUS_UZ)


@router.get("/meet/lessons/{lid}")
async def lesson_page(request: Request, lid: int):
    les = S.lesson(lid)
    if not les:
        return go("/meet/lessons", err="Dars topilmadi")
    les = S.decorate(les)
    g = S.group(les["group_id"])
    people = T.person_rows(lid)
    sm = T.lesson_summary(lid)
    atts = [dict(r) for r in db.q("SELECT * FROM mt_attendance WHERE lesson_id=? ORDER BY first_in", (lid,))]
    return mpage(request, "meet_lesson.html", l=les, g=g, people=people, sm=sm, atts=atts, report=T.report_text(lid) if les["status"] in ("done", "live") else "",
                 teacher=S.lesson_teacher(les), student_names=[p["person"] for p in people if not p["is_teacher"]] + sm["absent"],
                 late_min=S.cfg("late_min"), early_min=S.cfg("early_min"), aliases=T.aliases(g["id"]))


@router.post("/meet/lessons/{lid}/cancel")
async def lesson_cancel(lid: int, reason: str = Form(""), back: str = Form("")):
    les = S.lesson(lid)
    msg = await S.cancel_lesson(lid, reason)
    return go(_safe(back, f"/meet/groups/{les['group_id']}" if les else "/meet/lessons"), msg=msg + ". Guruh va o'qituvchiga xabar yuborildi")


@router.post("/meet/lessons/{lid}/move")
async def lesson_move(lid: int, when: str = Form(...), duration: str = Form(""), back: str = Form("")):
    les = S.lesson(lid)
    try:
        ns = datetime.strptime(when, "%Y-%m-%dT%H:%M")
    except ValueError:
        return go(f"/meet/lessons/{lid}", err="Yangi sana/vaqt noto'g'ri")
    msg = await S.move_lesson(lid, ns, _int(duration))
    return go(_safe(back, f"/meet/lessons/{lid}"), msg=msg + ". Guruh va o'qituvchiga xabar yuborildi")


@router.post("/meet/lessons/{lid}/record")
async def lesson_record(lid: int, url: str = Form("")):
    db.ex("UPDATE mt_lessons SET record_url=? WHERE id=?", (url.strip(), lid))
    return go(f"/meet/lessons/{lid}", msg="Yozuv havolasi saqlandi")


@router.post("/meet/lessons/{lid}/send")
async def lesson_send(lid: int, who: str = Form("group"), back: str = Form("")):
    msg = await S.send_now(lid, "teacher" if who == "teacher" else "group")
    return go(_safe(back, f"/meet/lessons/{lid}"), msg=msg) if msg.startswith(("Yuborildi",)) else go(_safe(back, f"/meet/lessons/{lid}"), err=msg)


@router.post("/meet/lessons/{lid}/provision")
async def lesson_provision(lid: int):
    ok, why = await S.provision(lid)
    return go(f"/meet/lessons/{lid}", msg="Meet xonasi va kalendar tayyor") if ok else go(f"/meet/lessons/{lid}", err=why)


@router.post("/meet/lessons/{lid}/poll")
async def lesson_poll(lid: int):
    les = S.lesson(lid)
    if not les or not les.get("space_name"):
        return go(f"/meet/lessons/{lid}", err="Meet xonasi hali yo'q")
    try:
        res = await T.poll_lesson(les)
    except Exception as e:
        return go(f"/meet/lessons/{lid}", err=f"Meet API: {e.short() if isinstance(e, G.GoogleError) else e}")
    return go(f"/meet/lessons/{lid}", msg=("Yangilandi: " + f"{res['n']} ishtirokchi, hozir {res['in_now']} kishi") if res["conf"] else "Meet'da hali hech kim kirmagan")


@router.post("/meet/lessons/{lid}/finish")
async def lesson_finish(lid: int):
    les = S.lesson(lid)
    if les and les["status"] in ("planned", "live"):
        await T.finalize(les, missed=not db.one("SELECT 1 FROM mt_attendance WHERE lesson_id=?", (lid,)))
    return go(f"/meet/lessons/{lid}", msg="Dars yakunlandi, hisobot tayyorlandi")


@router.post("/meet/attendance/{aid}/teacher")
async def att_teacher(aid: int, flag: str = Form("1")):
    a = db.one("SELECT lesson_id FROM mt_attendance WHERE id=?", (aid,))
    T.mark_teacher(aid, flag == "1")
    return go(f"/meet/lessons/{a['lesson_id']}" if a else "/meet/lessons", msg="Belgilandi")


# ================================================================ hisobotlar
@router.get("/meet/reports")
async def reports_page(request: Request):
    ym = _ym(request)
    gs = S.groups()
    gid = _int(request.query_params.get("group")) or (gs[0]["id"] if gs else None)
    grid = R.month_grid(gid, ym) if gid else None
    return mpage(request, "meet_reports.html", groups=gs, gid=gid, ym=ym, prev=R.shift_month(ym, -1), nxt=R.shift_month(ym, 1), grid=grid,
                 g=S.group(gid) if gid else None)


@router.get("/meet/reports/export")
async def reports_export(request: Request):
    ym = _ym(request)
    gid = _int(request.query_params.get("group"))
    g = S.group(gid)
    if not g:
        return go("/meet/reports", err="Guruh tanlanmagan")
    data = R.export_xlsx(gid, ym)
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in g["title"])[:40].strip() or "guruh"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="davomat_{safe}_{ym}.xlsx"'.encode("ascii", "ignore").decode()})


@router.get("/meet/stats")
async def stats_page(request: Request):
    ym = _ym(request)
    return mpage(request, "meet_stats.html", st=R.stats(ym), ym=ym, prev=R.shift_month(ym, -1), nxt=R.shift_month(ym, 1),
                 live=S.lessons_between(S.fmt(S.now() - timedelta(days=1)), S.fmt(S.now() + timedelta(days=1)), statuses=["live"]))


# ================================================================ ogohlantirishlar
@router.get("/meet/alerts")
async def alerts_page(request: Request):
    S.ready()
    rows = [dict(r) for r in db.q("SELECT * FROM mt_alerts ORDER BY id DESC LIMIT 150")]
    return mpage(request, "meet_alerts.html", alerts=rows)


@router.post("/meet/alerts/seen")
async def alerts_seen(id: str = Form("all")):
    if id == "all":
        db.ex("UPDATE mt_alerts SET seen=1")
    elif id.isdigit():
        db.ex("UPDATE mt_alerts SET seen=1 WHERE id=?", (int(id),))
    return go("/meet/alerts")


# ================================================================ Google: sozlama, ulanish, diagnostika
@router.get("/meet/settings")
async def settings_page(request: Request):
    S.ready()
    vals = {k: S.cfg(k) for k in S.DEFAULTS}
    return mpage(request, "meet_settings.html", v=vals, client_id=S.get("google_client_id", ""), has_secret=bool(S.get("google_client_secret")),
                 g_state=S.get("g_state", "none"), g_email=S.get("g_email", ""), g_connected_at=S.get("g_connected_at", ""),
                 redirect_uri=G.redirect_uri(), configured=G.configured())


_INT_RANGE = {"late_min": (0, 180), "early_min": (0, 180), "remind_a": (5, 1440), "remind_b": (1, 240), "teacher_dm_min": (1, 180),
              "group_link_min": (1, 120), "teacher_late_min": (1, 60), "poll_sec": (30, 60), "horizon_days": (1, 30), "max_people": (0, 500)}


@router.post("/meet/settings")
async def settings_save(request: Request):
    f = await request.form()
    for k, (lo, hi) in _INT_RANGE.items():
        v = _int(f.get(k))
        if v is not None:
            S.put(k, max(lo, min(hi, v)))
    for k in ("remind_on", "report_on", "report_to_group"):
        S.put(k, 1 if f.get(k) else 0)
    S.put("host_type", "workspace" if f.get("host_type") == "workspace" else "gmail")
    S.put("moderation", "OFF" if f.get("moderation") == "OFF" else "ON")
    S.put("admin_username", (f.get("admin_username") or "").strip().lstrip("@"))
    return go("/meet/settings", msg="Sozlamalar saqlandi")


@router.post("/meet/settings/google")
async def settings_google(client_id: str = Form(""), client_secret: str = Form("")):
    if client_id.strip():
        S.put("google_client_id", client_id.strip())
    if client_secret.strip():
        S.put("google_client_secret", client_secret.strip())
    return go("/meet/settings", msg="OAuth kalit saqlandi (secret shifrlangan). Endi «Google'ga ulash» ni bosing")


@router.get("/meet/google/connect")
async def google_connect():
    try:
        return RedirectResponse(G.start_auth(), status_code=303)
    except G.GoogleError as e:
        return go("/meet/settings", err=e.short())


@router.get("/meet/google/callback")
async def google_callback(request: Request):
    q = request.query_params
    if q.get("error"):
        return go("/meet/settings", err=f"Google ulanishni rad etdi: {q.get('error')}")
    try:
        email = await G.finish_auth(q.get("code", ""), q.get("state", ""))
    except G.GoogleError as e:
        return go("/meet/settings", err=e.short())
    except Exception as e:
        log.exception("Google callback")
        return go("/meet/settings", err=f"{type(e).__name__}: {e}")
    return go("/meet/diag", msg=f"Google ulandi: {email}. Endi diagnostikani ishga tushiring")


@router.post("/meet/google/disconnect")
async def google_disconnect():
    G.disconnect()
    return go("/meet/settings", msg="Google uzildi. Boshqa akkauntni ulash uchun «Google'ga ulash» ni bosing")


@router.get("/meet/diag")
async def diag_page(request: Request):
    last = S.jget("diag_last", None)
    return mpage(request, "meet_diag.html", last=last, g_state=S.get("g_state", "none"), configured=G.configured(), host_type=S.cfg("host_type"),
                 test_email=S.get("diag_email", ""), teachers=[t for t in S.teachers(True) if t.get("gmail")])


@router.post("/meet/diag/run")
async def diag_run(test_email: str = Form("")):
    S.ready()
    S.put("diag_email", test_email.strip())
    try:
        items = await G.diagnose(test_email)
    except Exception as e:
        log.exception("Google diagnostika")
        items = [{"key": "x", "title": "Diagnostika", "state": "fail", "detail": f"{type(e).__name__}: {e}", "fix": "", "ms": 0}]
    S.jput("diag_last", {"at": S.fmt(S.now()), "items": items, "host": S.get("g_email", ""), "host_type": S.cfg("host_type")})
    return go("/meet/diag")


# ================================================================ Telegram akkaunt (Majlislar)
def _acc(aid: int):
    return db.one("SELECT * FROM accounts WHERE id=? AND workspace='meet'", (aid,))


@router.get("/meet/account")
async def account_page(request: Request):
    acc = S.meet_account()
    info = None
    if acc:
        svc = manager.get(acc["id"])
        info = svc.info
        if not info:
            try:
                await svc.status()
                info = svc.info
            except Exception:
                pass
    return mpage(request, "meet_account.html", acc=acc, info=info, login_id=_int(request.query_params.get("login"), 0),
                 step=request.query_params.get("step", ""), api_ok=bool(db.get_setting("api_id") and db.get_setting("api_hash")),
                 n_groups=len(S.groups()))


@router.post("/meet/account/add")
async def account_add(request: Request, name: str = Form(""), phone: str = Form(...)):
    if S.meet_account():
        return go("/meet/account", err="Majlislar uchun faqat bitta akkaunt ulanadi")
    if not (db.get_setting("api_id") and db.get_setting("api_hash")):
        return go("/meet/account", err="Avval Group Post → Sozlamalar da Telegram API ID va API HASH ni kiriting")
    digits = "".join(c for c in phone if c.isdigit())
    clash = db.one("SELECT id FROM accounts WHERE workspace<>'meet' AND REPLACE(REPLACE(phone,'+',''),' ','')=?", (digits,))
    if clash:
        return go("/meet/account", err="Bu raqam boshqa bo'limda ishlatilmoqda. Majlislar uchun alohida akkaunt kerak.")
    aid = manager.create(name.strip() or "Majlislar akkaunti", request.state.user["id"], "meet")
    try:
        await manager.get(aid).send_code(phone.strip())
    except Exception as e:
        await manager.delete(aid)
        return go("/meet/account", err=f"Kod yuborilmadi: {e}")
    return go(f"/meet/account?login={aid}&step=code", msg="Telegram'ga kelgan kodni kiriting")


async def _done(aid):
    await manager.get(aid).status()
    return go("/meet/groups?scan=1", msg="Akkaunt ulandi. Endi o'quvchilar guruhlarini tanlang")


@router.post("/meet/account/{aid}/code")
async def account_code(aid: int, code: str = Form(...)):
    if not _acc(aid):
        return go("/meet/account", err="Akkaunt topilmadi")
    try:
        res = await manager.get(aid).sign_in_code(code.strip().replace(" ", ""))
    except Exception as e:
        return go(f"/meet/account?login={aid}&step=code", err=f"Kod noto'g'ri yoki eskirgan: {e}")
    if res == "password":
        return go(f"/meet/account?login={aid}&step=password", msg="Akkauntda 2 bosqichli parol bor, uni kiriting")
    return await _done(aid)


@router.post("/meet/account/{aid}/password")
async def account_password(aid: int, password: str = Form(...)):
    if not _acc(aid):
        return go("/meet/account", err="Akkaunt topilmadi")
    try:
        await manager.get(aid).sign_in_password(password)
    except Exception as e:
        return go(f"/meet/account?login={aid}&step=password", err=f"Parol noto'g'ri: {e}")
    return await _done(aid)


@router.post("/meet/account/{aid}/reconnect")
async def account_reconnect(aid: int, phone: str = Form(...)):
    if not _acc(aid):
        return go("/meet/account", err="Akkaunt topilmadi")
    try:
        await manager.get(aid).send_code(phone.strip())
    except Exception as e:
        return go("/meet/account", err=f"Kod yuborilmadi: {e}")
    return go(f"/meet/account?login={aid}&step=code", msg="Telegram'ga kelgan kodni kiriting")


@router.post("/meet/account/{aid}/logout")
async def account_logout(aid: int):
    if not _acc(aid):
        return go("/meet/account", err="Akkaunt topilmadi")
    try:
        await manager.get(aid).logout()
    except Exception as e:
        return go("/meet/account", err=str(e))
    return go("/meet/account", msg="Akkauntdan chiqildi (qayta ulash mumkin)")


@router.post("/meet/account/{aid}/delete")
async def account_delete(aid: int):
    if not _acc(aid):
        return go("/meet/account", err="Akkaunt topilmadi")
    await manager.delete(aid)
    return go("/meet/account", msg="Akkaunt o'chirildi (guruhlar, jadval va davomat saqlandi)")


@router.post("/meet/account/test")
async def account_test():
    try:
        await S.tg_send("me", "✅ Majlislar: Telegram akkaunt ishlayapti.")
    except S.TgFail as e:
        return go("/meet/account", err=f"Yuborilmadi: {e}")
    return go("/meet/account", msg="Sinov xabari «Saqlangan xabarlar»ga yuborildi")


# ================================================================ yo'riqnoma
@router.get("/meet/guide")
async def guide_page(request: Request):
    return mpage(request, "meet_guide.html", redirect_uri=G.redirect_uri(), scopes=G.SCOPES)
