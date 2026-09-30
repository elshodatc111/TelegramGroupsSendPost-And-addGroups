"""Kanallarim: o'z kanal va raqobatchi kanallardan postlar/ko'rsatkichlarni yig'ish (faqat o'qish)."""
import asyncio
from datetime import datetime

from . import ch_data, ch_tg, db
from .config import log
from .core import manager

progress: dict[int, dict] = {}
_lock: dict[int, asyncio.Lock] = {}

FIRST_LIMIT = 300      # birinchi sinxronda nechta post
REFRESH_LIMIT = 100    # keyingi sinxronlarda (ko'rsatkichlarni yangilash uchun)

UPSERT = ("INSERT INTO ch_posts(channel_id,comp_id,msg_id,date,text,media,duration,views,forwards,reactions,replies,grouped_id,has_link,fetched_at,upd_at) "
          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(channel_id,comp_id,msg_id) DO UPDATE SET "
          "text=excluded.text, media=excluded.media, duration=excluded.duration, views=excluded.views, forwards=excluded.forwards, "
          "reactions=excluded.reactions, replies=excluded.replies, has_link=excluded.has_link, upd_at=excluded.upd_at")


def save_posts(cid: int, comp_id: int, rows: list[dict]) -> int:
    now = db.now()
    db.many(UPSERT, [(cid, comp_id, r["msg_id"], r["date"], r["text"], r["media"], r["duration"], r["views"], r["forwards"],
                      r["reactions"], r["replies"], r["grouped_id"], r["has_link"], now, now) for r in rows])
    return len(rows)


def _log_members(cid: int, comp_id: int, members):
    if members:
        db.ex("INSERT OR REPLACE INTO ch_members_log(channel_id,comp_id,day,members) VALUES(?,?,?,?)",
              (cid, comp_id, datetime.now().strftime("%Y-%m-%d"), int(members)))


async def sync_own(ch) -> int:
    aid, tg = ch["account_id"], ch["tg_id"]
    first = db.one("SELECT COUNT(*) c FROM ch_posts WHERE channel_id=? AND comp_id=0", (ch["id"],))["c"] == 0
    rows = await ch_tg.recent_posts(aid, tg, FIRST_LIMIT if first else REFRESH_LIMIT)
    n = save_posts(ch["id"], 0, rows)
    info = await ch_tg.channel_info(aid, tg)
    db.ex("UPDATE ch_channels SET title=?, username=?, about=?, members=?, synced_at=? WHERE id=?",
          (info["title"], info["username"], info["about"], info["members"], db.now(), ch["id"]))
    _log_members(ch["id"], 0, info["members"])
    return n


async def sync_competitor(ch, comp) -> int:
    aid = ch["account_id"]
    first = db.one("SELECT COUNT(*) c FROM ch_posts WHERE channel_id=? AND comp_id=?", (ch["id"], comp["id"]))["c"] == 0
    try:
        rows = await ch_tg.recent_posts(aid, comp["tg_id"], FIRST_LIMIT if first else REFRESH_LIMIT)
        n = save_posts(ch["id"], comp["id"], rows)
        info = await ch_tg.channel_info(aid, comp["tg_id"])
        db.ex("UPDATE ch_competitors SET title=?, username=?, about=?, members=?, last_sync=?, error=NULL WHERE id=?",
              (info["title"], info["username"], info["about"], info["members"], db.now(), comp["id"]))
        _log_members(ch["id"], comp["id"], info["members"])
        return n
    except ch_tg.TgError as e:
        db.ex("UPDATE ch_competitors SET error=?, last_sync=? WHERE id=?", (str(e)[:250], db.now(), comp["id"]))
        raise


async def sync_channel(cid: int, with_competitors: bool = True) -> dict:
    """Bitta kanal uchun to'liq sinxron. Bir vaqtda bitta sinxron ishlaydi."""
    ch = ch_data.get(cid)
    if not ch or ch["status"] != "active":
        return {"ok": False, "msg": "Kanal faol emas"}
    lock = _lock.setdefault(cid, asyncio.Lock())
    if lock.locked():
        return {"ok": False, "msg": "Sinxron allaqachon ketmoqda"}
    async with lock:
        comps = ch_data.competitors(cid) if with_competitors else []
        prog = progress[cid] = {"running": True, "done": 0, "total": 1 + len(comps), "msg": "O'z kanal yangilanmoqda", "errors": []}
        res = {"ok": True, "own": 0, "comp": 0, "errors": []}
        try:
            try:
                res["own"] = await sync_own(ch)
            except Exception as e:
                res["errors"].append(f"O'z kanal: {e}")
                log.warning("sync_own xatosi", exc_info=True)
            prog["done"] += 1
            for comp in comps:
                prog["msg"] = f"Raqobatchi: {comp['title']}"
                try:
                    res["comp"] += await sync_competitor(ch, comp)
                except Exception as e:
                    res["errors"].append(f"{comp['title']}: {e}")
                    log.warning("sync_competitor xatosi (%s)", comp["title"], exc_info=True)
                prog["done"] += 1
                await asyncio.sleep(1.5)            # Telegram'ni zo'riqtirmaslik uchun
        finally:
            prog.update(running=False, msg="Tugadi" if not res["errors"] else "Xatolar bilan tugadi", errors=res["errors"])
        ch_data.log_event(cid, "sync", f"o'z: {res['own']}, raqobatchi: {res['comp']}, xato: {len(res['errors'])}")
        return res


async def loop():
    """Har ~6 soatda faol kanallarni yangilaydi."""
    await asyncio.sleep(150)
    while True:
        try:
            hours = max(1, int(db.get_setting("ch_sync_hours", 6) or 6))
            acc = ch_data.channel_account()
            if acc:
                for ch in ch_data.channels():
                    last = ch["synced_at"]
                    due = True
                    if last:
                        try:
                            due = (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds() > hours * 3600
                        except ValueError:
                            due = True
                    if due:
                        await sync_channel(ch["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ch_collect.loop")
        await asyncio.sleep(1800)
