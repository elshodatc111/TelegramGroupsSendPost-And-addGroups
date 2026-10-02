"""Image Generator bo'limi route'lari: loyihalar, brendlar (odamlar), joylar, sozlamalar (OpenAI, modellar, video platformalar), yo'riqnoma."""
import io

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from markupsafe import Markup

from . import ai, db, img_ai, img_db as D, img_work as W, web
from .web import go, page

router = APIRouter()
YEAR = 60 * 60 * 24 * 365
MAX_UPLOAD = 20 * 1024 * 1024

web.ICONS.update({k: Markup(v) for k, v in {
    "wand": '<path d="M15 4V2"/><path d="M15 16v-2"/><path d="M8 9h2"/><path d="M20 9h2"/><path d="M17.8 11.8L19 13"/><path d="M17.8 6.2L19 5"/><path d="M3 21l9-9"/><path d="M12.2 6.2L11 5"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "package": '<path d="M16.5 9.4L7.5 4.21"/><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
    "map-pin": '<path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/>',
}.items()})


# ================================================================ umumiy
def ipage(request: Request, name: str, **ctx):
    D.ready()
    ctx.update(img_busy=D.busy_count(), ai_ok=ai.configured())
    return page(request, name, **ctx)


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


async def save_upload(f, *parts: str) -> str:
    """Yuklangan rasmni tekshiradi (Pillow), aylantirilgan holda JPEG qilib saqlaydi. Nisbiy yo'lni qaytaradi; xato bo'lsa ValueError (o'zbekcha)."""
    from PIL import Image, ImageOps
    data = await f.read()
    if not data:
        raise ValueError("Fayl bo'sh")
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"«{f.filename}» juda katta (20 MB gacha)")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise ValueError(f"«{f.filename}» rasm emas yoki buzilgan (JPG, PNG, WEBP)")
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((2048, 2048))
    dest, rel = D.new_path(*parts)
    im.save(dest, "JPEG", quality=92)
    return rel


def _files(form, key):
    return [f for f in form.getlist(key) if getattr(f, "filename", None)]


# ================================================================ bo'lim almashtirish va fayllar
@router.post("/img/switch")
async def img_switch():
    resp = RedirectResponse("/img", status_code=303)
    resp.set_cookie("ws", "img", max_age=YEAR, samesite="lax")
    return resp


@router.get("/img/f/{path:path}")
async def img_file(path: str, dl: str = ""):
    p = D.resolve(path)
    if not p:
        return Response("Topilmadi", status_code=404)
    headers = {"Cache-Control": "private, max-age=3600"}
    if dl:
        headers["Content-Disposition"] = f'attachment; filename="{D.safe_name(dl, "rasm")}.jpg"'
    return FileResponse(p, media_type="image/jpeg", headers=headers)


# ================================================================ loyihalar
@router.get("/img")
async def home(request: Request):
    ps = D.projects()
    return ipage(request, "img_home.html", projects=ps, STATUS=D.STATUS_UZ, n_brands=len(D.brands()), cost30=D.total_cost(30))


@router.get("/img/new")
async def new_page(request: Request):
    return ipage(request, "img_new.html", brands=D.brands(), FORMATS=D.FORMATS, STYLES=D.STYLES,
                 bid=_int(request.query_params.get("brand")), img_plats=D.platforms(active_only=True, kind="image"),
                 vid_plats=D.platforms(active_only=True, kind="video"))


@router.post("/img/script-ai")
async def script_ai(brand_id: int = Form(...), topic: str = Form(""), seconds: int = Form(30), extra: str = Form("")):
    b = D.brand(brand_id)
    if not b:
        return JSONResponse({"ok": False, "err": "Avval brendni tanlang"})
    if not topic.strip():
        return JSONResponse({"ok": False, "err": "Mavzuni yozing"})
    try:
        res = await img_ai.write_script(b, topic.strip(), seconds, extra, D.people(brand_id))
    except ai.AIError as e:
        return JSONResponse({"ok": False, "err": str(e)})
    return JSONResponse({"ok": True, **res})


@router.post("/img/new")
async def create(request: Request):
    f = await request.form()
    brand_id = _int(f.get("brand_id"), 0)
    script = (f.get("script") or "").strip()
    if not D.brand(brand_id):
        return go("/img/new", err="Brendni tanlang (avval «Brendlar» bo'limida brend yarating)")
    if len(script) < 20:
        return go("/img/new", err="Ssenariyni kiriting (yoki mavzu yozib «AI ssenariy yozsin» ni bosing)")
    img = (f.get("image_platform") or "").strip()
    if not any(x["name"] == img for x in D.platforms(active_only=True, kind="image")):
        return go("/img/new", err="Rasm promptlari qaysi platforma uchun yozilishini tanlang (platformalarni Sozlamalarda qo'shishingiz mumkin)")
    vnames = {x["name"] for x in D.platforms(active_only=True, kind="video")}
    vids = [v for v in f.getlist("video_platforms") if v in vnames]
    if not ai.configured():
        return go("/img/settings", err="Avval OpenAI kalitini kiriting")
    pid = D.create_project(brand_id, f.get("title"), script, f.get("fmt") or "9:16", f.get("style") or "real", f.get("topic") or "", img, vids)
    D.enqueue("analyze", pid)
    D.set_project(pid, status="analyzing", msg="Navbatda")
    return go(f"/img/p/{pid}", msg="Ssenariy o'rganilmoqda...")


def _problems(pid) -> list[str]:
    out = []
    if not ai.configured():
        out.append("OpenAI kaliti kiritilmagan")
    for r in D.roles(pid):
        if not r["person"]:
            out.append(f"«{r['title']}» uchun odam tanlanmagan")
            continue
        if not r["person"]["consent"]:
            out.append(f"«{r['person']['name']}»: roziligi olinganligi belgilanmagan")
        if not r["person"]["photos"]:
            out.append(f"«{r['person']['name']}»: referens rasmlar yo'q (kamera orqali oling yoki yuklang)")
    for pl in D.plocs(pid):
        if not (pl["description"] or "").strip():
            out.append(f"Joy «{pl['name']}»: tavsif bo'sh")
    return out


@router.get("/img/p/{pid}")
async def project_page(request: Request, pid: int):
    p = D.project(pid)
    if not p:
        return go("/img", err="Loyiha topilmadi")
    b = D.brand(p["brand_id"])
    img, vids = D.project_platforms(p)
    common = dict(proj=p, brand=b, STATUS=D.STATUS_UZ, FORMATS=D.FORMATS, STYLES=D.STYLES, p_img=img, p_vids=vids)
    if p["status"] in ("new", "analyzing", "error") and not D.scenes(pid, full=False):
        return ipage(request, "img_wait.html", **common)
    if p["status"] == "needs_assets":
        return ipage(request, "img_assets.html", scenes=D.scenes(pid, full=False), roles=D.roles(pid), plocs=D.plocs(pid), people=D.people(p["brand_id"]),
                     libs=D.locations(p["brand_id"]), problems=_problems(pid), KINDS=D.KINDS, **common)
    scs = D.scenes(pid)
    return ipage(request, "img_project.html", scenes=scs, roles=D.roles(pid), plocs=D.plocs(pid),
                 n_done=len([s for s in scs if s["status"] == "done"]), problems=_problems(pid) if p["status"] in ("stopped", "error") else [], **common)


@router.get("/img/p/{pid}/status.json")
async def project_status(pid: int):
    p = D.project(pid)
    if not p:
        return JSONResponse({"gone": True})
    scs = D.scenes(pid, full=False)
    active = {"writing"}
    busy = p["status"] in ("analyzing", "running") or any(s["status"] in active for s in scs) or D.job_active(project_id=pid) or any(D.job_active(scene_id=s["id"]) for s in scs)
    sig = ";".join(f"{s['id']}:{s['status']}:{s['active_ver']}" for s in scs) + f"|{p['status']}|{len(scs)}"
    return JSONResponse({"status": p["status"], "status_uz": D.STATUS_UZ.get(p["status"], p["status"]), "progress": p["progress"], "msg": p["msg"] or "",
                         "busy": busy, "sig": sig, "error": p["error"] or "", "cost": round(p["cost"] or 0, 4),
                         "scenes": {s["id"]: D.SCENE_UZ.get(s["status"], s["status"]) for s in scs}})


# ---------------------------------------------------------------- 2-bosqich: tahlil natijasini tasdiqlash
def _editable(pid):
    p = D.project(pid)
    return p if p and p["status"] == "needs_assets" else None


@router.post("/img/p/{pid}/scene/{sid}/save")
async def scene_save(pid: int, sid: int, label: str = Form(""), visual: str = Form(""), voice: str = Form(""), onscreen: str = Form("")):
    if not _editable(pid) or not (s := D.scene(sid, full=False)) or s["project_id"] != pid:
        return go(f"/img/p/{pid}", err="Sahnani hozir o'zgartirib bo'lmaydi")
    if not visual.strip():
        return go(f"/img/p/{pid}#s{sid}", err="Ko'rinish tavsifi bo'sh bo'lmasin")
    D.set_scene(sid, label=label.strip()[:180], visual=visual.strip(), voice=voice.strip(), onscreen=onscreen.strip())
    return go(f"/img/p/{pid}#s{sid}", msg=f"{s['idx']}-sahna saqlandi")


@router.post("/img/p/{pid}/loc/{plid}/save")
async def ploc_save(pid: int, plid: int, name: str = Form(""), description: str = Form(""), location_id: int = Form(0)):
    p = _editable(pid)
    pl = db.one("SELECT * FROM img_plocs WHERE id=? AND project_id=?", (plid, pid))
    if not p or not pl:
        return go(f"/img/p/{pid}", err="Joyni hozir o'zgartirib bo'lmaydi")
    lib = D.location(location_id) if location_id else None
    if lib and lib["brand_id"] != p["brand_id"]:
        lib = None
    desc = description.strip() or (lib["description"] if lib else "")
    if not desc:
        return go(f"/img/p/{pid}#l{plid}", err="Joy tavsifini yozing")
    db.ex("UPDATE img_plocs SET name=?, description=?, location_id=? WHERE id=?", ((name.strip() or pl["name"])[:180], desc, lib["id"] if lib else None, plid))
    return go(f"/img/p/{pid}#l{plid}", msg="Joy saqlandi")


@router.post("/img/p/{pid}/role/{rid}/person")
async def role_person(pid: int, rid: int, person_id: str = Form(""), new_name: str = Form(""), new_kind: str = Form("staff"), consent: str = Form("")):
    p = _editable(pid)
    r = db.one("SELECT * FROM img_roles WHERE id=? AND project_id=?", (rid, pid))
    if not p or not r:
        return go(f"/img/p/{pid}", err="Obrazni hozir o'zgartirib bo'lmaydi")
    if person_id == "new":
        if not new_name.strip():
            return go(f"/img/p/{pid}#r{rid}", err="Yangi odam ismini kiriting")
        pe, err = D.save_person(p["brand_id"], {"name": new_name, "kind": new_kind, "appearance": r["description"], "consent": consent})
        if err:
            return go(f"/img/p/{pid}#r{rid}", err=err)
    else:
        pe = _int(person_id)
        per = D.person(pe) if pe else None
        if not per or per["brand_id"] != p["brand_id"]:
            return go(f"/img/p/{pid}#r{rid}", err="Odamni tanlang")
        db.ex("UPDATE img_people SET consent=? WHERE id=?", (1 if consent else 0, pe))
    per = D.person(pe)
    if r["person_id"] != pe:                               # odam almashsa: eski tavsif tozalanadi (yangisi yoziladi)
        db.ex("UPDATE img_roles SET identity=NULL WHERE id=?", (rid,))
    db.ex("UPDATE img_roles SET person_id=? WHERE id=?", (pe, rid))
    return go(f"/img/p/{pid}#r{rid}", msg=f"«{per['name']}» tanlandi")


@router.post("/img/p/{pid}/reanalyze")
async def reanalyze(pid: int, script: str = Form("")):
    p = D.project(pid)
    if not p or p["status"] not in ("needs_assets", "error", "new", "analyzing"):
        return go(f"/img/p/{pid}", err="Ssenariyni faqat boshlanishidan oldin qayta tahlil qilish mumkin")
    if D.job_active(project_id=pid):
        return go(f"/img/p/{pid}", err="Tahlil allaqachon ketmoqda")
    if len(script.strip()) >= 20:
        db.ex("UPDATE img_projects SET script=? WHERE id=?", (script.strip(), pid))
    D.enqueue("analyze", pid)
    D.set_project(pid, status="analyzing", msg="Navbatda", error=None)
    return go(f"/img/p/{pid}", msg="Ssenariy qayta o'rganilmoqda (obrazlar tanlovi tozalanadi)")


@router.post("/img/p/{pid}/start")
async def start(pid: int):
    p = D.project(pid)
    if not p or p["status"] not in ("needs_assets", "stopped", "partial", "error"):
        return go(f"/img/p/{pid}", err="Loyihani hozir boshlab bo'lmaydi")
    pr = _problems(pid)
    if pr:
        return go(f"/img/p/{pid}", err="Boshlashdan oldin: " + "; ".join(pr)[:700])
    if D.job_active(kind="pipeline", project_id=pid):
        return go(f"/img/p/{pid}", err="Allaqachon ishlayapti")
    D.enqueue("pipeline", pid)
    D.set_project(pid, status="running", msg="Navbatda", error=None)
    return go(f"/img/p/{pid}", msg="Boshlandi: jarayon orqa fonda ishlaydi, sahifani yopsangiz ham davom etadi")


@router.post("/img/p/{pid}/stop")
async def stop(pid: int):
    p = D.project(pid)
    if not p:
        return go("/img", err="Loyiha topilmadi")
    db.ex("UPDATE img_jobs SET status='cancelled', finished_at=? WHERE project_id=? AND status='queued'", (D.now(), pid))
    D.set_project(pid, status="stopped", msg="To'xtatildi")
    return go(f"/img/p/{pid}", msg="To'xtatildi (hozirgi qadam tugagach to'xtaydi)")


@router.post("/img/p/{pid}/delete")
async def delete(pid: int):
    p = D.project(pid)
    if p and p["status"] in ("analyzing", "running") and D.job_active(project_id=pid):
        D.set_project(pid, status="stopped")
    D.delete_project(pid)
    return go("/img", msg="Loyiha o'chirildi")


# ---------------------------------------------------------------- natija: sahna amallari
def _scene_job(sid, payload=None):
    s = D.scene(sid, full=False)
    if not s:
        return None, "Sahna topilmadi"
    if D.job_active(scene_id=sid) or s["status"] == "writing":
        return s, "Bu sahna hozir yozilmoqda"
    D.enqueue("regen", s["project_id"], sid, payload)
    D.set_scene(sid, status="writing", error=None)
    return s, None


@router.post("/img/s/{sid}/edit")
async def scene_edit(sid: int, note: str = Form("")):
    if len(note.strip()) < 3:
        return go("/img", err="Nimani o'zgartirish kerakligini yozing")
    s, err = _scene_job(sid, {"note": note.strip()})
    return go(f"/img/p/{s['project_id']}#s{sid}" if s else "/img", **({"err": err} if err else {"msg": "Tavsif bo'yicha promptlar qayta yozilmoqda"}))


@router.post("/img/s/{sid}/reroll")
async def scene_reroll(sid: int):
    s, err = _scene_job(sid, {})
    return go(f"/img/p/{s['project_id']}#s{sid}" if s else "/img", **({"err": err} if err else {"msg": "Boshqa variant yozilmoqda"}))


@router.post("/img/s/{sid}/activate/{vid}")
async def scene_activate(sid: int, vid: int):
    s = D.scene(sid)
    v = next((x for x in (s or {}).get("versions", []) if x["id"] == vid), None)
    if not s or not v:
        return go("/img", err="Versiya topilmadi")
    D.set_scene(sid, active_ver=vid)
    return go(f"/img/p/{s['project_id']}#s{sid}", msg=f"{v['n']}-versiya asosiy qilindi")


@router.get("/img/p/{pid}/zip")
async def project_zip(pid: int, history: str = ""):
    p = D.project(pid)
    if not p:
        return Response("Topilmadi", status_code=404)
    data = W.build_zip(pid, history=bool(history))
    name = D.safe_name(p["title"], f"loyiha_{pid}") + ("_tarix" if history else "") + ".zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ================================================================ brendlar va odamlar
@router.get("/img/brands")
async def brands_page(request: Request):
    return ipage(request, "img_brands.html", brands=D.brands())


def _set_logo(bid, rel):
    old = D.brand(bid)
    if old and old["logo"] and old["logo"] != rel:
        D.rm_file(old["logo"])
    db.ex("UPDATE img_brands SET logo=? WHERE id=?", (rel, bid))


@router.post("/img/brands")
async def brand_create(request: Request):
    form = await request.form()
    bid, err = D.save_brand(dict(form))
    if err:
        return go("/img/brands", err=err)
    files = _files(form, "logo")
    if files:
        try:
            _set_logo(bid, await save_upload(files[0], "brands", str(bid)))
        except ValueError as e:
            return go(f"/img/brands/{bid}", err=str(e))
    return go(f"/img/brands/{bid}", msg="Brend qo'shildi. Endi odamlarini (xodim, o'qituvchi) qo'shing")


@router.get("/img/brands/{bid}")
async def brand_page(request: Request, bid: int):
    b = D.brand(bid)
    if not b:
        return go("/img/brands", err="Brend topilmadi")
    return ipage(request, "img_brand.html", b=b, people=D.people(bid), locs=D.locations(bid), KINDS=D.KINDS, STATUS=D.STATUS_UZ,
                 projects=[p for p in D.projects() if p["brand_id"] == bid])


@router.post("/img/brands/{bid}/save")
async def brand_save(request: Request, bid: int):
    form = await request.form()
    if not D.brand(bid):
        return go("/img/brands", err="Brend topilmadi")
    _, err = D.save_brand(dict(form), bid)
    if err:
        return go(f"/img/brands/{bid}", err=err)
    files = _files(form, "logo")
    if files:
        try:
            _set_logo(bid, await save_upload(files[0], "brands", str(bid)))
        except ValueError as e:
            return go(f"/img/brands/{bid}", err=str(e))
    return go(f"/img/brands/{bid}", msg="Brend saqlandi")


@router.post("/img/brands/{bid}/delete")
async def brand_delete(bid: int):
    err = D.delete_brand(bid)
    return go("/img/brands", **({"err": err} if err else {"msg": "Brend va unga tegishli hamma narsa o'chirildi"}))


async def _add_photos(pid: int, form) -> str:
    """Yuklangan yoki kameradan olingan rasmlarni odamga qo'shadi. Xabar matnini qaytaradi (xatolar qavs ichida)."""
    one = (form.get("label") or "").strip()
    msg = ""
    for f in _files(form, "photos"):
        name = (f.filename or "").lower()
        shot = name[4:].split(".")[0] if name.startswith("cam_") else ""          # kameradan: cam_front.jpg ...
        try:
            D.add_photo(pid, await save_upload(f, "people", str(pid)), D.CAM_LABELS.get(shot, one).strip())
        except ValueError as e:
            msg += f" ({e})"
    return msg


@router.post("/img/brands/{bid}/people")
async def person_add(request: Request, bid: int):
    form = await request.form()
    if not D.brand(bid):
        return go("/img/brands", err="Brend topilmadi")
    pid, err = D.save_person(bid, dict(form))
    if err:
        return go(f"/img/brands/{bid}", err=err)
    msg = "Odam qo'shildi" + await _add_photos(pid, form)
    return go(f"/img/brands/{bid}#p{pid}", msg=msg)


@router.post("/img/people/{pid}/save")
async def person_save(request: Request, pid: int):
    form = await request.form()
    pe = D.person(pid)
    if not pe:
        return go("/img/brands", err="Odam topilmadi")
    _, err = D.save_person(pe["brand_id"], dict(form), pid)
    if err:
        return go(f"/img/brands/{pe['brand_id']}#p{pid}", err=err)
    return go(f"/img/brands/{pe['brand_id']}#p{pid}", msg="Saqlandi" + await _add_photos(pid, form))


@router.post("/img/people/{pid}/photos")
async def person_photos(request: Request, pid: int):
    """Faqat rasm qo'shish (kamera yoki fayl): loyiha sahifasidan ham ishlaydi."""
    form = await request.form()
    pe = D.person(pid)
    if not pe:
        return go("/img/brands", err="Odam topilmadi")
    nxt = form.get("next") or ""
    dest = nxt if nxt.startswith("/img/") and "//" not in nxt else f"/img/brands/{pe['brand_id']}#p{pid}"
    msg = await _add_photos(pid, form)
    return go(dest, msg="Rasmlar qo'shildi" + msg)


@router.post("/img/people/{pid}/photo/{phid}/delete")
async def photo_delete(pid: int, phid: int):
    pe = D.person(pid)
    ph = D.photo(phid)
    if not pe or not ph or ph["person_id"] != pid:
        return go("/img/brands", err="Rasm topilmadi")
    D.delete_photo(phid)
    return go(f"/img/brands/{pe['brand_id']}#p{pid}", msg="Rasm o'chirildi")


@router.post("/img/people/{pid}/delete")
async def person_delete(pid: int):
    pe = D.person(pid)
    if not pe:
        return go("/img/brands", err="Odam topilmadi")
    D.delete_person(pid)
    return go(f"/img/brands/{pe['brand_id']}", msg="Odam o'chirildi")


# ================================================================ joylar kutubxonasi
@router.get("/img/locations")
async def locations_page(request: Request):
    bid = _int(request.query_params.get("brand"))
    return ipage(request, "img_locations.html", brands=D.brands(), bid=bid, locs=D.locations(bid))


@router.post("/img/locations/describe")
async def location_describe(brand_id: int = Form(...), name: str = Form("")):
    b = D.brand(brand_id)
    if not b or not name.strip():
        return JSONResponse({"ok": False, "err": "Brend va joy nomini kiriting"})
    try:
        data = await img_ai.text_json(
            "Write a detailed look of a location for a business video, in UZBEK, 2-4 sentences: walls, floor, light, furniture, props and colours that fit the business. "
            'Return ONLY JSON: {"description": "..."}', f"{D.brand_text(b)}\nLocation name: {name.strip()}", note="joy tavsifi", max_out=1200, effort="low")
    except ai.AIError as e:
        return JSONResponse({"ok": False, "err": str(e)})
    return JSONResponse({"ok": True, "description": str(data.get("description", "")).strip()})


@router.post("/img/locations")
async def location_add(brand_id: int = Form(...), name: str = Form(""), description: str = Form("")):
    if not D.brand(brand_id):
        return go("/img/locations", err="Brendni tanlang")
    lid, err = D.save_location(brand_id, name, description)
    return go(f"/img/locations?brand={brand_id}", **({"err": err} if err else {"msg": "Joy qo'shildi"}))


@router.post("/img/locations/{lid}/save")
async def location_save(lid: int, name: str = Form(""), description: str = Form("")):
    lo = D.location(lid)
    if not lo:
        return go("/img/locations", err="Joy topilmadi")
    _, err = D.save_location(lo["brand_id"], name, description, lid)
    return go(f"/img/locations?brand={lo['brand_id']}", **({"err": err} if err else {"msg": "Saqlandi"}))


@router.post("/img/locations/{lid}/delete")
async def location_delete(lid: int):
    lo = D.location(lid)
    if lo:
        D.delete_location(lid)
    return go(f"/img/locations?brand={lo['brand_id']}" if lo else "/img/locations", msg="Joy o'chirildi")


# ================================================================ sozlamalar
def _settings_ctx(models=None):
    v = {"batch": D.cfg("batch"), "model_text_raw": db.get_setting("img_model_text") or "", "model_script_raw": db.get_setting("img_model_script") or ""}
    return dict(v=v, plats=D.platforms(), key_ok=ai.configured(), total=D.total_cost(), cost30=D.total_cost(30),
                defaults=dict(text=ai.model_for("analysis"), script=ai.model_for("idea")), models=models)


@router.get("/img/settings")
async def settings_page(request: Request):
    return ipage(request, "img_settings.html", **_settings_ctx())


@router.post("/img/settings/key")
async def settings_key(key: str = Form("")):
    if key.strip():
        db.set_setting("openai_key", key.strip())
        return go("/img/settings", msg="OpenAI kaliti saqlandi (shifrlangan). Telegram SMM va Instagram bo'limlarida ham shu kalit ishlatiladi")
    return go("/img/settings", err="Kalitni kiriting")


@router.post("/img/settings/models")
async def settings_models(request: Request):
    """Mavjud modellar ro'yxatini OpenAI'dan olib ko'rsatadi."""
    try:
        models = await ai.list_models()
    except ai.AIError as e:
        return go("/img/settings", err=str(e))
    return ipage(request, "img_settings.html", **_settings_ctx(models))


@router.post("/img/settings")
async def settings_save(request: Request):
    f = await request.form()
    for k in ("model_text", "model_script"):
        D.set_cfg(k, (f.get(k) or "").strip())             # bo'sh = umumiy standart model
    D.set_cfg("batch", str(max(1, min(_int(f.get("batch"), 5), 10))))
    return go("/img/settings", msg="Sozlamalar saqlandi")


def _plat_form(f):
    return f.get("name"), f.get("kind"), f.get("max_sec"), f.get("rules"), bool(f.get("audio"))


@router.post("/img/settings/platform/add")
async def platform_add(request: Request):
    err = D.save_platform(*_plat_form(await request.form()))
    return go("/img/settings#plat", **({"err": err} if err else {"msg": "Platforma qo'shildi: yangi loyihalarda tanlash mumkin"}))


@router.post("/img/settings/platform/{pid}/save")
async def platform_save(request: Request, pid: int):
    err = D.save_platform(*_plat_form(await request.form()), pid=pid)
    return go("/img/settings#plat", **({"err": err} if err else {"msg": "Platforma saqlandi"}))


@router.post("/img/settings/platform/{pid}/toggle")
async def platform_toggle(pid: int):
    db.ex("UPDATE img_platforms SET active=1-active WHERE id=?", (pid,))
    return go("/img/settings#plat")


@router.post("/img/settings/platform/{pid}/delete")
async def platform_delete(pid: int):
    db.ex("DELETE FROM img_platforms WHERE id=?", (pid,))
    return go("/img/settings#plat", msg="Platforma o'chirildi (tayyor promptlar saqlanib qoladi)")


@router.post("/img/settings/platform/restore")
async def platform_restore():
    n = 0
    for i, p in enumerate(D.PLATFORMS):
        if not db.one("SELECT 1 FROM img_platforms WHERE name=?", (p[0],)):
            db.ex("INSERT INTO img_platforms(name,kind,active,max_sec,rules,audio,sort,builtin) VALUES(?,?,1,?,?,?,?,1)", (p[0], p[1], p[2], p[4], p[3], i))
            n += 1
    return go("/img/settings#plat", msg=f"{n} ta standart platforma qaytarildi" if n else "Hamma standart platformalar mavjud")


@router.get("/img/guide")
async def guide(request: Request):
    return ipage(request, "img_guide.html")
