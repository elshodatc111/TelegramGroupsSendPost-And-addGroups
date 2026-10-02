"""Image Generator: fon ishchisi (navbat), loyiha qadamlari va ZIP. Faqat matn: rasm/video yaratilmaydi.

Qadamlar (qayta ishga tushirilganda tugallanganini o'tkazib yuboradi):
  analyze  - ssenariyni sahnalarga, odamlar va joylarga ajratadi (1 ta so'rov)
  pipeline - odam/joy tavsifi (1 ta so'rov) -> sahnalar guruh-guruh: rasm + video + audio promptlar (guruhga 1 ta so'rov)
  regen    - bitta sahnani qayta yozish (o'zbekcha izoh bo'yicha yoki boshqa variant)
"""
import asyncio
import io
import zipfile
from datetime import datetime, timedelta

from . import ai, db, img_ai, img_db as D, notify
from .config import log


class Stopped(Exception):
    pass


# ---------------------------------------------------------------- navbat
def claim_next():
    r = db.one("SELECT * FROM img_jobs WHERE status='queued' ORDER BY id LIMIT 1")
    if not r:
        return None
    db.ex("UPDATE img_jobs SET status='running', started_at=? WHERE id=? AND status='queued'", (D.now(), r["id"]))
    return dict(r)


def finish_job(jid, status, error=""):
    db.ex("UPDATE img_jobs SET status=?, error=?, finished_at=? WHERE id=?", (status, error[:1500], D.now(), jid))


async def loop():
    """Fon sikli: navbatdagi ishlarni birin-ketin bajaradi (syscheck.supervise ostida)."""
    D.ready()
    await asyncio.sleep(3)
    while True:
        try:
            job = claim_next()
            if job:
                await run_job(job)
            else:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("img_work sikli")
            await asyncio.sleep(5)


async def run_job(job: dict):
    kind, pid, sid = job["kind"], job["project_id"], job["scene_id"]
    payload = D.jl(job["payload"], {})
    try:
        if kind == "analyze":
            await job_analyze(pid)
        elif kind == "pipeline":
            await job_pipeline(pid)
        elif kind == "regen":
            await job_regen(sid, payload)
        else:
            raise RuntimeError(f"Noma'lum ish turi: {kind}")
        finish_job(job["id"], "done")
    except Stopped:
        finish_job(job["id"], "cancelled", "To'xtatildi")
    except Exception as e:
        log.warning("Image Generator ishi bajarilmadi (%s #%s): %s", kind, job["id"], e, exc_info=not isinstance(e, ai.AIError))
        msg = str(e) if isinstance(e, ai.AIError) else f"{type(e).__name__}: {e}"
        finish_job(job["id"], "error", msg)
        if pid and kind in ("analyze", "pipeline"):
            D.set_project(pid, status="error", error=msg[:900], msg="Xato")
            notify.event("error", "imagegen", f"Loyiha #{pid}: {msg[:300]}")
        elif sid:
            D.set_scene(sid, status="error", error=msg[:900])


def check_stop(pid):
    p = D.project(pid) if pid else None
    if not p or p["status"] == "stopped":
        raise Stopped()


# ---------------------------------------------------------------- tahlil
async def job_analyze(pid):
    p = D.project(pid)
    if not p:
        raise Stopped()
    b = D.brand(p["brand_id"])
    D.set_project(pid, status="analyzing", msg="Ssenariy o'rganilmoqda", progress=0, error=None)
    ppl, locs = D.people(p["brand_id"]), D.locations(p["brand_id"])
    res = await img_ai.analyze(b, p["script"], p["fmt"], ppl, locs, ctx={"project_id": pid})
    check_stop(pid)
    # eski natijalarni almashtiramiz (faqat boshlanmagan loyihada tahlil qayta ishlanadi)
    for s in D.rows("SELECT id FROM img_scenes WHERE project_id=?", (pid,)):
        db.ex("DELETE FROM img_prompts WHERE scene_id=?", (s["id"],))
        db.ex("DELETE FROM img_versions WHERE scene_id=?", (s["id"],))
    for t in ("img_scenes", "img_roles", "img_plocs"):
        db.ex(f"DELETE FROM {t} WHERE project_id=?", (pid,))
    for r in res["roles"]:
        db.ex("INSERT INTO img_roles(project_id,rkey,title,description,person_id,slots) VALUES(?,?,?,?,?,?)",
              (pid, r["key"], r["title"], r["description"], r["person_id"], "[]"))
    lib_by_name = {lo["name"].strip().lower(): lo["id"] for lo in locs}
    for lo in res["locations"]:
        lid = lo["library_id"] or lib_by_name.get(lo["name"].strip().lower())
        db.ex("INSERT INTO img_plocs(project_id,lkey,name,description,location_id) VALUES(?,?,?,?,?)", (pid, lo["key"], lo["name"], lo["description"], lid))
    for s in res["scenes"]:
        db.ex("INSERT INTO img_scenes(project_id,idx,t_from,t_to,duration,label,kind,visual,voice,onscreen,roles,lkey,status,updated_at) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
              (pid, s["idx"], s["t_from"], s["t_to"], s["duration"], s["label"], s["kind"], s["visual"], s["voice"], s["onscreen"], D.jd(s["roles"]), s["location"], D.now()))
    title = p["title"] if p["title"] and p["title"] != "Nomsiz loyiha" else (res["title"] or "Nomsiz loyiha")
    D.set_project(pid, status="needs_assets", title=title, progress=0, msg="Odamlarni tanlang")


# ---------------------------------------------------------------- yordamchilar
def _inputs(pid):
    p = D.project(pid)
    img, vids = D.project_platforms(p)
    return p, D.brand(p["brand_id"]), D.roles(pid), D.plocs(pid), img, vids


def _save_scene(sc: dict, res: dict, note="", model=""):
    vid = D.add_version(sc["id"], res["image"], note=note, model=model)
    D.set_prompts(sc["id"], vid, res["video"], res["audio"])
    D.set_scene(sc["id"], active_ver=vid, status="done", error=None, warn=None)
    return vid


async def ensure_sheets(pid, roles, plocs, brand):
    """Odam va joylarning qisqa ingliz tavsifi (bir marta yoziladi, keyin hamma sahnada bir xil ishlatiladi)."""
    need_r = [r for r in roles if not (r.get("identity") or "").strip()]
    need_p = [x for x in plocs if not (x.get("place_en") or "").strip()]
    if not need_r and not need_p:
        return
    people, places = await img_ai.character_sheets(brand, need_r, need_p, {"project_id": pid})
    for r in need_r:
        txt = people.get(r["rkey"]) or (r.get("person") or {}).get("appearance") or r["description"] or r["title"]
        db.ex("UPDATE img_roles SET identity=? WHERE id=?", (txt, r["id"]))
    for x in need_p:
        txt = places.get(x["lkey"]) or x["description"] or x["name"]
        db.ex("UPDATE img_plocs SET place_en=? WHERE id=?", (txt, x["id"]))


def _prog(pid, done, total, msg):
    D.set_project(pid, progress=min(99, int(done * 100 / max(total, 1))), msg=msg)


# ---------------------------------------------------------------- loyiha jarayoni
async def job_pipeline(pid):
    p = D.project(pid)
    if not p:
        raise Stopped()
    D.set_project(pid, status="running", error=None, msg="Boshlandi", started_at=p["started_at"] or D.now())
    for pl in D.plocs(pid):                                # kutubxonada yo'q joylar brendga qo'shiladi
        if not pl["location_id"]:
            lid, _err = D.save_location(p["brand_id"], pl["name"], pl["description"] or pl["name"])
            if lid:
                db.ex("UPDATE img_plocs SET location_id=? WHERE id=?", (lid, pl["id"]))
    p, brand, roles, plocs, img, vids = _inputs(pid)
    scs = D.scenes(pid, full=False)
    todo = [s for s in scs if s["status"] != "done"]
    total = len(scs)
    done = total - len(todo)
    _prog(pid, done, total + 1, "Odam va joylar tavsifi yozilmoqda")
    check_stop(pid)
    await ensure_sheets(pid, roles, plocs, brand)
    p, brand, roles, plocs, img, vids = _inputs(pid)
    size = max(1, min(int(D.num("batch", 5)), 10))
    by_idx = {s["idx"]: s for s in scs}
    for i in range(0, len(todo), size):
        chunk = todo[i:i + size]
        check_stop(pid)
        for s in chunk:
            D.set_scene(s["id"], status="writing", error=None)
        _prog(pid, done, total + 1, f"{chunk[0]['idx']}-{chunk[-1]['idx']}-sahna promptlari yozilmoqda ({total} tadan)")
        prev = by_idx.get(chunk[0]["idx"] - 1)
        got = {}
        try:
            got = await img_ai.write_prompts(brand, p, chunk, roles, plocs, img, vids, prev_visual=prev["visual"] if prev else "", ctx={"project_id": pid})
        except ai.AIError as e:
            if img_ai.is_fatal(e):
                for s in chunk:
                    D.set_scene(s["id"], status="error", error=str(e)[:900])
                raise
            log.warning("Promptlar paketi yozilmadi: %s", e)
        for s in chunk:
            res = got.get(s["idx"])
            if not res:                                    # to'liq kelmagan sahna alohida qayta uriniladi
                check_stop(pid)
                try:
                    one = await img_ai.write_prompts(brand, p, [s], roles, plocs, img, vids, prev_visual=(by_idx.get(s["idx"] - 1) or {}).get("visual", ""), ctx={"project_id": pid})
                    res = one.get(s["idx"])
                except ai.AIError as e:
                    if img_ai.is_fatal(e):
                        D.set_scene(s["id"], status="error", error=str(e)[:900])
                        raise
                    D.set_scene(s["id"], status="error", error=str(e)[:900])
                    continue
            if res:
                _save_scene(s, res)
                done += 1
            else:
                D.set_scene(s["id"], status="error", error="AI bu sahna uchun to'liq prompt yozmadi")
        D.set_project(pid, progress=min(99, int(done * 100 / (total + 1))))
    finish_project(pid)


def finish_project(pid):
    scs = D.scenes(pid, full=False)
    bad = [s for s in scs if s["status"] != "done"]
    p = D.project(pid)
    if bad:
        D.set_project(pid, status="partial", progress=100, msg=f"{len(bad)} ta sahna yozilmadi: qayta urinib ko'ring", finished_at=D.now())
        notify.toast("Image Generator", f"«{p['title']}»: {len(scs) - len(bad)}/{len(scs)} sahna tayyor, {len(bad)} tasida xato")
    else:
        D.set_project(pid, status="done", progress=100, msg="Hammasi tayyor", finished_at=D.now())
        notify.toast("Image Generator", f"«{p['title']}» promptlari tayyor: {len(scs)} ta sahna")
    notify.event("info", "imagegen", f"Loyiha «{p['title']}»: {len(scs) - len(bad)}/{len(scs)} sahna tayyor, xarajat ${p['cost']:.4f}")


async def job_regen(sid, payload):
    sc = D.scene(sid)
    if not sc:
        raise Stopped()
    pid = sc["project_id"]
    D.set_project(pid, msg=f"{sc['idx']}-sahna qayta yozilmoqda")
    try:
        p, brand, roles, plocs, img, vids = _inputs(pid)
        await ensure_sheets(pid, roles, plocs, brand)
        p, brand, roles, plocs, img, vids = _inputs(pid)
        note = (payload.get("note") or "").strip()
        old = {"image": sc["active"]["image_prompt"]} if sc.get("active") else None
        prev_sc = next((x for x in D.scenes(pid, full=False) if x["idx"] == sc["idx"] - 1), None)
        got = await img_ai.write_prompts(brand, p, [sc], roles, plocs, img, vids, prev_visual=prev_sc["visual"] if prev_sc else "",
                                         old=old, note=note, variant=not note, ctx={"project_id": pid, "scene_id": sid})
        res = got.get(sc["idx"])
        if not res:
            raise ai.AIError("AI to'liq prompt yozib bera olmadi, qayta urinib ko'ring")
        _save_scene(sc, res, note=note or "boshqa variant")
    finally:
        if D.scene(sid, full=False)["status"] == "writing":
            D.set_scene(sid, status="done" if sc.get("active") else "error")
        _after_scene_job(pid)


def _after_scene_job(pid):
    """Tayyor loyihaning bitta sahnasi qayta yozilgach, loyiha holatini (tayyor / xatoli) yangilaydi."""
    p = D.project(pid)
    if p and p["status"] in ("done", "partial"):
        scs = D.scenes(pid, full=False)
        bad = [s for s in scs if s["status"] != "done"]
        D.set_project(pid, status="partial" if bad else "done", progress=100, msg=(f"{len(bad)} ta sahna yozilmadi" if bad else "Hammasi tayyor"))


# ---------------------------------------------------------------- matn va ZIP
def scene_text(sc: dict) -> str:
    """Bitta sahna uchun to'liq matn: ssenariy qismi + barcha promptlar (ZIP uchun)."""
    out = [f"SAHNA {sc['idx']} ({sc['t_from']:g}-{sc['t_to']:g} s, {sc['duration']:g} s)" + (f" — {sc['label']}" if sc.get("label") else ""),
           f"Ko'rinish: {sc['visual']}", f"Ovoz: {sc['voice'] or '-'}", f"Ekranda: {sc['onscreen'] or '-'}", ""]
    a = sc.get("active")
    if a:
        out += ["RASM PROMPTI (English):", a["image_prompt"], ""]
    for plat, t in (sc.get("video") or {}).items():
        out += [f"VIDEO PROMPTI — {plat} (English):", t, ""]
    if sc.get("audio"):
        out += ["AUDIO PROMPTI (English; ovoz matni o'zbekcha):", sc["audio"], ""]
    return "\n".join(out)


def build_zip(pid: int, history=False) -> bytes:
    p = D.project(pid)
    scs = D.scenes(pid)
    img, vids = D.project_platforms(p)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("SSENARIY.txt", f"{p['title']}\nBrend: {p['brand_name']}\nFormat: {p['fmt']}\n"
                                   f"Rasm platformasi: {p['image_platform'] or '-'}\nVideo platformalar: {', '.join(v['name'] for v in vids) or '-'}\n\n{p['script']}\n")
        allp = []
        for s in scs:
            txt = scene_text(s)
            allp.append(txt)
            z.writestr(f"promptlar/sahna_{s['idx']:02d}.txt", txt)
            if history:
                for v in s["versions"]:
                    z.writestr(f"tarix/sahna_{s['idx']:02d}_v{v['n']}.txt",
                               f"Versiya {v['n']} ({v['created_at']})" + (f"\nIzoh: {v['note']}" if v["note"] else "") + f"\n\nRASM PROMPTI:\n{v['image_prompt']}\n")
        z.writestr("PROMPTLAR.txt", ("\n" + "=" * 70 + "\n\n").join(allp))
        roles = D.roles(pid)
        lines = []
        for r in roles:
            pe = r.get("person")
            if not pe:
                continue
            lines.append(f"{r['title']} = {pe['name']}: {r.get('identity') or ''}")
            for ph in pe["photos"]:
                f = D.resolve(ph["path"])
                if f:
                    z.write(f, f"referens/{D.safe_name(r['title'])}_{D.safe_name(pe['name'])}/{D.safe_name(ph['label'] or str(ph['id']))}_{ph['id']}.jpg", compress_type=zipfile.ZIP_STORED)
        if lines:
            z.writestr("referens/ODAMLAR.txt", "Shu rasmlarni rasm/video platformasiga referens sifatida yuklang (yuz o'zgarmasligi uchun):\n\n" + "\n".join(lines))
    return buf.getvalue()


# ---------------------------------------------------------------- Tizim tahlili
def health_items(item) -> list[dict]:
    """syscheck.run_all() ga bitta qator: Image Generator holati (kalit, navbat, oxirgi xatolar)."""
    D.ready()
    g = "Image Generator"
    out = []
    if not ai.configured():
        return [item(g, "Prompt yozish (OpenAI)", "info", "OpenAI kaliti kiritilmagan", "Image Generator → Sozlamalar.", key="img_key")]
    busy = D.busy_count()
    failed = db.one("SELECT COUNT(*) c FROM img_jobs WHERE status='error' AND finished_at >= ?", ((datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),))["c"]
    stuck = db.one("SELECT COUNT(*) c FROM img_jobs WHERE status='queued' AND created_at < ?", ((datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),))["c"]
    state = "warn" if (failed or stuck) else "ok"
    detail = f"navbatda/ishlayotgan: {busy}; so'nggi 2 kunda xatoli ishlar: {failed}"
    fix = ""
    if stuck:
        detail += f"; 30 daqiqadan beri kutayotgan: {stuck}"
        fix = "Navbat siklini «Fon jarayonlari» qatorida tekshiring; dasturni qayta ishga tushiring."
    elif failed:
        fix = "Loyiha sahifasida xato sababi ko'rinadi (balans, moderatsiya yoki tarmoq)."
    out.append(item(g, "Prompt yozish navbati", state, detail, fix, key="img_queue"))
    return out
