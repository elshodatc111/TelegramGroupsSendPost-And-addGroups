"""Guruhlardan avtomatik chiqish (kuniga bir marta tekshiradi)."""
import asyncio
import random

from . import db
from .analytics import leave_candidates, leave_settings
from .config import log
from .core import manager
from .limits import blacklist_add, event

MAX_PER_RUN = 5


async def leave_group(aid: int, tg_id: int, title: str, reason: str, auto: bool):
    await manager.get(aid).leave(tg_id)
    blacklist_add(aid, tg_id, title, "Chiqilgan: " + reason)
    db.ex("DELETE FROM groups WHERE account_id=? AND tg_id=?", (aid, tg_id))
    db.ex("DELETE FROM group_list_items WHERE tg_id=? AND list_id IN (SELECT id FROM group_lists WHERE account_id=?)", (tg_id, aid))
    db.ex("INSERT INTO leave_log(account_id,tg_id,title,reason,auto,ts) VALUES(?,?,?,?,?,?)",
          (aid, tg_id, title, reason, int(auto), db.now()))
    event(aid, "left", str(tg_id))


async def run_once():
    for a in db.q("SELECT * FROM accounts WHERE workspace='posting'"):
        uid = a["user_id"] or 1
        svc = manager.services.get(a["id"])
        if not svc or not svc.info or not leave_settings(uid)["on"]:
            continue
        if db.one("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('running','queued','waiting')", (a["id"],)):
            continue
        for c in leave_candidates(a["id"], uid)[:MAX_PER_RUN]:
            try:
                await leave_group(a["id"], c["tg_id"], c["title"], c["reason"], True)
                log.info("Avtomatik chiqildi: %s (%s)", c["title"], c["reason"])
            except Exception as e:
                log.warning("Chiqib bo'lmadi %s: %s", c["title"], type(e).__name__)
                if type(e).__name__ == "FloodWaitError":
                    break
            await asyncio.sleep(random.uniform(20, 45))


async def loop():
    await asyncio.sleep(300)
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("leaver")
        await asyncio.sleep(6 * 3600)
