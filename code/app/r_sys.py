"""Tizim tahlili: holat tekshiruvi, loglar, AI xulosa, yo'riqnoma."""
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from . import ai, db, syscheck
from .config import LOG_DIR, log
from .web import go, page

router = APIRouter()
_last: dict = {}


@router.get("/sys")
async def sys_home(request: Request):
    rep = _last.get("rep") if request.query_params.get("fresh") != "1" else None
    if rep is None:
        rep = await syscheck.run_all()
        _last["rep"] = rep
    concl = None
    raw = db.get_setting("sys_ai")
    if raw:
        try:
            concl = json.loads(raw)
        except Exception:
            concl = None
    return page(request, "sys.html", rep=rep, concl=concl, ai_ok=ai.configured(), log_path=str(LOG_DIR / "app.log"))


@router.post("/sys/run")
async def sys_run():
    _last["rep"] = await syscheck.run_all()
    return go("/sys", msg="Tekshiruv yangilandi")


@router.post("/sys/ai")
async def sys_ai():
    rep = _last.get("rep") or await syscheck.run_all()
    try:
        data = await syscheck.ai_conclusion(rep)
    except ai.AIError as e:
        return go("/sys", err=str(e))
    except Exception as e:
        log.warning("Tizim AI xulosasi xato", exc_info=True)
        return go("/sys", err=f"{type(e).__name__}: {e}")
    data["at"] = db.now()
    db.set_setting("sys_ai", json.dumps(data, ensure_ascii=False))
    return go("/sys", msg="AI xulosa tayyor")


@router.post("/sys/fix/mysql")
async def sys_fix_mysql():
    import asyncio
    try:
        msg = await asyncio.get_event_loop().run_in_executor(None, syscheck.fix_mysql)
    except Exception as e:
        return go("/sys", err=f"MySQL ishga tushmadi: {e}")
    return go("/sys?fresh=1", msg=msg)


@router.post("/sys/events/clear")
async def sys_events_clear():
    db.ex("DELETE FROM sys_events")
    return go("/sys?fresh=1", msg="Voqealar tozalandi")


@router.get("/sys/guide")
async def sys_guide(request: Request):
    return page(request, "sys_guide.html")


@router.get("/sys/api")
async def sys_api():
    rep = await syscheck.run_all()
    return JSONResponse({"counts": dict(rep["counts"]), "at": rep["at"]})
