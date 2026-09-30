"""Guruh auditi: a'zolar soni va reklama imkoniyatini tekshiradi, mos kelmaganlardan chiqadi, qolganlarini mute qiladi."""
import asyncio
import random
from datetime import datetime, timedelta

from . import db
from .config import log
from .core import manager
from .leaver import leave_group

PROTECT_TAG = "saqlash"
progress: dict[int, dict] = {}


def get_cfg(aid: int) -> dict:
    r = db.one("SELECT * FROM audit_cfg WHERE account_id=?", (aid,))
    if not r:
        db.ex("INSERT INTO audit_cfg(account_id) VALUES(?)", (aid,))
        r = db.one("SELECT * FROM audit_cfg WHERE account_id=?", (aid,))
    return dict(r)


def protected_ids(aid: int) -> set[int]:
    return {r["tg_id"] for r in db.q("SELECT tg_id FROM group_tags WHERE account_id=? AND tag=?", (aid, PROTECT_TAG))}


def judge(g, cfg: dict) -> tuple[str, str]:
    """('leave'|'keep'|'unknown', sabab). Sabablar vergul bilan birlashtiriladi."""
    reasons = []
    m = g["members"]
    if m is not None and m < cfg["min_members"]:
        reasons.append(f"A'zolar kam ({m} < {cfg['min_members']})")
    if cfg["check_post"] and not g["can_post"]:
        reasons.append("Kanal: reklama yozib bo'lmaydi" if g["kind"] == "channel" else "Xabar yozish mumkin emas")
    if cfg["check_ads"] and g["ads_flag"]:
        reasons.append("Qoidada reklama taqiqlangan")
    if reasons:
        return "leave", "; ".join(reasons)
    if m is None:
        return "unknown", "A'zolar soni aniqlanmadi"
    return "keep", "Mos"


def evaluate(aid: int) -> dict:
    cfg = get_cfg(aid)
    prot = protected_ids(aid)
    now = db.now()
    n = {"leave": 0, "keep": 0, "unknown": 0, "protected": 0}
    for g in db.q("SELECT * FROM groups WHERE account_id=?", (aid,)):
        v, reason = judge(g, cfg)
        if g["tg_id"] in prot and v == "leave":
            n["protected"] += 1
            reason = "Himoyalangan: " + reason
            v = "keep"
        n[v if v in n else "keep"] += 1
        db.ex("UPDATE groups SET verdict=?, verdict_reason=?, audited_at=? WHERE account_id=? AND tg_id=?", (v, reason, now, aid, g["tg_id"]))
    return n


async def scan(aid: int, refresh_all: bool = False) -> dict:
    """Guruhlar ro'yxatini yangilaydi, qoida/a'zolar ma'lumotini to'ldiradi va hukm chiqaradi."""
    svc = manager.get(aid)
    prog = progress[aid] = {"running": True, "done": 0, "total": 0, "msg": "Guruhlar yuklanmoqda"}
    try:
        await svc.fetch_groups()
        stale = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        rows = db.q("SELECT tg_id FROM groups WHERE account_id=? AND (?=1 OR checked_at IS NULL OR checked_at<? OR members IS NULL)",
                    (aid, int(refresh_all), stale))
        prog.update(total=len(rows), msg="Guruh qoidalari tekshirilmoqda")
        for i, r in enumerate(rows):
            try:
                info = await svc.group_info(r["tg_id"])
                db.ex("UPDATE groups SET about=?, slowmode=?, no_media=?, no_links=?, ads_flag=?, checked_at=? WHERE account_id=? AND tg_id=?",
                      (info["about"], info["slowmode"], info["no_media"], info["no_links"], info["ads_flag"], db.now(), aid, r["tg_id"]))
                if info.get("members"):
                    db.ex("UPDATE groups SET members=? WHERE account_id=? AND tg_id=?", (info["members"], aid, r["tg_id"]))
            except Exception as e:
                log.info("Audit: %s tekshirilmadi: %s", r["tg_id"], type(e).__name__)
                if type(e).__name__ == "FloodWaitError":
                    prog["msg"] = "Telegram kutishni so'radi, keyinroq davom eting"
                    break
            prog["done"] = i + 1
            await asyncio.sleep(random.uniform(1.0, 2.2))
        res = evaluate(aid)
        db.ex("UPDATE audit_cfg SET last_scan=? WHERE account_id=?", (db.now(), aid))
        prog["result"] = res
        return res
    finally:
        prog.update(running=False)


def leave_candidates(aid: int) -> list:
    prot = protected_ids(aid)
    return [g for g in db.q("SELECT * FROM groups WHERE account_id=? AND verdict='leave' ORDER BY members", (aid,)) if g["tg_id"] not in prot]


def left_today(aid: int) -> int:
    t0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    return db.one("SELECT COUNT(*) c FROM leave_log WHERE account_id=? AND reason LIKE 'Audit:%' AND ts>=?", (aid, t0))["c"]


async def run_leave(aid: int, only: list[int] | None = None, ignore_cap: bool = False) -> dict:
    """Mos kelmagan guruhlardan chiqadi (kunlik chegara bilan). Faol yuborish bor bo'lsa, kutadi."""
    cfg = get_cfg(aid)
    if db.one("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('running','queued','waiting')", (aid,)):
        return {"left": 0, "failed": 0, "blocked": "Faol yuborish ketmoqda, u tugagach qayta urinib ko'ring"}
    cands = leave_candidates(aid)
    if only is not None:
        cands = [c for c in cands if c["tg_id"] in set(only)]
    room = len(cands) if ignore_cap else max(0, cfg["max_leave"] - left_today(aid))
    ok = fail = 0
    prog = progress.setdefault(aid, {})
    prog.update(running=True, done=0, total=min(room, len(cands)), msg="Guruhlardan chiqilmoqda")
    try:
        for c in cands[:room]:
            try:
                await leave_group(aid, c["tg_id"], c["title"], "Audit: " + (c["verdict_reason"] or ""), only is None)
                ok += 1
            except Exception as e:
                fail += 1
                log.warning("Audit: chiqib bo'lmadi %s: %s", c["title"], type(e).__name__)
                if type(e).__name__ == "FloodWaitError":
                    break
            prog["done"] = ok + fail
            await asyncio.sleep(random.uniform(cfg["min_delay"], max(cfg["min_delay"], cfg["max_delay"])))
    finally:
        prog.update(running=False)
    return {"left": ok, "failed": fail, "remaining": max(0, len(cands) - ok)}


async def mute_pending(aid: int, limit: int = 400) -> dict:
    """Hali mute qilinmagan barcha guruhlarni (bildirishnomasiz) holatga o'tkazadi."""
    svc = manager.get(aid)
    ok = fail = 0
    prog = progress.setdefault(aid, {})
    rows = db.q("SELECT tg_id FROM groups WHERE account_id=? AND COALESCE(muted,0)=0 LIMIT ?", (aid, limit))
    prog.update(running=True, done=0, total=len(rows), msg="Bildirishnomalar o'chirilmoqda")
    try:
        for r in rows:
            try:
                await svc.mute(r["tg_id"])
                db.ex("UPDATE groups SET muted=1 WHERE account_id=? AND tg_id=?", (aid, r["tg_id"]))
                ok += 1
            except Exception as e:
                fail += 1
                log.info("Mute xatosi %s: %s", r["tg_id"], type(e).__name__)
                if type(e).__name__ == "FloodWaitError":
                    break
            prog["done"] = ok + fail
            await asyncio.sleep(random.uniform(0.8, 1.8))
    finally:
        prog.update(running=False)
    return {"muted": ok, "failed": fail}


async def run_all(aid: int) -> dict:
    """Bir tugma bilan: tahlil -> chiqish -> mute."""
    res = {"scan": await scan(aid)}
    res["leave"] = await run_leave(aid)
    if get_cfg(aid)["mute_all"]:
        res["mute"] = await mute_pending(aid)
    return res


async def tick():
    for a in db.q("SELECT a.* FROM accounts a JOIN audit_cfg c ON c.account_id=a.id WHERE c.auto=1"):
        aid = a["id"]
        svc = manager.services.get(aid)
        if not svc or not svc.info or progress.get(aid, {}).get("running"):
            continue
        cfg = get_cfg(aid)
        last = cfg["last_scan"]
        if not last or datetime.strptime(last, "%Y-%m-%d %H:%M:%S") < datetime.now() - timedelta(hours=12):
            await scan(aid)
        await run_leave(aid)
        if cfg["mute_all"]:
            await mute_pending(aid)


async def loop():
    await asyncio.sleep(420)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("auditor.tick")
        await asyncio.sleep(1800)
