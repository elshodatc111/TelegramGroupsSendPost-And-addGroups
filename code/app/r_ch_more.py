"""Kanallarim: statistika, auditoriya, ogohlantirishlar, A/B, izohlar, oylik reja, hisobotlar, rasm."""
import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse

from . import ai, ch_assist, ch_data, ch_extra, ch_insight, ch_report, ch_tg, ch_track, charts, db, imagegen, notify
from .config import log
from .web import go, need_channel, page

router = APIRouter()
_tasks: set = set()


def _bg(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _safe(coro, label):
    try:
        await coro
    except Exception as e:
        log.warning("%s: %s", label, e, exc_info=True)
        notify.event("error", label, f"{type(e).__name__}: {e}")


def _ch(request):
    return request.state.channel


async def _ai_call(coro, back: str, ok_msg: str):
    try:
        res = await coro
        return res, None
    except ai.AIError as e:
        return None, go(back, err=str(e))
    except Exception as e:
        log.warning("AI so'rovi xato: %s", e, exc_info=True)
        return None, go(back, err=f"{type(e).__name__}: {e}")


# ================================================================ statistika
@router.get("/ch/stats")
async def stats_page(request: Request):
    if (r := need_channel(request)):
        return r
    ch = _ch(request)
    days = 30 if request.query_params.get("d") not in ("7", "14", "60", "90") else int(request.query_params["d"])
    d = ch_insight.daily(ch["id"], days)
    m = ch_insight.monthly(ch["id"], 12)
    labels = [x["day"][5:] for x in d]
    g_mem = charts.line([x["members"] for x in d], labels)
    g_views = charts.bars([x["views"] for x in d], labels)
    g_net = charts.bars([x["net"] or 0 for x in d], labels)
    g_clicks = charts.bars([x["clicks"] for x in d], labels) if any(x["clicks"] for x in d) else None
    off = ch_insight.official(ch["id"])
    tot_views = sum(x["views"] for x in d)
    first = next((x["members"] for x in d if x["members"] is not None), None)
    last = next((x["members"] for x in reversed(d) if x["members"] is not None), None)
    return page(request, "ch_stats.html", ch=ch, d=list(reversed(d)), m=list(reversed(m)), days=days, g_mem=g_mem, g_views=g_views,
                g_net=g_net, g_clicks=g_clicks, tot_views=tot_views, tot_posts=sum(x["posts"] for x in d),
                tot_clicks=sum(x["clicks"] for x in d), net=(last - first) if (first is not None and last is not None) else None,
                off_status=off["status"], busy=bool(db.get_setting(f"ch_busy_{ch['id']}")))


async def _refresh(ch):
    from . import ch_collect
    db.set_setting(f"ch_busy_{ch['id']}", "1")
    try:
        await ch_collect.sync_channel(ch["id"], with_competitors=False)
        ch2 = ch_data.get(ch["id"])
        await ch_insight.collect_channel(ch2, force_audience=True)
        await ch_track.pull()
        ch_insight.viral_scan(ch["id"])
    finally:
        db.del_setting(f"ch_busy_{ch['id']}")


@router.post("/ch/stats/refresh")
async def stats_refresh(request: Request, next: str = Form("/ch/stats")):
    ch = _ch(request)
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    if not next.startswith("/ch/"):
        next = "/ch/stats"
    _bg(_safe(_refresh(ch), "statistika"))
    return go(next, msg="Yangilanmoqda… 1–3 daqiqadan so'ng sahifani yangilang")


# ================================================================ auditoriya
@router.get("/ch/audience")
async def audience_page(request: Request):
    if (r := need_channel(request)):
        return r
    ch = _ch(request)
    a = ch_insight.audience_latest(ch["id"])
    off = ch_insight.official(ch["id"])
    tr = ch_insight.audience_trend(ch["id"], 30)
    blocks = []
    if a:
        blocks = [("Faol (oxirgi 1 hafta ichida Telegram'da bo'lgan)", (a["a_online"] or 0) + (a["a_week"] or 0)),
                  ("Oxirgi oyda bo'lgan", a["a_month"] or 0), ("Uzoq vaqt kirmagan", a["a_long"] or 0),
                  ("Premium", a["premium"] or 0), ("Botlar", a["bots"] or 0), ("O'chirilgan akkauntlar", a["deleted"] or 0)]
    graphs = off["graphs"]
    g = {}
    for k in ("languages_graph", "top_hours_graph", "views_by_source_graph", "new_followers_by_source_graph", "mute_graph", "growth_graph"):
        gr = graphs.get(k)
        if gr:
            g[k] = gr
    lang = None
    if g.get("languages_graph"):
        ser = next(iter(g["languages_graph"]["series"].values()), [])
        lang = sorted(zip(g["languages_graph"]["x"], ser), key=lambda x: -x[1]) if ser else None
        if lang:
            lang = [(str(n), v) for n, v in lang][:8]
    src = None
    if g.get("views_by_source_graph"):
        src = [(n, sum(v)) for n, v in g["views_by_source_graph"]["series"].items()]
    return page(request, "ch_audience.html", ch=ch, a=a, blocks=blocks, off=off, tr=tr, lang=lang, src=src,
                lang_chart=charts.hbars(lang) if lang else None,
                src_chart=charts.hbars(sorted(src, key=lambda x: -x[1])) if src else None,
                trend_chart=charts.line([x["active_pct"] for x in tr], [x["day"][5:] for x in tr]) if len(tr) > 1 else None,
                aud_err=db.get_setting(f"ch_aud_err_{ch['id']}", ""), graphs=g, hbars=charts.hbars,
                members=ch["members"] or 0)


# ================================================================ viral ogohlantirish
@router.get("/ch/alerts")
async def alerts_page(request: Request):
    if (r := need_channel(request)):
        return r
    ch = _ch(request)
    rows = ch_insight.alerts(ch["id"], 100)
    items = []
    for r_ in rows:
        d = dict(r_)
        try:
            d["an"] = json.loads(r_["analysis"]) if r_["analysis"] else None
        except Exception:
            d["an"] = None
        items.append(d)
    return page(request, "ch_alerts.html", ch=ch, items=items, x=db.get_setting("ch_viral_x", 2.5), mn=db.get_setting("ch_viral_min", 150),
                notify_on=db.get_setting("ch_notify", "1") != "0")


@router.post("/ch/alerts/seen")
async def alerts_seen(request: Request):
    ch = _ch(request)
    if ch:
        db.ex("UPDATE ch_alerts SET seen=1 WHERE channel_id=?", (ch["id"],))
    return go("/ch/alerts", msg="Belgilandi")


@router.post("/ch/alerts/scan")
async def alerts_scan(request: Request):
    ch = _ch(request)
    n = len(ch_insight.viral_scan(ch["id"])) if ch else 0
    return go("/ch/alerts", msg=f"Tekshirildi: {n} ta yangi signal")


@router.post("/ch/alerts/settings")
async def alerts_settings(request: Request, x: float = Form(2.5), mn: int = Form(150), notify_on: str = Form("")):
    db.set_setting("ch_viral_x", str(min(max(x, 1.3), 20)))
    db.set_setting("ch_viral_min", str(max(mn, 10)))
    db.set_setting("ch_notify", "1" if notify_on else "0")
    return go("/ch/alerts", msg="Saqlandi")


@router.post("/ch/alerts/test-toast")
async def alerts_toast(request: Request):
    ok = notify.toast("Sinov", "Windows bildirishnomasi ishlayapti")
    return go("/ch/alerts", msg="Bildirishnoma yuborildi" if ok else "", err=None if ok else "Bildirishnoma yuborilmadi (faqat Windows'da va yoqilgan bo'lsa ishlaydi)")


@router.post("/ch/alerts/{aid}/analyze")
async def alerts_analyze(request: Request, aid: int):
    ch = _ch(request)
    a = db.one("SELECT channel_id FROM ch_alerts WHERE id=?", (aid,))
    if not ch or not a or a["channel_id"] != ch["id"]:
        return go("/ch/alerts", err="Topilmadi")
    res, err = await _ai_call(ch_extra.analyze_alert(aid), "/ch/alerts", "")
    db.ex("UPDATE ch_alerts SET seen=1 WHERE id=?", (aid,))
    return err or go("/ch/alerts", msg="Tahlil tayyor")


# ================================================================ A/B
@router.get("/ch/ab")
async def ab_page(request: Request):
    if (r := need_channel(request)):
        return r
    ch = _ch(request)
    ideas = db.q("SELECT id, title FROM ch_ideas WHERE channel_id=? AND kind NOT IN ('comments') ORDER BY id DESC LIMIT 30", (ch["id"],))
    return page(request, "ch_ab.html", ch=ch, tests=ch_extra.ab_list(ch["id"]), ideas=ideas,
                now=(datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:00"))


@router.post("/ch/ab/new")
async def ab_new(request: Request, base: str = Form(""), idea_id: str = Form(""), note: str = Form("")):
    ch = _ch(request)
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    iid = int(idea_id) if idea_id.isdigit() else None
    res, err = await _ai_call(ch_extra.propose_ab(ch["id"], base, iid, note), "/ch/ab", "")
    return err or go("/ch/ab", msg="Variant taklif qilindi. Xohlasangiz matnlarni o'zingiz o'zgartiring")


@router.post("/ch/ab/manual")
async def ab_manual(request: Request, a: str = Form(""), b: str = Form("")):
    ch = _ch(request)
    if not ch or not a.strip() or not b.strip():
        return go("/ch/ab", err="Ikkala variant matnini kiriting")
    data = {"hypothesis": "O'zim tuzgan sinov", "a": a.strip(), "b": b.strip()}
    db.ex("INSERT INTO ch_ab(channel_id,title,hypothesis,status,result_json,created_at) VALUES(?,?,?,'draft',?,?)",
          (ch["id"], "O'z variantlarim", data["hypothesis"], json.dumps(data, ensure_ascii=False), db.now()))
    return go("/ch/ab", msg="Saqlandi")


@router.post("/ch/ab/{abid}/schedule")
async def ab_schedule(request: Request, abid: int, a: str = Form(""), b: str = Form(""), when: str = Form(""), gap: int = Form(2)):
    ch = _ch(request)
    row = db.one("SELECT * FROM ch_ab WHERE id=?", (abid,))
    if not ch or not row or row["channel_id"] != ch["id"] or row["status"] != "draft":
        return go("/ch/ab", err="Sinov topilmadi yoki allaqachon ishga tushgan")
    when = when.strip().replace("T", " ")
    if len(when) == 16:
        when += ":00"
    try:
        t = datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return go("/ch/ab", err="Yuborish vaqtini kiriting")
    if t <= datetime.now():
        return go("/ch/ab", err="Vaqt kelajakda bo'lishi kerak")
    if not a.strip() or not b.strip():
        return go("/ch/ab", err="Ikkala variant matni kerak")
    data = json.loads(row["result_json"] or "{}")
    data["a"], data["b"] = a.strip(), b.strip()
    db.ex("UPDATE ch_ab SET result_json=? WHERE id=?", (json.dumps(data, ensure_ascii=False), abid))
    ch_extra.schedule_ab(abid, a, b, when, min(max(gap, 1), 14))
    return go("/ch/plan", msg="A va B postlar rejaga qo'shildi")


@router.post("/ch/ab/{abid}/evaluate")
async def ab_eval(request: Request, abid: int):
    row = db.one("SELECT channel_id FROM ch_ab WHERE id=?", (abid,))
    if row and _ch(request) and row["channel_id"] == _ch(request)["id"]:
        ch_extra.evaluate_ab(abid)
    return go("/ch/ab", msg="Natija yangilandi")


@router.post("/ch/ab/{abid}/delete")
async def ab_delete(request: Request, abid: int):
    ch = _ch(request)
    if ch:
        db.ex("DELETE FROM ch_ab WHERE id=? AND channel_id=? AND status='draft'", (abid, ch["id"]))
    return go("/ch/ab", msg="O'chirildi")


# ================================================================ izohlar
@router.get("/ch/comments")
async def comments_page(request: Request):
    if (r := need_channel(request)):
        return r
    ch = _ch(request)
    st = ch_extra.comments_state(ch["id"])
    rep, rep_at = ch_extra.last_comments_report(ch["id"])
    n = db.one("SELECT COUNT(*) c FROM ch_comments WHERE channel_id=?", (ch["id"],))["c"]
    return page(request, "ch_comments.html", ch=ch, st=st, rep=rep, rep_at=rep_at, n=n)


@router.post("/ch/comments/collect")
async def comments_collect(request: Request):
    ch = _ch(request)
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    try:
        res = await ch_extra.collect_comments(ch["id"])
    except Exception as e:
        return go("/ch/comments", err=f"{type(e).__name__}: {e}")
    if not res["enabled"]:
        return go("/ch/comments", err="Bu kanalda izohlar yoqilmagan (muhokama guruhi ulanmagan)")
    return go("/ch/comments", msg=f"{res['n']} ta izoh olindi")


@router.post("/ch/comments/analyze")
async def comments_analyze(request: Request):
    ch = _ch(request)
    if not ch or not ch_extra.comments_state(ch["id"])["enabled"]:
        return go("/ch/comments", err="Izohlar yoqilmagan")
    res, err = await _ai_call(ch_extra.analyze_comments(ch["id"]), "/ch/comments", "")
    return err or go("/ch/comments", msg="Izohlar tahlil qilindi")


# ================================================================ oylik reja
@router.get("/ch/month")
async def month_page(request: Request):
    if (r := need_channel(request)):
        return r
    ch = _ch(request)
    plans = db.q("SELECT id, month, created_at, cost FROM ch_month WHERE channel_id=? ORDER BY id DESC LIMIT 20", (ch["id"],))
    cur = None
    mid = request.query_params.get("id", "")
    if mid.isdigit():
        row, body = ch_extra.month_get(int(mid))
        if row and row["channel_id"] == ch["id"]:
            cur = {"row": row, "body": body}
    elif plans:
        row, body = ch_extra.month_get(plans[0]["id"])
        cur = {"row": row, "body": body}
    nxt = (datetime.now().replace(day=1) + timedelta(days=32)).strftime("%Y-%m")
    busy = []
    if cur:
        mm = cur["row"]["month"]
        y_, m_ = int(mm[:4]), int(mm[5:7])
        busy = ch_assist.busy_slots(ch["id"], datetime(y_, m_, 1), datetime(y_ + (m_ == 12), (m_ % 12) + 1, 1) - timedelta(seconds=1))
    return page(request, "ch_month.html", ch=ch, plans=plans, busy=busy, cap=ch_assist.max_per_day(), mcur=cur, cur_month=datetime.now().strftime("%Y-%m"), next_month=nxt)


@router.post("/ch/month/cap")
async def month_cap(request: Request, cap: int = Form(2), back: str = Form("/ch/month")):
    db.set_setting("ch_max_per_day", str(min(max(cap, 1), 10)))
    return go(back if back.startswith("/ch/") else "/ch/month", msg="Kunlik post me'yori saqlandi")


@router.post("/ch/month/new")
async def month_new(request: Request, month: str = Form(""), note: str = Form("")):
    ch = _ch(request)
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    if len(month) != 7:
        month = datetime.now().strftime("%Y-%m")
    res, err = await _ai_call(ch_extra.generate_month(ch["id"], month, note), "/ch/month", "")
    return err or go(f"/ch/month?id={res}", msg="Oylik reja tayyor")


@router.post("/ch/month/{mid}/write")
async def month_write(request: Request, mid: int, idx: int = Form(0)):
    ch = _ch(request)
    row, _ = ch_extra.month_get(mid)
    if not ch or not row or row["channel_id"] != ch["id"]:
        return go("/ch/month", err="Topilmadi")
    res, err = await _ai_call(ch_extra.write_item(mid, idx), f"/ch/month?id={mid}", "")
    return err or go(f"/ch/month?id={mid}#i{idx}", msg="Post matni yozildi")


@router.post("/ch/month/{mid}/push")
async def month_push(request: Request, mid: int):
    ch = _ch(request)
    row, body = ch_extra.month_get(mid)
    if not ch or not row or row["channel_id"] != ch["id"]:
        return go("/ch/month", err="Topilmadi")
    form = await request.form()
    idxs = [int(v) for v in form.getlist("pick") if str(v).isdigit()]
    if not idxs:
        return go(f"/ch/month?id={mid}", err="Postlarni belgilang")
    n = ch_extra.push_to_plan(mid, idxs)
    return go("/ch/plan?s=draft", msg=f"{n} ta qoralama rejaga qo'shildi. Matn va vaqtni tekshirib «Tasdiqlash» ni bosing")


@router.post("/ch/month/{mid}/delete")
async def month_delete(request: Request, mid: int):
    ch = _ch(request)
    if ch:
        db.ex("DELETE FROM ch_month WHERE id=? AND channel_id=?", (mid, ch["id"]))
    return go("/ch/month", msg="O'chirildi")


# ================================================================ hisobotlar
@router.get("/ch/reports")
async def reports_page(request: Request):
    if (r := need_channel(request)):
        return r
    ch = _ch(request)
    rows = db.q("SELECT * FROM ch_reports WHERE channel_id=? ORDER BY id DESC LIMIT 60", (ch["id"],))
    return page(request, "ch_reports.html", ch=ch, rows=[r for r in rows if Path(r["path"]).exists()],
                weekly=db.get_setting("ch_weekly_on", "1") != "0", ai_ok=ai.configured())


@router.post("/ch/reports/make")
async def reports_make(request: Request, days: int = Form(7), with_ai: str = Form("")):
    ch = _ch(request)
    if not ch:
        return go("/ch/channels", err="Kanal tanlanmagan")
    try:
        await ch_report.make(ch["id"], min(max(days, 3), 90), bool(with_ai))
    except Exception as e:
        log.warning("Hisobot xatosi", exc_info=True)
        return go("/ch/reports", err=f"{type(e).__name__}: {e}")
    return go("/ch/reports", msg="Hisobot tayyor")


@router.post("/ch/reports/weekly")
async def reports_weekly(request: Request, on: str = Form("")):
    db.set_setting("ch_weekly_on", "1" if on else "0")
    return go("/ch/reports", msg="Saqlandi")


@router.get("/ch/reports/{rid}/download")
async def reports_dl(request: Request, rid: int):
    ch = _ch(request)
    r = db.one("SELECT * FROM ch_reports WHERE id=?", (rid,))
    if not ch or not r or r["channel_id"] != ch["id"] or not Path(r["path"]).exists():
        return go("/ch/reports", err="Fayl topilmadi")
    return FileResponse(r["path"], filename=Path(r["path"]).name)


# ================================================================ rasm
@router.get("/ch/images")
async def images_page(request: Request):
    if (r := need_channel(request)):
        return r
    ch = _ch(request)
    st = imagegen.status()
    imgs = db.q("SELECT * FROM ch_images WHERE channel_id=? ORDER BY id DESC LIMIT 60", (ch["id"],))
    plan = db.q("SELECT id, title, status, scheduled_at, image_prompt FROM ch_plan WHERE channel_id=? AND status IN ('draft','scheduled','failed','missed') "
                "ORDER BY COALESCE(scheduled_at, created_at) LIMIT 50", (ch["id"],))
    sel = None
    pid = request.query_params.get("plan", "")
    if pid.isdigit():
        sel = db.one("SELECT * FROM ch_plan WHERE id=? AND channel_id=?", (int(pid), ch["id"]))
    return page(request, "ch_images.html", ch=ch, st=st, imgs=imgs, plan=plan, sel=sel, models=imagegen.MODELS, sizes=imagegen.SIZES,
                layouts=imagegen.LAYOUTS, colors=ch_data.colors(ch), brand_ok=bool((ch["brand_name"] or "").strip() or (ch["phones"] or "").strip()),
                ai_ok=ai.configured())


@router.post("/ch/images/prompt")
async def images_prompt(request: Request, plan_id: str = Form(""), text: str = Form(""), title: str = Form("")):
    from fastapi.responses import JSONResponse
    ch = _ch(request)
    if not ch:
        return JSONResponse({"ok": False, "msg": "Kanal tanlanmagan"})
    if plan_id.isdigit():
        it = db.one("SELECT * FROM ch_plan WHERE id=? AND channel_id=?", (int(plan_id), ch["id"]))
        if it:
            text, title = it["text"], it["title"]
    if not text.strip():
        return JSONResponse({"ok": False, "msg": "Post matni bo'sh"})
    try:
        d = await imagegen.write_prompt(ch["id"], text, title)
        return JSONResponse({"ok": True, **d})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)})


@router.post("/ch/images/generate")
async def images_generate(request: Request, prompt: str = Form(""), headline: str = Form(""), subline: str = Form(""), layout: str = Form("bottom"),
                          size: str = Form("1080x1080"), engine: str = Form("local"), plan_id: str = Form("")):
    from fastapi.responses import JSONResponse
    ch = _ch(request)
    if not ch:
        return JSONResponse({"ok": False, "msg": "Kanal tanlanmagan"})
    pid = int(plan_id) if plan_id.isdigit() else None
    job = imagegen.start(ch["id"], prompt.strip(), headline.strip(), subline.strip(), layout, size, "local" if engine == "local" else "gradient", plan_id=pid)
    return JSONResponse({"ok": True, "job": job})


@router.get("/ch/images/job/{job}")
async def images_job(request: Request, job: str):
    from fastapi.responses import JSONResponse
    j = imagegen.jobs.get(job)
    ch = _ch(request)
    if not j or not ch or j.get("channel") != ch["id"]:
        return JSONResponse({"done": True, "ok": False, "msg": "Vazifa topilmadi"})
    return JSONResponse({k: j.get(k) for k in ("done", "ok", "msg", "image_id", "name")})


@router.post("/ch/images/{iid}/attach")
async def images_attach(request: Request, iid: int, plan_id: int = Form(0)):
    ch = _ch(request)
    img = db.one("SELECT * FROM ch_images WHERE id=?", (iid,))
    if not ch or not img or img["channel_id"] != ch["id"]:
        return go("/ch/images", err="Rasm topilmadi")
    if imagegen.attach_to_plan(iid, plan_id):
        return go(f"/ch/plan?edit={plan_id}", msg="Rasm postga biriktirildi")
    return go("/ch/images", err="Postga biriktirib bo'lmadi (yuborilgan bo'lishi mumkin)")


@router.post("/ch/images/{iid}/delete")
async def images_delete(request: Request, iid: int):
    from .config import MEDIA_DIR
    ch = _ch(request)
    img = db.one("SELECT * FROM ch_images WHERE id=?", (iid,))
    if ch and img and img["channel_id"] == ch["id"]:
        try:
            (MEDIA_DIR / img["name"]).unlink(missing_ok=True)
        except OSError:
            pass
        db.ex("DELETE FROM ch_images WHERE id=?", (iid,))
    return go("/ch/images", msg="O'chirildi")


@router.post("/ch/images/model")
async def images_model(request: Request, model: str = Form("")):
    if model in imagegen.MODELS:
        db.set_setting("img_model", model)
        imagegen.unload()
    return go("/ch/images", msg="Model tanlandi")


@router.post("/ch/images/install")
async def images_install(request: Request):
    p = imagegen.installer_path()
    if os.name == "nt" and p.exists():
        import subprocess
        subprocess.Popen(["cmd", "/c", "start", "", str(p)], cwd=str(p.parent))
        return go("/ch/images", msg="O'rnatish oynasi ochildi. Tugagach dasturni qayta ishga tushiring")
    return go("/ch/images", err="Avtomatik ochib bo'lmadi: code\\tools\\install_imagegen.bat faylini ikki marta bosing")


# ================================================================ Kontent reja: AI tahlil, vaqt taklifi (matnga tegmaydi)
@router.post("/ch/plan/ai-review")
async def plan_ai_review(request: Request):
    from fastapi.responses import JSONResponse
    ch = _ch(request)
    if not ch:
        return JSONResponse({"ok": False, "msg": "Kanal tanlanmagan"})
    f = await request.form()
    pid = int(f.get("plan_id") or 0) or None
    if pid and not db.one("SELECT 1 FROM ch_plan WHERE id=? AND channel_id=?", (pid, ch["id"])):
        pid = None
    try:
        data = await ch_assist.review_post(ch["id"], f.get("text") or "", f.get("title") or "", int(f.get("media_n") or 0),
                                           "html" if f.get("html") else "none", pid, f.get("when") or "")
        return JSONResponse({"ok": True, **data})
    except ai.AIError as e:
        return JSONResponse({"ok": False, "msg": str(e)})
    except Exception as e:
        log.warning("Post tahlili xato: %s", e, exc_info=True)
        return JSONResponse({"ok": False, "msg": f"{type(e).__name__}: {e}"})


@router.get("/ch/plan/times")
async def plan_times(request: Request):
    from fastapi.responses import JSONResponse
    ch = _ch(request)
    if not ch:
        return JSONResponse({"ok": False, "msg": "Kanal tanlanmagan"})
    pid = request.query_params.get("plan", "")
    return JSONResponse({"ok": True, **ch_assist.suggest_times(ch["id"], int(pid) if pid.isdigit() else None)})


@router.post("/ch/images/ai-advice")
async def images_ai_advice(request: Request, plan_id: str = Form(""), text: str = Form(""), title: str = Form("")):
    from fastapi.responses import JSONResponse
    ch = _ch(request)
    if not ch:
        return JSONResponse({"ok": False, "msg": "Kanal tanlanmagan"})
    if plan_id.isdigit():
        it = db.one("SELECT * FROM ch_plan WHERE id=? AND channel_id=?", (int(plan_id), ch["id"]))
        if it:
            text, title = it["text"], it["title"]
    try:
        data = await ch_assist.image_advice(ch["id"], text, title)
        return JSONResponse({"ok": True, **data})
    except ai.AIError as e:
        return JSONResponse({"ok": False, "msg": str(e)})
    except Exception as e:
        log.warning("Rasm maslahati xato: %s", e, exc_info=True)
        return JSONResponse({"ok": False, "msg": f"{type(e).__name__}: {e}"})
