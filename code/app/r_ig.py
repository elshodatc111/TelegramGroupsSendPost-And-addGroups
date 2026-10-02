"""Instagram bo'limi: akkauntlar, maqsad, statistika, g'oya/ssenariy, reja, nazorat, reklama tavsiyasi, sozlamalar, yo'riqnoma."""
import asyncio
import json
import secrets
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import ai, cf, charts, db, ig_fit, ig_ai, ig_api, ig_collect, ig_data, ig_diag, ig_plan, ig_stats, ig_tunnel, notify
from .config import MEDIA_DIR, TMP_DIR, log
from .web import go, page

router = APIRouter()
YEAR = 365 * 24 * 3600
_tasks: set = set()
STATUS = {"draft": "Qoralama", "scheduled": "Rejalashtirilgan", "reminded": "Eslatildi", "publishing": "Joylanmoqda",
          "published": "Joylandi", "failed": "Xato", "missed": "O'tkazib yuborildi", "cancelled": "Bekor qilindi"}
BADGE = {"draft": "waiting", "scheduled": "scheduled", "reminded": "scheduled", "publishing": "running", "published": "sent",
         "failed": "failed", "missed": "failed", "cancelled": "waiting"}


def _bg(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def cur_ig(request: Request):
    return getattr(request.state, "ig", None)


def need_ig(request: Request):
    if cur_ig(request):
        return None
    return page(request, "ig_pick.html", conf=ig_data.app_conf())


def _when(v):
    v = (v or "").strip().replace("T", " ")
    if not v:
        return None
    return v + ":00" if len(v) == 16 else v


def _ai_ok():
    return ai.configured()


async def _ai(coro, back):
    try:
        return await coro, None
    except ai.AIError as e:
        return None, go(back, err=str(e))
    except Exception as e:
        log.warning("Instagram AI xato: %s", e, exc_info=True)
        return None, go(back, err=f"{type(e).__name__}: {e}")


# ================================================================ bo'lim / akkaunt tanlash
@router.post("/ig/select")
async def ig_select(id: str = Form(""), next: str = Form("/ig")):
    nxt = next if next.startswith("/ig") and not next.startswith("//") else "/ig"
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie("igid", id if id.isdigit() else "", max_age=YEAR, samesite="lax")
    return resp


# ================================================================ bosh sahifa
@router.get("/ig")
async def ig_home(request: Request):
    accs = request.state.igs
    if not accs:
        return page(request, "ig_pick.html", conf=ig_data.app_conf())
    rows = []
    for a in accs:
        nxt = db.one("SELECT title, scheduled_at FROM ig_plan WHERE account_id=? AND status IN ('scheduled','reminded') ORDER BY scheduled_at LIMIT 1", (a["id"],))
        s = ig_stats.summary(a["id"], 30)
        rows.append({"a": a, "s": s, "gp": ig_stats.goal_progress(a), "next": nxt, "days": ig_data.token_days(a),
                     "queued": db.one("SELECT COUNT(*) c FROM ig_plan WHERE account_id=? AND status IN ('scheduled','reminded')", (a["id"],))["c"]})
    due = db.q("SELECT p.*, a.username FROM ig_plan p JOIN ig_accounts a ON a.id=p.account_id WHERE p.status='reminded' ORDER BY p.scheduled_at")
    return page(request, "ig_home.html", rows=rows, due=due, ai_ok=_ai_ok(), mode=ig_data.publish_mode())


# ================================================================ akkauntlar
@router.get("/ig/accounts")
async def ig_accounts_page(request: Request):
    return page(request, "ig_accounts.html", accs=ig_data.accounts(), conf=ig_data.app_conf(), days=ig_data.token_days,
                oauth_ok=bool(ig_data.app_conf()["app_id"] and ig_data.app_conf()["secret"]))


async def _attach(request: Request, tok: str, expires: str | None, label: str):
    try:
        prof = await ig_api.me(tok)
    except ig_api.IGError as e:
        return go("/ig/accounts", err=f"Token ishlamadi: {e}")
    if prof.get("account_type") not in ("BUSINESS", "MEDIA_CREATOR", "CREATOR"):
        return go("/ig/accounts", err=f"@{prof.get('username')} Professional akkaunt emas ({prof.get('account_type')}). Instagram ilovasida Business yoki Creator ga o'tkazing.")
    if not expires:
        try:                                        # muddatini bilish uchun yangilab ko'ramiz (24 soatdan eski bo'lsa o'tadi)
            js = await ig_api.refresh(tok)
            tok, expires = js["access_token"], ig_api.expires_at(js)
        except ig_api.IGError:
            pass
    aid = ig_data.upsert_account(prof, tok, expires)
    ig_data.log_event(aid, "connect", f"{label}: @{prof.get('username')}")
    _bg(ig_collect.sync_account(aid, demographics=True))
    resp = go("/ig/profile" if not ig_data.profile_complete(ig_data.get(aid)) else "/ig", msg=f"@{prof.get('username')} ulandi. Ma'lumotlar yig'ilmoqda (1-2 daqiqa)")
    resp.set_cookie("igid", str(aid), max_age=YEAR, samesite="lax")
    return resp


@router.post("/ig/accounts/token")
async def ig_add_token(request: Request, token: str = Form("")):
    tok = token.strip()
    if len(tok) < 30:
        return go("/ig/accounts", err="Token juda qisqa. Meta dasturidagi «Generate token» tugmasi bergan uzun matnni qo'ying.")
    conf = ig_data.app_conf()
    expires = None
    if conf["secret"]:
        try:                                         # qisqa muddatli bo'lsa uzoq muddatliga almashtiramiz
            js = await ig_api.long_lived(conf["secret"], tok)
            tok, expires = js["access_token"], ig_api.expires_at(js)
        except ig_api.IGError:
            pass
    return await _attach(request, tok, expires, "qo'lda token")


@router.get("/ig/oauth/start")
async def ig_oauth_start():
    c = ig_data.app_conf()
    if not (c["app_id"] and c["secret"]):
        return go("/ig/accounts", err="Avval App ID va App Secret ni kiriting")
    st = secrets.token_urlsafe(16)
    db.set_setting("ig_oauth_state", f"{st}|{int(time.time()) + 900}")
    return RedirectResponse(ig_api.auth_url(c["app_id"], c["redirect"], st), status_code=303)


@router.get("/ig/oauth/callback")
async def ig_oauth_cb(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if error:
        return go("/ig/accounts", err=f"Instagram rad etdi: {error_description or error}")
    saved = db.get_setting("ig_oauth_state") or ""
    db.del_setting("ig_oauth_state")
    ok = "|" in saved and saved.split("|")[0] == state and int(saved.split("|")[1]) > time.time()
    if not ok or not code:
        return go("/ig/accounts", err="Ulanish seansi eskirgan yoki noto'g'ri. Qaytadan urining.")
    c = ig_data.app_conf()
    try:
        js = await ig_api.exchange_code(c["app_id"], c["secret"], c["redirect"], code)
        ll = await ig_api.long_lived(c["secret"], js["access_token"])
    except ig_api.IGError as e:
        return go("/ig/accounts", err=f"Token olinmadi: {e}")
    return await _attach(request, ll["access_token"], ig_api.expires_at(ll), "OAuth")


@router.post("/ig/accounts/{aid}/delete")
async def ig_delete(aid: int, confirm: str = Form("")):
    a = ig_data.get(aid)
    if not a:
        return go("/ig/accounts", err="Akkaunt topilmadi")
    if confirm.strip().lstrip("@").lower() != (a["username"] or "").lower():
        return go("/ig/accounts", err="O'chirish uchun akkaunt nomini to'g'ri yozing")
    ig_data.delete_account(aid)
    resp = go("/ig/accounts", msg=f"@{a['username']} va uning barcha ma'lumotlari o'chirildi")
    resp.set_cookie("igid", "", max_age=YEAR, samesite="lax")
    return resp


@router.post("/ig/accounts/{aid}/refresh-token")
async def ig_refresh_token(aid: int):
    a = ig_data.get(aid)
    if not a:
        return go("/ig/accounts", err="Akkaunt topilmadi")
    ok = await ig_collect.refresh_token_if_needed(a, force=True)
    a = ig_data.get(aid)
    return go("/ig/accounts", msg="Token yangilandi") if ok else go("/ig/accounts", err=f"Yangilanmadi: {a['last_error'] or 'token 24 soatdan yosh yoki yaroqsiz'}")


@router.post("/ig/sync")
async def ig_sync(request: Request, next: str = Form("/ig")):
    a = cur_ig(request)
    if a:
        _bg(ig_collect.sync_account(a["id"], demographics=True))
    nxt = next if next.startswith("/ig") else "/ig"
    return go(nxt, msg="Ma'lumot yig'ish boshlandi: 1-2 daqiqadan so'ng sahifani yangilang")


# ================================================================ profil va maqsad
@router.get("/ig/profile")
async def ig_profile(request: Request):
    if (r := need_ig(request)):
        return r
    a = cur_ig(request)
    goal, rep = ig_ai.last_report(a["id"], "goal")
    return page(request, "ig_profile.html", a=a, langs=ig_data.LANGS, goal=goal, gp=ig_stats.goal_progress(a), ai_ok=_ai_ok(),
                goal_at=rep["created_at"] if rep else None)


@router.post("/ig/profile/save")
async def ig_profile_save(request: Request):
    a = cur_ig(request)
    if not a:
        return go("/ig/accounts", err="Akkaunt tanlanmagan")
    form = await request.form()
    ig_data.save_profile(a["id"], dict(form))
    return go("/ig/profile", msg="Profil saqlandi")


@router.post("/ig/goal/propose")
async def ig_goal_propose(request: Request):
    a = cur_ig(request)
    res, bad = await _ai(ig_ai.propose_goal(a), "/ig/profile")
    if bad:
        return bad
    ig_ai.save_report(a["id"], "goal", res, res.get("cost"))
    return go("/ig/profile", msg="AI maqsad taklif qildi. O'zgartirib «Maqsadni saqlash» ni bosing")


@router.post("/ig/goal/save")
async def ig_goal_save(request: Request, goal_text: str = Form(""), goal_target: int = Form(0), goal_date: str = Form("")):
    a = cur_ig(request)
    db.ex("UPDATE ig_accounts SET goal_text=?, goal_target=?, goal_date=? WHERE id=?",
          (goal_text.strip(), max(0, goal_target), goal_date.strip()[:10] or None, a["id"]))
    return go("/ig/profile", msg="Maqsad saqlandi")


# ================================================================ statistika
@router.get("/ig/stats")
async def ig_stats_page(request: Request):
    if (r := need_ig(request)):
        return r
    a = cur_ig(request)
    days = int(request.query_params["d"]) if request.query_params.get("d") in ("7", "14", "30", "60", "90") else 30
    d = ig_stats.daily(a["id"], days)
    labels = [x["day"][5:] for x in d]
    return page(request, "ig_stats.html", a=a, days=days, d=list(reversed(d)), s=ig_stats.summary(a["id"], days),
                g_fol=charts.line([x.get("followers") for x in d], labels),
                g_net=charts.bars([x.get("net") or 0 for x in d], labels),
                g_reach=charts.bars([x.get("reach") or 0 for x in d], labels),
                g_int=charts.bars([x.get("interactions") or 0 for x in d], labels),
                fm=ig_stats.by_format(a["id"], 60), m=list(reversed(ig_stats.monthly(a["id"]))), demo=ig_collect.demo(a["id"]),
                gp=ig_stats.goal_progress(a), hs=_heat(a["id"]))


def _heat(aid):
    sc = ig_stats.hour_scores(aid)
    if not sc:
        return None
    mx = max(sc.values()) or 1
    return {"max": mx, "rows": [(ig_plan.DAYS_UZ[w][:3], [round(sc.get((w, h), 0) / mx, 2) for h in range(6, 24)]) for w in range(7)]}


@router.get("/ig/posts")
async def ig_posts(request: Request):
    if (r := need_ig(request)):
        return r
    a = cur_ig(request)
    key = request.query_params.get("o", "ts")
    ms = [dict(m, er=ig_stats.er(m), fmt=ig_stats.FMT_L.get(ig_stats.fmt_of(m), "")) for m in ig_stats.media(a["id"], None, 200)]
    if key in ("reach", "views", "likes", "comments", "saved", "shares", "er"):
        ms.sort(key=lambda m: -(m[key] or 0))
    return page(request, "ig_posts.html", a=a, ms=ms, o=key)


# ================================================================ g'oya va ssenariy
@router.get("/ig/ideas")
async def ig_ideas_page(request: Request):
    if (r := need_ig(request)):
        return r
    a = cur_ig(request)
    kind = "script" if request.query_params.get("k") == "script" else "idea"
    rows = db.q("SELECT * FROM ig_ideas WHERE account_id=? AND kind=? ORDER BY id DESC LIMIT 80", (a["id"], kind))
    return page(request, "ig_ideas.html", a=a, rows=rows, kind=kind, ai_ok=_ai_ok(), langs=ig_data.LANGS, prefill=request.query_params.get("topic", ""),
                month_cost=ai.month_cost(-a["id"]))


@router.post("/ig/ideas/new")
async def ig_ideas_new(request: Request, lang: str = Form("uz"), n: int = Form(3), note: str = Form(""), fmt: str = Form("")):
    a = cur_ig(request)
    res, bad = await _ai(ig_ai.new_ideas(a, lang, n, note.strip(), fmt), "/ig/ideas")
    if bad:
        return bad
    ideas, meta = res
    for i in ideas:
        db.ex("INSERT INTO ig_ideas(account_id,created_at,kind,lang,fmt,note,title,body_json,model,cost) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (a["id"], db.now(), "idea", lang, str(i.get("format", ""))[:16], note.strip()[:500], str(i["title"])[:300],
               json.dumps(i, ensure_ascii=False), meta.get("model"), (meta.get("cost") or 0) / len(ideas)))
    return go("/ig/ideas", msg=f"{len(ideas)} ta g'oya tayyor")


@router.post("/ig/script/new")
async def ig_script_new(request: Request, topic: str = Form(""), duration: int = Form(30), lang: str = Form("uz"), style: str = Form(""),
                        idea_id: int = Form(0)):
    a = cur_ig(request)
    body = None
    if idea_id:
        row = db.one("SELECT * FROM ig_ideas WHERE id=? AND account_id=?", (idea_id, a["id"]))
        if row:
            body = json.loads(row["body_json"] or "{}")
            topic = topic or row["title"]
    if not topic.strip():
        return go("/ig/ideas?k=script", err="Mavzuni yozing")
    res, bad = await _ai(ig_ai.new_script(a, topic.strip(), max(10, min(180, duration)), lang, style.strip(), body), "/ig/ideas?k=script")
    if bad:
        return bad
    data, meta = res
    iid = db.ex("INSERT INTO ig_ideas(account_id,created_at,kind,lang,fmt,note,title,body_json,model,cost) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (a["id"], db.now(), "script", lang, "reel", style.strip()[:500], str(data.get("title") or topic)[:300],
                 json.dumps(data, ensure_ascii=False), meta.get("model"), meta.get("cost") or 0))
    return go(f"/ig/ideas/{iid}", msg="Ssenariy tayyor")


@router.get("/ig/ideas/{iid}")
async def ig_idea(request: Request, iid: int):
    a = cur_ig(request)
    row = db.one("SELECT * FROM ig_ideas WHERE id=? AND account_id=?", (iid, a["id"] if a else 0))
    if not row:
        return go("/ig/ideas", err="Topilmadi")
    return page(request, "ig_idea.html", a=a, row=row, b=json.loads(row["body_json"] or "{}"), langs=ig_data.LANGS, ai_ok=_ai_ok())


@router.post("/ig/ideas/{iid}/status")
async def ig_idea_status(request: Request, iid: int, status: str = Form("new")):
    a = cur_ig(request)
    if status in ("new", "accepted", "rejected", "used"):
        db.ex("UPDATE ig_ideas SET status=? WHERE id=? AND account_id=?", (status, iid, a["id"]))
    return go(f"/ig/ideas/{iid}")


@router.post("/ig/ideas/{iid}/to-plan")
async def ig_idea_to_plan(request: Request, iid: int, lang: str = Form("uz")):
    a = cur_ig(request)
    row = db.one("SELECT * FROM ig_ideas WHERE id=? AND account_id=?", (iid, a["id"]))
    if not row:
        return go("/ig/ideas", err="Topilmadi")
    b = json.loads(row["body_json"] or "{}")
    cap = (b.get("caption") or "") if row["kind"] == "script" else ((b.get("captions") or {}).get(lang) or next(iter((b.get("captions") or {}).values()), ""))
    tags = " ".join(b.get("hashtags") or [])
    if tags and tags not in cap:
        cap = (cap.rstrip() + "\n\n" + tags).strip()
    fmt = row["fmt"] or "photo"
    mt = {"reel": "REELS", "carousel": "CAROUSEL", "photo": "IMAGE", "story": "STORIES"}.get(fmt, "IMAGE")
    notes = (b.get("concept") or "")[:900] if row["kind"] == "idea" else "Ssenariy: " + row["title"]
    pid = db.ex("INSERT INTO ig_plan(account_id,title,caption,media_json,mtype,status,idea_id,created_at,notes) VALUES(?,?,?,?,?,?,?,?,?)",
                (a["id"], row["title"][:250], cap, "[]", mt, "draft", iid, db.now(), notes))
    db.ex("UPDATE ig_ideas SET status='accepted', plan_id=? WHERE id=?", (pid, iid))
    return go(f"/ig/plan?edit={pid}", msg="Rejaga qoralama sifatida qo'shildi: rasm/video qo'shib vaqtni belgilang")


@router.post("/ig/ideas/{iid}/delete")
async def ig_idea_delete(request: Request, iid: int):
    a = cur_ig(request)
    row = db.one("SELECT kind FROM ig_ideas WHERE id=? AND account_id=?", (iid, a["id"]))
    db.ex("DELETE FROM ig_ideas WHERE id=? AND account_id=?", (iid, a["id"]))
    return go("/ig/ideas" + ("?k=script" if row and row["kind"] == "script" else ""), msg="O'chirildi")


# ================================================================ reja
@router.get("/ig/plan")
async def ig_plan_page(request: Request):
    if (r := need_ig(request)):
        return r
    a = cur_ig(request)
    flt = request.query_params.get("s", "")
    sql, args = "SELECT * FROM ig_plan WHERE account_id=?", [a["id"]]
    if flt in STATUS:
        sql += " AND status=?"
        args.append(flt)
    items = db.q(sql + " ORDER BY COALESCE(scheduled_at, created_at) DESC LIMIT 200", args)
    counts = {r["status"]: r["c"] for r in db.q("SELECT status, COUNT(*) c FROM ig_plan WHERE account_id=? GROUP BY status", (a["id"],))}
    edit = None
    if request.query_params.get("edit", "").isdigit():
        edit = db.one("SELECT * FROM ig_plan WHERE id=? AND account_id=?", (int(request.query_params["edit"]), a["id"]))
    ready = [i for i in items if i["status"] == "reminded"]
    return page(request, "ig_plan.html", a=a, items=items, counts=counts, flt=flt, edit=edit, ai_ok=_ai_ok(), STATUS_L=STATUS, BADGE=BADGE,
                media=ig_data.media_urls, mtypes=ig_data.MTYPES, mode=ig_data.publish_mode(), ready=ready, modes=ig_data.MODE_LABELS,
                cloud_ok=cf.configured(), ffmpeg_ok=bool(ig_fit.ffmpeg()), cloud_n=db.one("SELECT COUNT(*) c FROM cf_files")["c"])


async def _apply_fit(aid, uploads: list[str], fresh_ids: list[int], fp: dict, story: bool):
    """Yangi yuklangan fayllar va bulutdan yangi tanlanganlarni Instagram formatiga moslaydi.
    Qaytaradi: (yangi mahalliy nomlar, {eski bulut id: yangi bulut id}). Xatoda ig_fit.FitError / cf.CFError."""
    import httpx
    import uuid as _uuid
    from pathlib import Path
    out, remap = [], {}
    for n in uploads:
        src = MEDIA_DIR / n
        if ig_data.is_video(n):
            if fp["fmt"] == "auto":
                out.append(n)
                continue
            dst = src.with_name(_uuid.uuid4().hex[:12] + ".mp4")
            await ig_fit.fit_video(src, dst, fp)
        else:
            fmt = fp["fmt"] if fp["fmt"] != "auto" else ig_fit.auto_target(*ig_fit.image_size(src), story=story)
            if not fmt:
                out.append(n)
                continue
            dst = ig_fit.fit_image(src, src.with_name(_uuid.uuid4().hex[:12] + ".jpg"), fp, fmt)
        src.unlink(missing_ok=True)
        out.append(f"ig/{aid}/{dst.name}")
    for fid in fresh_ids:
        row = cf.get(fid)
        if not row:
            continue
        ext = Path(row["key"]).suffix.lower()
        tmp = TMP_DIR / f"fit_{_uuid.uuid4().hex[:8]}{ext}"
        res = TMP_DIR / f"fit_{_uuid.uuid4().hex[:8]}{'.mp4' if row['kind'] == 'video' else '.jpg'}"
        try:
            async with httpx.AsyncClient(timeout=300, follow_redirects=True) as cl:
                r = await cl.get(cf.public_url(row))
            if r.status_code != 200:
                raise ig_fit.FitError(f"Bulutdagi «{row['name']}» faylini yuklab bo'lmadi (HTTP {r.status_code})")
            tmp.write_bytes(r.content)
            if row["kind"] == "video":
                if fp["fmt"] == "auto":
                    continue
                await ig_fit.fit_video(tmp, res, fp)
                label = ig_fit.LABELS[fp["fmt"]]
            else:
                fmt = fp["fmt"] if fp["fmt"] != "auto" else ig_fit.auto_target(*ig_fit.image_size(tmp), story=story)
                if not fmt:
                    continue
                ig_fit.fit_image(tmp, res, fp, fmt)
                label = ig_fit.LABELS[fmt]
            new = await cf.upload(res, f"{row['name']} · {label}", res.name)
            remap[fid] = new["id"]
        finally:
            tmp.unlink(missing_ok=True)
            res.unlink(missing_ok=True)
    return out, remap


@router.post("/ig/plan/save")
async def ig_plan_save(request: Request):
    a = cur_ig(request)
    if not a:
        return go("/ig/accounts", err="Akkaunt tanlanmagan")
    form = await request.form()
    caption = (form.get("caption") or "").strip()
    pid = int(form.get("id") or 0)
    uploads = await ig_data.save_uploads(a["id"], form.getlist("files"))
    old = db.one("SELECT * FROM ig_plan WHERE id=? AND account_id=?", (pid, a["id"])) if pid else None
    if pid and (not old or old["status"] == "published"):
        return go("/ig/plan", err="Bu postni tahrirlab bo'lmaydi")
    picks = []
    for x in (form.get("cloud_ids") or "").split(","):
        if x.strip().isdigit() and cf.get(int(x)) and f"cf:{int(x)}" not in picks:
            picks.append(f"cf:{int(x)}")
    fp = ig_fit.params(form)
    story = form.get("mtype") == "STORIES" or (not form.get("mtype") and fp["fmt"] == "9:16" and not any(ig_data.is_video(n) for n in uploads))
    fresh = [int(x) for x in (form.get("fit_cloud") or "").split(",") if x.strip().isdigit()]
    if uploads or fresh:
        try:
            uploads, remap = await _apply_fit(a["id"], uploads, fresh, fp, story)
        except (ig_fit.FitError, cf.CFError) as e:
            return go(f"/ig/plan?edit={pid}" if pid else "/ig/plan", err=str(e)[:500])
        except Exception as e:
            log.warning("Moslash xato: %s", e, exc_info=True)
            return go(f"/ig/plan?edit={pid}" if pid else "/ig/plan", err=f"Faylni moslab bo'lmadi: {type(e).__name__}: {e}"[:500])
        picks = [f"cf:{remap[int(x[3:])]}" if x[3:].isdigit() and int(x[3:]) in remap else x for x in picks]
    names = picks + uploads
    if old and not uploads and not form.get("media_touch"):
        names = ig_data.media_list(old)
    mtype = ig_data.guess_mtype(names, form.get("mtype"))
    if not form.get("mtype") and fp["fmt"] == "9:16" and mtype == "IMAGE":
        mtype = "STORIES"
    mode = form.get("mode") if form.get("mode") in ("auto", "reminder") else None
    when = _when(form.get("when"))
    back = f"/ig/plan?edit={pid}" if pid else "/ig/plan"
    if not caption and not names:
        return go(back, err="Tavsif yoki media kiriting")
    if len(caption) > 2200:
        return go(back, err=f"Tavsif {len(caption)} belgi: Instagram limiti 2200")
    if mtype == "CAROUSEL" and not 2 <= len(names) <= 10 and names:
        return go(back, err="Karusel uchun 2 dan 10 tagacha rasm/video kerak")
    eff = mode or ig_data.publish_mode()
    if when and eff == "auto" and not names:
        return go(back, err="Avto joylash uchun rasm yoki video shart")
    if when:
        try:
            if datetime.strptime(when, "%Y-%m-%d %H:%M:%S") < datetime.now() - timedelta(minutes=1):
                return go(back, err="Vaqt o'tib ketgan: kelajak vaqtni tanlang")
        except ValueError:
            return go(back, err="Vaqt formati noto'g'ri")
    status = "scheduled" if when else "draft"
    title = (form.get("title") or caption[:60] or "Post").strip()[:250]
    notes = (form.get("notes") or "").strip()
    if pid:
        db.ex("UPDATE ig_plan SET title=?,caption=?,media_json=?,mtype=?,scheduled_at=?,status=?,mode=?,error=NULL,reminded=0,notes=? WHERE id=?",
              (title, caption, json.dumps(names), mtype, when, status, mode, notes, pid))
    else:
        pid = db.ex("INSERT INTO ig_plan(account_id,title,caption,media_json,mtype,scheduled_at,status,mode,created_at,notes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (a["id"], title, caption, json.dumps(names), mtype, when, status, mode, db.now(), notes))
    cap = ig_data.max_per_day()
    msg = "Rejalashtirildi" if when else "Qoralama saqlandi"
    if when:
        day = when[:10]
        n = db.one("SELECT COUNT(*) c FROM ig_plan WHERE account_id=? AND scheduled_at LIKE ? AND status IN ('scheduled','reminded') AND id<>?",
                   (a["id"], day + "%", pid))["c"]
        if n + 1 > cap:
            msg += f". Diqqat: {day} kuniga {n + 1} ta post (kunlik me'yor {cap})"
    return go("/ig/plan", msg=msg)


@router.post("/ig/plan/{pid}/done")
async def ig_plan_done(request: Request, pid: int, permalink: str = Form("")):
    a = cur_ig(request)
    if db.one("SELECT id FROM ig_plan WHERE id=? AND account_id=?", (pid, a["id"])):
        ig_plan.mark_done(pid, permalink.strip()[:590])
        _bg(cf.after_post(pid))
        ig_data.log_event(a["id"], "post", f"reja #{pid} qo'lda joylandi deb belgilandi")
    return go("/ig/plan", msg="Joylandi deb belgilandi")


@router.post("/ig/plan/{pid}/snooze")
async def ig_plan_snooze(request: Request, pid: int, minutes: int = Form(30)):
    a = cur_ig(request)
    it = db.one("SELECT * FROM ig_plan WHERE id=? AND account_id=?", (pid, a["id"]))
    if it and it["scheduled_at"]:
        t = datetime.now() + timedelta(minutes=max(5, min(minutes, 1440)))
        db.ex("UPDATE ig_plan SET scheduled_at=?, status='scheduled', reminded=0 WHERE id=?", (t.strftime("%Y-%m-%d %H:%M:%S"), pid))
    return go("/ig/plan", msg=f"{minutes} daqiqaga surildi")


@router.post("/ig/plan/{pid}/publish-now")
async def ig_plan_now(request: Request, pid: int):
    a = cur_ig(request)
    it = db.one("SELECT * FROM ig_plan WHERE id=? AND account_id=?", (pid, a["id"]))
    if not it:
        return go("/ig/plan", err="Topilmadi")
    _bg(ig_plan.publish_item(pid))
    return go("/ig/plan", msg="Joylash boshlandi (tunnel ochiladi, 1-3 daqiqa). Natijani shu sahifada ko'ring")


@router.post("/ig/plan/{pid}/cancel")
async def ig_plan_cancel(request: Request, pid: int):
    a = cur_ig(request)
    db.ex("UPDATE ig_plan SET status='cancelled' WHERE id=? AND account_id=? AND status<>'published'", (pid, a["id"]))
    return go("/ig/plan", msg="Bekor qilindi")


@router.post("/ig/plan/{pid}/delete")
async def ig_plan_delete(request: Request, pid: int):
    a = cur_ig(request)
    db.ex("DELETE FROM ig_plan WHERE id=? AND account_id=?", (pid, a["id"]))
    return go("/ig/plan", msg="O'chirildi")


@router.post("/ig/plan/ai-review")
async def ig_plan_review(request: Request):
    a = cur_ig(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Akkaunt tanlanmagan"})
    f = await request.form()
    try:
        pid = int(f.get("plan_id") or 0) or None
        data = await ig_ai.review_caption(a, f.get("caption") or "", f.get("title") or "", f.get("mtype") or "IMAGE", int(f.get("media_n") or 0), pid, f.get("when") or "")
    except ai.AIError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    except Exception as e:
        log.warning("IG caption tahlili xato", exc_info=True)
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return JSONResponse({"ok": True, "data": data})


@router.get("/ig/plan/times")
async def ig_plan_times(request: Request):
    a = cur_ig(request)
    if not a:
        return JSONResponse({"ok": False, "error": "Akkaunt tanlanmagan"})
    pid = request.query_params.get("plan_id", "")
    return JSONResponse({"ok": True, "data": ig_plan.suggest_times(a["id"], int(pid) if pid.isdigit() else None)})


# ================================================================ nazorat / tahlil / reklama
def _report_page(request, tpl, kind, **extra):
    a = cur_ig(request)
    body, rep = ig_ai.last_report(a["id"], kind)
    return page(request, tpl, a=a, rep=body, rep_at=rep["created_at"] if rep else None, ai_ok=_ai_ok(), **extra)


@router.get("/ig/control")
async def ig_control(request: Request):
    if (r := need_ig(request)):
        return r
    a = cur_ig(request)
    now = datetime.now()
    upcoming = db.q("SELECT * FROM ig_plan WHERE account_id=? AND status IN ('scheduled','reminded') ORDER BY scheduled_at LIMIT 30", (a["id"],))
    return _report_page(request, "ig_control.html", "control", upcoming=upcoming, week=ig_plan.suggest_times(a["id"]), now=now)


@router.post("/ig/control/run")
async def ig_control_run(request: Request):
    a = cur_ig(request)
    res, bad = await _ai(ig_ai.control_plan(a), "/ig/control")
    if bad:
        return bad
    ig_ai.save_report(a["id"], "control", res, res.get("cost"))
    return go("/ig/control", msg="Reja tekshirildi")


@router.post("/ig/control/idea-to-plan")
async def ig_control_to_plan(request: Request, title: str = Form(""), fmt: str = Form("photo"), when: str = Form("")):
    a = cur_ig(request)
    mt = {"reel": "REELS", "carousel": "CAROUSEL", "photo": "IMAGE", "story": "STORIES"}.get(fmt, "IMAGE")
    w = _when(when[:16].replace(" ", "T") if when else "")
    try:
        if w:
            datetime.strptime(w, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        w = None
    pid = db.ex("INSERT INTO ig_plan(account_id,title,caption,media_json,mtype,status,created_at,scheduled_at,notes) VALUES(?,?,?,?,?,?,?,?,?)",
                (a["id"], title[:250] or "Post", "", "[]", mt, "draft", db.now(), None, ("Tavsiya etilgan vaqt: " + w[:16]) if w else ""))
    return go(f"/ig/plan?edit={pid}", msg="Qoralama yaratildi. Tavsif va media qo'shing; yaxshisi «G'oya» sahifasidan to'liq g'oya oling")


@router.get("/ig/analysis")
async def ig_analysis(request: Request):
    if (r := need_ig(request)):
        return r
    return _report_page(request, "ig_analysis.html", "analysis", gp=ig_stats.goal_progress(cur_ig(request)))


@router.post("/ig/analysis/run")
async def ig_analysis_run(request: Request):
    a = cur_ig(request)
    res, bad = await _ai(ig_ai.analyse(a), "/ig/analysis")
    if bad:
        return bad
    ig_ai.save_report(a["id"], "analysis", res, res.get("cost"))
    return go("/ig/analysis", msg="Tahlil tayyor")


@router.get("/ig/boost")
async def ig_boost(request: Request):
    if (r := need_ig(request)):
        return r
    a = cur_ig(request)
    return _report_page(request, "ig_boost.html", "boost", cands=ig_ai.boost_candidates(a["id"]))


@router.post("/ig/boost/run")
async def ig_boost_run(request: Request):
    a = cur_ig(request)
    res, bad = await _ai(ig_ai.boost_advice(a), "/ig/boost")
    if bad:
        return bad
    ig_ai.save_report(a["id"], "boost", res, res.get("cost"))
    return go("/ig/boost", msg="Tavsiya tayyor")


# ================================================================ AI sarfi
@router.get("/ig/usage")
async def ig_usage(request: Request):
    if (r := need_ig(request)):
        return r
    a = cur_ig(request)
    rows = db.q("SELECT * FROM ch_usage WHERE channel_id=? ORDER BY id DESC LIMIT 100", (-a["id"],))
    by = db.q("SELECT purpose, COUNT(*) n, SUM(tokens_in) ti, SUM(tokens_out) tout, SUM(cost) c FROM ch_usage WHERE channel_id=? GROUP BY purpose", (-a["id"],))
    return page(request, "ig_usage.html", a=a, rows=rows, by=by, month=ai.month_cost(-a["id"]), cap=float(db.get_setting("ai_cap_usd", 0) or 0))


# ================================================================ sozlamalar va diagnostika
@router.get("/ig/settings")
async def ig_settings(request: Request):
    c = ig_data.app_conf()
    return page(request, "ig_settings.html", conf=c, has_secret=bool(c["secret"]), mode=ig_data.publish_mode(), remind=ig_data.remind_min(),
                cap=ig_data.max_per_day(), sync_h=db.get_setting("ig_sync_hours", 3), exe=ig_tunnel.find_exe(), exe_path=db.get_setting("ig_cloudflared", "") or "",
                grace=db.get_setting("ig_plan_grace_h", 6), ai_ok=_ai_ok())


@router.post("/ig/settings/app")
async def ig_set_app(app_id: str = Form(""), app_secret: str = Form(""), redirect: str = Form("")):
    db.set_setting("ig_app_id", app_id.strip())
    if app_secret.strip():
        db.set_setting("ig_app_secret", app_secret.strip())
    db.set_setting("ig_redirect", redirect.strip() or "http://localhost:8000/ig/oauth/callback")
    return go("/ig/settings", msg="Meta ilova sozlamalari saqlandi")


@router.post("/ig/settings/general")
async def ig_set_general(mode: str = Form("reminder"), remind: int = Form(10), cap: int = Form(1), sync_h: int = Form(3), grace: int = Form(6),
                         exe_path: str = Form("")):
    db.set_setting("ig_publish_mode", "auto" if mode == "auto" else "reminder")
    db.set_setting("ig_remind_min", max(0, min(remind, 240)))
    db.set_setting("ig_max_per_day", max(1, min(cap, 10)))
    db.set_setting("ig_sync_hours", max(1, min(sync_h, 48)))
    db.set_setting("ig_plan_grace_h", max(1, min(grace, 48)))
    db.set_setting("ig_cloudflared", exe_path.strip())
    return go("/ig/settings", msg="Saqlandi")


@router.post("/ig/settings/cloudflared")
async def ig_dl_cloudflared():
    try:
        p = await ig_tunnel.download_exe()
    except Exception as e:
        return go("/ig/settings", err=f"Yuklanmadi: {e}")
    return go("/ig/settings", msg=f"cloudflared yuklandi: {p}")


@router.get("/ig/diag")
async def ig_diag_page(request: Request):
    if (r := need_ig(request)):
        return r
    return page(request, "ig_diag.html", a=cur_ig(request), items=None)


@router.post("/ig/diag/run")
async def ig_diag_run(request: Request, tunnel: str = Form("")):
    if (r := need_ig(request)):
        return r
    a = cur_ig(request)
    items = await ig_diag.run(a, test_tunnel=bool(tunnel))
    return page(request, "ig_diag.html", a=a, items=items)


@router.get("/ig/guide")
async def ig_guide(request: Request):
    return page(request, "ig_guide.html", conf=ig_data.app_conf())


# ================================================================ ochiq fayl (faqat avto joylash paytida tunnel orqali)
@router.get("/ig-pub/{token}/{name}")
async def ig_public_file(token: str, name: str):
    p = ig_tunnel.resolve(token, name)
    if not p:
        return Response(status_code=404)
    return FileResponse(p)
