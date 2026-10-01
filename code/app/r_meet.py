"""Majlislar bo'limi route'lari (Zoom darslari): bosh sahifa, o'z kalendar, guruhlar va jadval, darslar, o'qituvchilar,
Zoom akkauntlar puli, ogohlantirishlar, Zoom diagnostika, sozlamalar (o'qituvchilar boti), Telegram akkaunt, yo'riqnoma."""
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from markupsafe import Markup

from . import db, meet_bot as B, meet_reports as R, meet_sched as S, meet_zoom as Z, web
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
    ps = S.pool_summary()
    ctx.update(meet_unseen=db.one("SELECT COUNT(*) c FROM zoom_alerts WHERE seen=0 AND level<>'info'")["c"],
               pool=ps, problems=S.pool_problems(), bot_ok=bool(S.get("bot_token")) and not S.get("bot_err"),
               WD=S.WEEKDAYS, WD_SHORT=S.WD_SHORT)
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


def _acc_rows() -> list[dict]:
    t = S.now()
    rows = []
    for a in S.accounts():
        a["st"] = S.account_status(a, t)
        a["check"] = S.jget(f"zcheck_{a['id']}", None)
        rows.append(a)
    return rows


# ================================================================ bo'lim almashtirish
@router.post("/meet/switch")
async def meet_switch():
    resp = RedirectResponse("/meet", status_code=303)
    resp.set_cookie("ws", "meet", max_age=YEAR, samesite="lax")
    return resp


# ================================================================ bosh sahifa + o'z kalendar
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
    today_l = S.lessons_between(S.fmt(today), S.fmt(today + timedelta(days=1)))
    nxt = S.lessons_between(S.fmt(S.now()), S.fmt(S.now() + timedelta(days=3)), statuses=["planned"])[:6]
    alerts = [dict(r) for r in db.q("SELECT * FROM zoom_alerts WHERE seen=0 ORDER BY id DESC LIMIT 6")]
    return mpage(request, "meet_home.html", days=days, by_day=by_day, today=today.strftime("%Y-%m-%d"), off=off, live=live, today_l=today_l, nxt=nxt,
                 alerts=alerts, n_groups=len(S.groups(True)), n_teachers=len(S.teachers(True)), accs=_acc_rows(), acc=S.meet_account(),
                 now_hm=S.now().strftime("%H:%M"), week_label=f"{days[0]:%d.%m} – {days[-1]:%d.%m.%Y}")


@router.get("/meet/calendar")
async def meet_calendar(request: Request):
    ym = _ym(request)
    gid = _int(request.query_params.get("group"))
    a, b = R.month_bounds(ym)
    ls = S.lessons_between(a, b, gid)
    by_day = {}
    for l in ls:
        by_day.setdefault(l["day"], []).append(l)
    first = datetime.strptime(ym + "-01", "%Y-%m-%d")
    ndays = (datetime.strptime(b, "%Y-%m-%d %H:%M:%S") - first).days
    cells = [None] * first.weekday() + [(first + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(ndays)]
    cells += [None] * (-len(cells) % 7)
    return mpage(request, "meet_calendar.html", ym=ym, prev=R.shift_month(ym, -1), nxt=R.shift_month(ym, 1), cells=cells, by_day=by_day,
                 today=S.now().strftime("%Y-%m-%d"), groups=S.groups(True), gid=gid)


@router.get("/meet/calendar.ics")
async def meet_calendar_ics(request: Request):
    S.ready()
    gid = _int(request.query_params.get("group"))
    ls = S.lessons_between(S.fmt(S.now() - timedelta(days=14)), S.fmt(S.now() + timedelta(days=120)), gid)
    g = S.group(gid) if gid else None
    return Response(S.ics(ls, g["title"] if g else "Majlislar (hamma guruhlar)"), media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="majlislar.ics"'})


@router.get("/meet/api/state")
async def meet_api_state():
    S.ready()
    return JSONResponse({"pool": S.pool_summary(), "problems": S.pool_problems(),
                         "live": [{"id": l["id"], "group": l["group_title"]} for l in
                                  S.lessons_between(S.fmt(S.now() - timedelta(days=1)), S.fmt(S.now() + timedelta(days=1)), statuses=["live"])]})


# ================================================================ o'qituvchilar
@router.get("/meet/teachers")
async def teachers_page(request: Request):
    S.ready()
    ts = S.teachers()
    gs = S.groups()
    for t in ts:
        t["groups"] = [g["title"] for g in gs if g.get("teacher_id") == t["id"]]
    edit = S.teacher(_int(request.query_params.get("edit")))
    return mpage(request, "meet_teachers.html", teachers=ts, edit=edit, bot_user=S.get("bot_username", ""))


@router.post("/meet/teachers/save")
async def teachers_save(request: Request):
    f = dict(await request.form())
    tid = _int(f.get("id"))
    err = S.save_teacher(f, tid)
    if err:
        return go("/meet/teachers" + (f"?edit={tid}" if tid else ""), err=err)
    return go("/meet/teachers", msg="O'qituvchi saqlandi. U botga /start yuborib ulanadi")


@router.post("/meet/teachers/{tid}/delete")
async def teachers_delete(tid: int):
    S.delete_teacher(tid)
    return go("/meet/teachers", msg="O'qituvchi o'chirildi (guruhlardan ajratildi)")


@router.post("/meet/teachers/{tid}/test")
async def teachers_test(tid: int):
    t = S.teacher(tid)
    if not t:
        return go("/meet/teachers", err="O'qituvchi topilmadi")
    try:
        await B.send_teacher(t, "✅ Majlislar boti ishlayapti: sinov xabari.")
    except B.BotFail as e:
        return go("/meet/teachers", err=f"{t['name']}: {e}")
    return go("/meet/teachers", msg=f"{t['name']} ga sinov xabari yuborildi")


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
        g["n_sched"] = db.one("SELECT COUNT(*) c FROM zoom_schedule WHERE group_id=? AND active=1", (g["id"],))["c"]
        nx = db.one("SELECT start_at FROM zoom_lessons WHERE group_id=? AND status='planned' AND start_at>? ORDER BY start_at LIMIT 1", (g["id"], S.fmt(S.now())))
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
    past = [S.decorate(dict(r)) for r in db.q("SELECT * FROM zoom_lessons WHERE group_id=? AND start_at<? ORDER BY start_at DESC LIMIT 20", (gid, now_s))]
    return mpage(request, "meet_group.html", g=g, scheds=S.schedules(gid), up=up, past=past, teachers=S.teachers(True), seg_note=S.duration_note(60))


@router.post("/meet/groups/{gid}/save")
async def group_save(gid: int, teacher_id: str = Form(""), note: str = Form(""), active: str = Form("")):
    db.ex("UPDATE zoom_groups SET teacher_id=?, note=?, active=? WHERE id=?", (_int(teacher_id), note.strip(), 1 if active else 0, gid))
    return go(f"/meet/groups/{gid}", msg="Saqlandi")


@router.post("/meet/groups/{gid}/delete")
async def group_delete(gid: int):
    g = S.group(gid)
    await S.delete_group(gid)
    return go("/meet/groups", msg=f"«{g['title'] if g else ''}» o'chirildi")


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
    r = db.one("SELECT * FROM zoom_schedule WHERE id=?", (sid,))
    if r:
        db.ex("UPDATE zoom_schedule SET active=? WHERE id=?", (0 if r["active"] else 1, sid))
    return go(f"/meet/groups/{r['group_id']}" if r else "/meet/groups")


@router.post("/meet/schedule/{sid}/delete")
async def schedule_delete(sid: int):
    r = db.one("SELECT * FROM zoom_schedule WHERE id=?", (sid,))
    if r:
        db.ex("DELETE FROM zoom_schedule WHERE id=?", (sid,))
    return go(f"/meet/groups/{r['group_id']}" if r else "/meet/groups", msg="Jadval qatori o'chirildi (yaratilgan darslar qoladi, kerak bo'lsa bekor qiling)")


@router.post("/meet/groups/{gid}/extra")
async def extra_lesson(gid: int, day: str = Form(...), time: str = Form(...), duration: str = Form("60"), teacher_id: str = Form("")):
    try:
        start = datetime.strptime(f"{day} {time}", "%Y-%m-%d %H:%M")
        dur = int(duration)
    except ValueError:
        return go(f"/meet/groups/{gid}", err="Sana, vaqt yoki davomiylik noto'g'ri")
    S.add_extra_lesson(gid, start, dur, _int(teacher_id))
    return go(f"/meet/groups/{gid}", msg="Qo'shimcha dars yaratildi. Zoom majlisi dars boshlanishidan oldin ochiladi")


# ================================================================ darslar
@router.get("/meet/lessons")
async def lessons_page(request: Request):
    ym = _ym(request)
    gid = _int(request.query_params.get("group"))
    st = request.query_params.get("status") or None
    a, b = R.month_bounds(ym)
    return mpage(request, "meet_lessons.html", lessons=S.lessons_between(a, b, gid, [st] if st else None), ym=ym, prev=R.shift_month(ym, -1),
                 nxt=R.shift_month(ym, 1), gid=gid, st=st or "", groups=S.groups(), statuses=S.STATUS_UZ)


@router.get("/meet/lessons/{lid}")
async def lesson_page(request: Request, lid: int):
    les = S.lesson(lid)
    if not les:
        return go("/meet/lessons", err="Dars topilmadi")
    les = S.decorate(les)
    return mpage(request, "meet_lesson.html", l=les, g=S.group(les["group_id"]), teacher=S.lesson_teacher(les),
                 can_continue=bool(les["meetings"]) and les["status"] in ("planned", "live", "done"))


@router.post("/meet/lessons/{lid}/cancel")
async def lesson_cancel(lid: int, reason: str = Form(""), back: str = Form("")):
    les = S.lesson(lid)
    msg = await S.cancel_lesson(lid, reason)
    return go(_safe(back, f"/meet/groups/{les['group_id']}" if les else "/meet/lessons"), msg=msg + ". Guruh va o'qituvchiga xabar yuborildi")


@router.post("/meet/lessons/{lid}/move")
async def lesson_move(lid: int, when: str = Form(...), duration: str = Form(""), back: str = Form("")):
    try:
        ns = datetime.strptime(when, "%Y-%m-%dT%H:%M")
    except ValueError:
        return go(f"/meet/lessons/{lid}", err="Yangi sana/vaqt noto'g'ri")
    msg = await S.move_lesson(lid, ns, _int(duration))
    return go(_safe(back, f"/meet/lessons/{lid}"), msg=msg + ". Guruh va o'qituvchiga xabar yuborildi")


@router.post("/meet/lessons/{lid}/send")
async def lesson_send(lid: int, who: str = Form("group"), back: str = Form("")):
    msg = await S.resend(lid, "teacher" if who == "teacher" else "group")
    return go(_safe(back, f"/meet/lessons/{lid}"), msg=msg) if msg.startswith("Yuborildi") else go(_safe(back, f"/meet/lessons/{lid}"), err=msg)


@router.post("/meet/lessons/{lid}/provision")
async def lesson_provision(lid: int):
    ok, why = await S.provision(lid)
    return go(f"/meet/lessons/{lid}", msg="Zoom majlisi tayyor") if ok else go(f"/meet/lessons/{lid}", err=why.split(": ", 1)[-1])


@router.post("/meet/lessons/{lid}/continue")
async def lesson_continue(lid: int):
    ms = [m for m in S.meetings_of(lid)]
    if not ms:
        return go(f"/meet/lessons/{lid}", err="Avval Zoom majlisi yaratilishi kerak")
    ok, why = await S.continue_lesson(ms[-1]["id"], "admin")
    return go(f"/meet/lessons/{lid}", msg=why) if ok else go(f"/meet/lessons/{lid}", err=why.split(": ", 1)[-1])


@router.post("/meet/meetings/{mid}/end")
async def meeting_end(mid: int):
    m = S.meeting(mid)
    if not m:
        return go("/meet", err="Majlis topilmadi")
    a = S.account_raw(m["account_id"])
    if a and m.get("zoom_id"):
        await Z.end_meeting(a, m["zoom_id"])
    db.ex("UPDATE zoom_meetings SET state='ended' WHERE id=?", (mid,))
    back = f"/meet/lessons/{m['lesson_id']}" if m.get("lesson_id") else "/meet/zoom"
    return go(back, msg="Majlis tugatildi, akkaunt bo'shatildi")


# ================================================================ Zoom akkauntlar puli
@router.get("/meet/zoom")
async def zoom_page(request: Request):
    S.ready()
    m = S.meeting(_int(request.query_params.get("m")))
    if m:
        m["start_url_plain"] = S.start_url_of(m)
    edit = S.account_raw(_int(request.query_params.get("edit")))
    if edit:
        edit.pop("client_secret", None)
    return mpage(request, "meet_zoom.html", accs=_acc_rows(), new_m=m, edit=edit, default_min=S.cfg("seg_default_min"), scopes=Z.SCOPES)


@router.post("/meet/zoom/add")
async def zoom_add(label: str = Form(""), email: str = Form(""), account_id: str = Form(""), client_id: str = Form(""),
                   client_secret: str = Form(""), max_min: str = Form("")):
    aid, err = S.add_account(label, email, account_id, client_id, client_secret, max_min)
    if err:
        return go("/meet/zoom", err=err)
    return await _check_and_go(aid, deep=False, new=True)


@router.post("/meet/zoom/bulk")
async def zoom_bulk(text: str = Form("")):
    n, errs = S.bulk_add(text)
    if errs and not n:
        return go("/meet/zoom", err=" · ".join(errs[:4]))
    return go("/meet/zoom", msg=f"{n} ta akkaunt qo'shildi. «Hammasini tekshirish» ni bosing." + (f" Xatolar: {' · '.join(errs[:3])}" if errs else ""))


async def _check_and_go(aid: int, deep: bool, new=False):
    a = S.account_raw(aid)
    if not a:
        return go("/meet/zoom", err="Akkaunt topilmadi")
    items = await Z.check_account(a, deep=deep)
    S.jput(f"zcheck_{aid}", {"at": S.fmt(S.now()), "items": items, "deep": deep})
    bad = [i for i in items if i["state"] == "fail"]
    if bad:
        S.set_account_err(aid, f"{bad[0]['title']}: {bad[0]['detail']}", cool_min=2)
        return go(f"/meet/zoom#acc{aid}", err=f"{a['label']}: {bad[0]['title']} — {bad[0]['detail']}. {bad[0]['fix']}")
    S.set_account_ok(aid)
    return go(f"/meet/zoom#acc{aid}", msg=("Akkaunt qo'shildi va tekshirildi" if new else "Akkaunt ishlayapti") + f": {a['label']}")


@router.post("/meet/zoom/{aid}/test")
async def zoom_test(aid: int):
    return await _check_and_go(aid, deep=True)


@router.post("/meet/zoom/test-all")
async def zoom_test_all():
    bad = 0
    for a in S.accounts(True):
        items = await Z.check_account(S.account_raw(a["id"]), deep=True)
        S.jput(f"zcheck_{a['id']}", {"at": S.fmt(S.now()), "items": items, "deep": True})
        if any(i["state"] == "fail" for i in items):
            bad += 1
            S.set_account_err(a["id"], next(i["title"] + ": " + i["detail"] for i in items if i["state"] == "fail"), cool_min=2)
        else:
            S.set_account_ok(a["id"])
    return go("/meet/zoom", msg="Hamma akkauntlar ishlayapti") if not bad else go("/meet/zoom", err=f"{bad} ta akkauntda xato bor (har birining ostida sababi ko'rsatilgan)")


@router.post("/meet/zoom/{aid}/update")
async def zoom_update(aid: int, label: str = Form(""), email: str = Form(""), account_id: str = Form(""), client_id: str = Form(""),
                      client_secret: str = Form(""), max_min: str = Form("40")):
    err = S.update_account(aid, label, email, account_id, client_id, client_secret, max_min)
    if err:
        return go(f"/meet/zoom?edit={aid}", err=err)
    return await _check_and_go(aid, deep=False)


@router.post("/meet/zoom/{aid}/toggle")
async def zoom_toggle(aid: int):
    a = S.account_raw(aid)
    if a:
        db.ex("UPDATE zoom_accounts SET enabled=? WHERE id=?", (0 if a["enabled"] else 1, aid))
    return go("/meet/zoom")


@router.post("/meet/zoom/{aid}/delete")
async def zoom_delete(aid: int):
    err = await S.delete_account(aid)
    return go("/meet/zoom", err=err) if err else go("/meet/zoom", msg="Akkaunt o'chirildi")


@router.post("/meet/zoom/{aid}/release")
async def zoom_release(aid: int):
    n = await S.release_account(aid)
    return go("/meet/zoom", msg=f"Akkaunt bo'shatildi ({n} ta majlis tugatildi deb belgilandi)")


@router.post("/meet/zoom/{aid}/new")
async def zoom_new(aid: int, topic: str = Form("")):
    mid, err = await S.adhoc_meeting(aid, topic.strip())
    if not mid:
        return go("/meet/zoom", err=err.split(": ", 1)[-1])
    return go(f"/meet/zoom?m={mid}", msg="Yangi konferensiya yaratildi")


# ================================================================ ogohlantirishlar
@router.get("/meet/alerts")
async def alerts_page(request: Request):
    S.ready()
    return mpage(request, "meet_alerts.html", alerts=[dict(r) for r in db.q("SELECT * FROM zoom_alerts ORDER BY id DESC LIMIT 150")])


@router.post("/meet/alerts/seen")
async def alerts_seen(id: str = Form("all")):
    if id == "all":
        db.ex("UPDATE zoom_alerts SET seen=1")
    elif id.isdigit():
        db.ex("UPDATE zoom_alerts SET seen=1 WHERE id=?", (int(id),))
    return go("/meet/alerts")


# ================================================================ sozlamalar (+ o'qituvchilar boti)
@router.get("/meet/settings")
async def settings_page(request: Request):
    S.ready()
    return mpage(request, "meet_settings.html", v={k: S.cfg(k) for k in S.DEFAULTS if k != "bot_token"}, has_token=bool(S.get("bot_token")),
                 bot_user=S.get("bot_username", ""), bot_err=S.get("bot_err", ""), beat=S.get("bot_beat", ""))


_INT_RANGE = {"remind_a": (5, 1440), "remind_b": (1, 240), "group_link_min": (1, 120), "t_remind_a": (2, 240), "t_remind_b": (1, 120),
              "create_lead_min": (10, 180), "prompt_before_min": (1, 10), "min_remaining": (1, 30), "seg_default_min": (0, 1440), "horizon_days": (1, 30)}


@router.post("/meet/settings")
async def settings_save(request: Request):
    f = await request.form()
    for k, (lo, hi) in _INT_RANGE.items():
        v = _int(f.get(k))
        if v is not None:
            S.put(k, max(lo, min(hi, v)))
    S.put("remind_on", 1 if f.get("remind_on") else 0)
    S.put("admin_username", (f.get("admin_username") or "").strip().lstrip("@"))
    return go("/meet/settings", msg="Sozlamalar saqlandi")


@router.post("/meet/settings/bot")
async def settings_bot(token: str = Form("")):
    token = token.strip()
    if not token:
        return go("/meet/settings", err="Bot tokenini kiriting")
    old = S.get("bot_token")
    S.put("bot_token", token)
    try:
        me = await B.verify()
    except B.BotFail as e:
        if old:
            S.put("bot_token", old)
        else:
            S.put("bot_token", "")
        return go("/meet/settings", err=f"Bot tokeni qabul qilinmadi: {e}")
    return go("/meet/settings", msg=f"Bot ulandi: @{me.get('username')}. O'qituvchilar unga /start yuborishi kerak")


@router.post("/meet/settings/bot/remove")
async def settings_bot_remove():
    S.put("bot_token", "")
    S.drop("bot_username")
    S.drop("bot_err")
    return go("/meet/settings", msg="Bot tokeni o'chirildi")


# ================================================================ diagnostika
@router.get("/meet/diag")
async def diag_page(request: Request):
    return mpage(request, "meet_diag.html", last=S.jget("diag_last", None))


@router.post("/meet/diag/run")
async def diag_run():
    S.ready()
    res = {"at": S.fmt(S.now()), "accounts": [], "bot": None, "tg": None}
    for a in S.accounts():
        if not a["enabled"]:
            continue
        try:
            items = await Z.check_account(S.account_raw(a["id"]), deep=True)
        except Exception as e:
            log.exception("Zoom diagnostika")
            items = [{"key": "x", "title": "Tekshiruv", "state": "fail", "detail": f"{type(e).__name__}: {e}", "fix": ""}]
        res["accounts"].append({"label": a["label"], "email": a["email"], "max_min": a["max_min"], "items": items})
        if any(i["state"] == "fail" for i in items):
            S.set_account_err(a["id"], next(i["title"] + ": " + i["detail"] for i in items if i["state"] == "fail"), cool_min=2)
        else:
            S.set_account_ok(a["id"])
    if S.get("bot_token"):
        try:
            me = await B.verify()
            ts = S.teachers(True)
            res["bot"] = {"state": "ok", "detail": f"@{me.get('username')}; ulangan o'qituvchilar: {sum(1 for t in ts if t.get('chat_id'))}/{len(ts)}"}
        except B.BotFail as e:
            res["bot"] = {"state": "fail", "detail": str(e)}
    else:
        res["bot"] = {"state": "warn", "detail": "Bot tokeni kiritilmagan (Sozlamalar)"}
    acc = S.meet_account()
    if not acc:
        res["tg"] = {"state": "warn", "detail": "Majlislar Telegram akkaunti ulanmagan (guruhlarga havola yuborilmaydi)"}
    else:
        try:
            await S._client()
            res["tg"] = {"state": "ok", "detail": "Telegram akkaunt ulangan"}
        except S.TgFail as e:
            res["tg"] = {"state": "fail", "detail": str(e)}
    S.jput("diag_last", res)
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
    return mpage(request, "meet_guide.html", scopes=Z.SCOPES, bot_user=S.get("bot_username", ""))
