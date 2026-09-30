"""Kanallarim: kontent reja — rejalashtirilgan postlarni o'z kanalimizga yuborish."""
import asyncio
import json
from datetime import datetime, timedelta

from . import ch_data, ch_tg, ch_track, db
from .config import log

FMT = "%Y-%m-%d %H:%M:%S"
_busy: set[int] = set()


def media_list(item) -> list[str]:
    try:
        return json.loads(item["media_json"] or "[]")
    except Exception:
        return []


async def send_item(item_id: int) -> bool:
    """Reja elementini hozir yuboradi. True = yuborildi."""
    it = db.one("SELECT * FROM ch_plan WHERE id=?", (item_id,))
    if not it or it["status"] in ("sent", "cancelled") or item_id in _busy:
        return False
    ch = ch_data.get(it["channel_id"])
    if not ch or ch["status"] != "active":
        db.ex("UPDATE ch_plan SET status='failed', error=? WHERE id=?", ("Kanal faol emas", item_id))
        return False
    _busy.add(item_id)
    try:
        names = media_list(it)
        mtype = "video" if (it["media_type"] == "video" and len(names) == 1) else (it["media_type"] or None)
        text = ch_track.apply(it["text"] or "", ch, it["track_code"])
        ids = await ch_tg.send_post(ch["account_id"], ch["tg_id"], text, it["parse_mode"] or "none", names, mtype)
        db.ex("UPDATE ch_plan SET status='sent', msg_id=?, sent_at=?, error=NULL WHERE id=?", (ids[0] if ids else None, db.now(), item_id))
        if it["idea_id"]:
            db.ex("UPDATE ch_ideas SET status='used' WHERE id=?", (it["idea_id"],))
        ch_data.log_event(ch["id"], "post", f"reja #{item_id} yuborildi")
        return True
    except Exception as e:
        log.warning("Reja yuborilmadi (#%s): %s", item_id, e, exc_info=True)
        db.ex("UPDATE ch_plan SET status='failed', error=? WHERE id=?", (f"{type(e).__name__}: {e}"[:480], item_id))
        return False
    finally:
        _busy.discard(item_id)


async def tick():
    now = datetime.now()
    grace = max(0, int(db.get_setting("ch_plan_grace_h", 3) or 3))
    due = db.q("SELECT * FROM ch_plan WHERE status='scheduled' AND scheduled_at<=? ORDER BY scheduled_at LIMIT 10", (now.strftime(FMT),))
    for it in due:
        try:
            late = now - datetime.strptime(it["scheduled_at"], FMT)
        except (TypeError, ValueError):
            late = timedelta(0)
        if late > timedelta(hours=grace):
            db.ex("UPDATE ch_plan SET status='missed', error=? WHERE id=?",
                  (f"Vaqtida yuborilmadi (dastur o'chiq edi, {int(late.total_seconds() // 3600)} soat kechikdi)", it["id"]))
            continue
        await send_item(it["id"])
        await asyncio.sleep(3)


async def loop():
    await asyncio.sleep(60)
    while True:
        try:
            if ch_data.channel_account():
                await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ch_plan.loop")
        await asyncio.sleep(30)
