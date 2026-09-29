"""Excel orqali guruhlarga a'zo bo'lish (akkaunt bo'yicha)."""
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import db
from .config import IMPORT_DIR, JOIN_MIN_FLOOR, log
from .core import joiner, manager
from .excel_import import SUPPORTED, analyze, export_report
from .web import BATCH_LABELS, JOIN_LABELS, acc_id, go, need_account, page

router = APIRouter()


def batch_counts(bid: int) -> dict:
    c = {r["status"]: r["c"] for r in db.q(
        "SELECT status, COUNT(*) c FROM join_targets WHERE batch_id=? GROUP BY status", (bid,))}
    total = sum(c.values())
    pending = c.get("pending", 0)
    ok = c.get("joined", 0) + c.get("already", 0)
    req = c.get("requested", 0)
    return {"total": total, "pending": pending, "joined": c.get("joined", 0), "already": c.get("already", 0),
            "requested": req, "ok": ok, "failed": total - pending - ok - req, "by": c}


def _own(bid, request):
    b = db.one("SELECT * FROM join_batches WHERE id=?", (bid,))
    return b


@router.get("/join")
async def join_page(request: Request):
    aid = acc_id(request)
    batches = db.q("SELECT * FROM join_batches WHERE account_id=? AND status!='draft' ORDER BY id DESC LIMIT 100", (aid,)) if aid else []
    st = await manager.get(aid).status() if aid else {}
    return page(request, "join.html", st=st, batches=[dict(b, **batch_counts(b["id"])) for b in batches],
                default_min=db.get_setting("join_min", 60), default_max=db.get_setting("join_max", 180),
                default_daily=db.get_setting("join_daily", 40))


@router.post("/join/upload")
async def join_upload(request: Request, file: UploadFile = File(...)):
    if (r := need_account(request)):
        return r
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUPPORTED:
        return go("/join", err="Faqat .xlsx, .csv yoki .txt fayl yuklang (eski .xls ni .xlsx qilib saqlang)")
    dest = IMPORT_DIR / f"{uuid.uuid4().hex}{ext}"
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
    try:
        columns = analyze(dest)
    except Exception as e:
        log.exception("Excel o'qish xatosi")
        return go("/join", err=f"Faylni o'qib bo'lmadi: {e}")
    if not columns:
        return go("/join", err="Faylda username yoki t.me havolasi topilmadi")
    bid = db.ex("INSERT INTO join_batches(account_id,filename,status,created_at,raw) VALUES(?,?,?,?,?)",
                (acc_id(request), file.filename, "draft", db.now(), json.dumps(columns, ensure_ascii=False)))
    return go(f"/join/{bid}/setup")


@router.get("/join/{bid}/setup")
async def join_setup(request: Request, bid: int):
    b = _own(bid, request)
    if not b:
        return go("/join", err="Topilmadi")
    if b["status"] != "draft":
        return go(f"/join/{bid}")
    columns = json.loads(b["raw"])
    best = max(range(len(columns)), key=lambda i: len(columns[i]["items"]))
    st = await manager.get(b["account_id"]).status()
    return page(request, "join_setup.html", b=b, columns=columns, best=best, st=st,
                min_delay=db.get_setting("join_min", 60), max_delay=db.get_setting("join_max", 180),
                daily=db.get_setting("join_daily", 40), floor=JOIN_MIN_FLOOR)


@router.post("/join/{bid}/start")
async def join_start(bid: int, cols: list[int] = Form(default=[]), min_delay: int = Form(60),
                     max_delay: int = Form(180), daily_limit: int = Form(40)):
    b = db.one("SELECT * FROM join_batches WHERE id=? AND status='draft'", (bid,))
    if not b:
        return go("/join", err="Paket topilmadi yoki allaqachon boshlangan")
    if not (await manager.get(b["account_id"]).status()).get("authorized"):
        return go("/accounts", err="Bu akkaunt Telegram'ga ulanmagan")
    if min_delay < JOIN_MIN_FLOOR or max_delay < min_delay:
        return go(f"/join/{bid}/setup", err=f"Interval noto'g'ri: min kamida {JOIN_MIN_FLOOR}s, max >= min")
    if not 1 <= daily_limit <= 500:
        return go(f"/join/{bid}/setup", err="Kunlik limit 1 dan 500 gacha bo'lsin")
    columns = json.loads(b["raw"])
    seen, rows = set(), []
    for ci in cols:
        if 0 <= ci < len(columns):
            for it in columns[ci]["items"]:
                k = (it["kind"], it["key"].lower() if it["kind"] == "username" else it["key"])
                if k not in seen:
                    seen.add(k)
                    rows.append((bid, it["ref"], it["kind"], it["key"]))
    if not rows:
        return go(f"/join/{bid}/setup", err="Kamida bitta ustunni tanlang")
    db.many("INSERT INTO join_targets(batch_id,ref,kind,key) VALUES(?,?,?,?)", rows)
    db.ex("UPDATE join_batches SET status='queued', total=?, min_delay=?, max_delay=?, daily_limit=?, raw=NULL WHERE id=?",
          (len(rows), min_delay, max_delay, daily_limit, bid))
    db.set_setting("join_min", min_delay)
    db.set_setting("join_max", max_delay)
    db.set_setting("join_daily", daily_limit)
    joiner.start(bid)
    return go(f"/join/{bid}", msg="A'zo bo'lish boshlandi")


@router.get("/join/{bid}")
async def join_report(request: Request, bid: int):
    b = _own(bid, request)
    if not b:
        return go("/join", err="Topilmadi")
    if b["status"] == "draft":
        return go(f"/join/{bid}/setup")
    return page(request, "join_report.html", b=b)


@router.get("/api/join/{bid}")
async def join_api(bid: int, rev: str = ""):
    b = db.one("SELECT * FROM join_batches WHERE id=?", (bid,))
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    counts = batch_counts(bid)
    new_rev = f"{counts['total'] - counts['pending']}:{b['status']}"
    data = {"b": {k: b[k] for k in ("id", "filename", "status", "next_at", "resume_at", "error", "daily_limit",
                                    "min_delay", "max_delay", "created_at")},
            "status_label": BATCH_LABELS.get(b["status"], b["status"]), "counts": counts,
            "server_now": db.now(), "rev": new_rev}
    if rev != new_rev:
        data["targets"] = [dict(t) for t in db.q(
            "SELECT id,ref,kind,status,detail,title,tried_at FROM join_targets WHERE batch_id=? ORDER BY id", (bid,))]
    return data


@router.post("/join/{bid}/cancel")
async def join_cancel(bid: int):
    db.ex("UPDATE join_batches SET status='cancelled', next_at=NULL WHERE id=? "
          "AND status IN ('queued','running','waiting')", (bid,))
    return go(f"/join/{bid}", msg="To'xtatildi")


@router.post("/join/{bid}/resume")
async def join_resume(bid: int):
    b = db.one("SELECT * FROM join_batches WHERE id=? AND status IN ('cancelled','interrupted','failed','waiting')", (bid,))
    if not b:
        return go(f"/join/{bid}", err="Davom ettirib bo'lmaydi")
    if not (await manager.get(b["account_id"]).status()).get("authorized"):
        return go("/accounts", err="Bu akkaunt Telegram'ga ulanmagan")
    db.ex("UPDATE join_batches SET status='queued', finished_at=NULL, error=NULL WHERE id=?", (bid,))
    joiner.start(bid)
    return go(f"/join/{bid}", msg="Davom ettirilmoqda")


@router.post("/join/{bid}/retry")
async def join_retry(bid: int):
    b = db.one("SELECT * FROM join_batches WHERE id=? AND status NOT IN ('running','queued','draft')", (bid,))
    if not b:
        return go(f"/join/{bid}", err="Hozir qayta urinib bo'lmaydi (jarayon ishlayapti)")
    if not (await manager.get(b["account_id"]).status()).get("authorized"):
        return go("/accounts", err="Bu akkaunt Telegram'ga ulanmagan")
    n = db.q("SELECT COUNT(*) c FROM join_targets WHERE batch_id=? AND status NOT IN "
             "('joined','already','requested','pending')", (bid,))[0]["c"]
    if not n:
        return go(f"/join/{bid}", err="Qayta urinadigan xatolar yo'q")
    db.ex("UPDATE join_targets SET status='pending', detail=NULL, tried_at=NULL WHERE batch_id=? AND status NOT IN "
          "('joined','already','requested','pending')", (bid,))
    db.ex("UPDATE join_batches SET status='queued', finished_at=NULL, error=NULL WHERE id=?", (bid,))
    joiner.start(bid)
    return go(f"/join/{bid}", msg=f"{n} ta ulanmagan guruhga qayta urinish boshlandi")


@router.post("/join/{bid}/delete")
async def join_delete(bid: int):
    db.ex("DELETE FROM join_batches WHERE id=? AND status NOT IN ('running','queued')", (bid,))
    return go("/join", msg="O'chirildi")


@router.get("/join/{bid}/export.xlsx")
async def join_export(bid: int):
    b = db.one("SELECT * FROM join_batches WHERE id=?", (bid,))
    if not b:
        return go("/join", err="Topilmadi")
    targets = db.q("SELECT * FROM join_targets WHERE batch_id=? ORDER BY id", (bid,))
    data = export_report(b, targets, JOIN_LABELS)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="azo_bolish_hisobot_{bid}.xlsx"'})
