"""Instagram SMM → Cloudflare: bulutli fayl kutubxonasi (R2)."""
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from . import cf, db
from .config import TMP_DIR
from .web import go, page

router = APIRouter()


def _stats():
    rows = cf.files()
    return rows, {r["id"]: len(cf.referenced_by(r["id"])) for r in rows}


@router.get("/ig/cloud")
async def cloud_page(request: Request):
    q = request.query_params.get("q", "")
    order = request.query_params.get("o", "new")
    rows = cf.files(q, order)
    refs = {r["id"]: cf.referenced_by(r["id"]) for r in rows}
    c = cf.conf()
    return page(request, "ig_cloud.html", rows=rows, refs=refs, u=cf.usage(), c=c, has_secret=bool(c["secret"]), ok=cf.configured(),
                q=q, order=order, fmt=cf.fmt_size, url=cf.public_url, tests=None)


@router.post("/ig/cloud/upload")
async def cloud_upload(request: Request):
    if not cf.configured():
        return go("/ig/cloud", err="Avval pastdagi «Sozlamalar»da Cloudflare R2 ma'lumotlarini kiriting")
    form = await request.form()
    files = [f for f in form.getlist("files") if getattr(f, "filename", None)]
    if not files:
        return go("/ig/cloud", err="Fayl tanlanmadi")
    name = (form.get("name") or "").strip()
    ok, errs = [], []
    for i, f in enumerate(files):
        tmp = TMP_DIR / f"cfup_{uuid.uuid4().hex[:10]}{Path(f.filename).suffix.lower()}"
        try:
            with open(tmp, "wb") as out:
                while chunk := await f.read(1024 * 1024):
                    out.write(chunk)
            nm = name if (name and len(files) == 1) else (f"{name} {i + 1}" if name else Path(f.filename).stem)
            r = await cf.upload(tmp, nm, f.filename)
            ok.append(r["name"])
        except cf.CFError as e:
            errs.append(str(e))
        except Exception as e:
            errs.append(f"«{f.filename}»: {type(e).__name__}: {e}")
        finally:
            for p in TMP_DIR.glob(tmp.stem + ".*"):
                p.unlink(missing_ok=True)
    if errs and not ok:
        return go("/ig/cloud", err=" | ".join(errs)[:900])
    msg = f"Yuklandi: {', '.join(ok)[:300]}" + (f". Xatolar: {' | '.join(errs)[:400]}" if errs else "")
    return go("/ig/cloud", msg=msg)


@router.post("/ig/cloud/delete")
async def cloud_delete(request: Request):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    if not ids:
        return go("/ig/cloud", err="Hech narsa tanlanmadi")
    try:
        res = await cf.delete_files(ids, force=bool(form.get("force")))
    except cf.CFError as e:
        return go("/ig/cloud", err=str(e))
    parts = [f"O'chirildi: {res['deleted']} ta"]
    if res["skipped"]:
        parts.append("Rejada ishlatilgani uchun qoldi: " + "; ".join(res["skipped"])[:300])
    if res["errors"]:
        parts.append("Xato: " + "; ".join(res["errors"])[:300])
    return go("/ig/cloud", **({"err": ". ".join(parts)} if res["errors"] and not res["deleted"] else {"msg": ". ".join(parts)}))


@router.post("/ig/cloud/{fid}/rename")
async def cloud_rename(fid: int, name: str = Form("")):
    if not cf.get(fid) or not name.strip():
        return go("/ig/cloud", err="Nom bo'sh bo'lmasin")
    n = cf.rename(fid, name)
    return go("/ig/cloud", msg=f"Nom: «{n}»")


@router.post("/ig/cloud/sync")
async def cloud_sync():
    try:
        r = await cf.sync()
    except cf.CFError as e:
        return go("/ig/cloud", err=str(e))
    return go("/ig/cloud", msg=f"Solishtirildi: bucketda {r['total']} ta fayl, qo'shildi {r['added']}, ro'yxatdan olindi {r['removed']}")


@router.post("/ig/cloud/settings")
async def cloud_settings(account: str = Form(""), key_id: str = Form(""), secret: str = Form(""), bucket: str = Form(""), public: str = Form(""),
                         warn_gb: float = Form(9), limit_gb: float = Form(10), autodelete: str = Form("")):
    db.set_setting("r2_account", account.strip())
    db.set_setting("r2_key_id", key_id.strip())
    if secret.strip():
        db.set_setting("r2_secret", secret.strip())
    db.set_setting("r2_bucket", bucket.strip())
    db.set_setting("r2_public", public.strip().rstrip("/"))
    limit_gb = max(0.1, min(limit_gb, 1000))
    db.set_setting("r2_limit_gb", limit_gb)
    db.set_setting("r2_warn_gb", max(0.05, min(warn_gb, limit_gb)))
    db.set_setting("r2_autodelete", "1" if autodelete else "0")
    cf.check_alert()
    return go("/ig/cloud", msg="Cloudflare sozlamalari saqlandi")


@router.post("/ig/cloud/test")
async def cloud_test(request: Request):
    tests = await cf.test_connection() if cf.configured() else [("Sozlama", False, "Barcha maydonlarni to'ldirib saqlang")]
    c = cf.conf()
    return page(request, "ig_cloud.html", rows=cf.files(), refs={}, u=cf.usage(), c=c, has_secret=bool(c["secret"]), ok=cf.configured(),
                q="", order="new", fmt=cf.fmt_size, url=cf.public_url, tests=tests)


@router.get("/ig/cloud/list.json")
async def cloud_list(request: Request):
    rows = cf.files(request.query_params.get("q", ""), "new")
    return JSONResponse({"ok": cf.configured(), "files": [{"id": r["id"], "name": r["name"], "kind": r["kind"], "size": cf.fmt_size(r["size"]),
                                                           "url": cf.public_url(r), "used": r["used_n"]} for r in rows]})
