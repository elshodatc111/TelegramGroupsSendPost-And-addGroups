"""Kanallarim: post kuzatuv havolalari (Cloudflare Worker) va bosishlar statistikasi.

Kanal sozlamasidagi o'zgarmas havola (lead_url) o'zgarmaydi. Har bir post uchun alohida qisqa kod beriladi:
    <worker>/c<kanal_id>/<kod>  ->  (Worker) lead_url ga yo'naltiradi va bosishni sanaydi.
Post yuborilayotganda matndagi lead_url shu kuzatuv havolasiga almashtiriladi.
"""
import asyncio
import secrets
import string

import httpx

from . import db
from .config import log

_ALPHA = string.ascii_lowercase + string.digits


class TrackError(Exception):
    pass


def base() -> str:
    return (db.get_setting("cf_worker_url") or "").strip().rstrip("/")


def key() -> str:
    return db.get_setting("cf_key") or ""


def enabled() -> bool:
    return bool(base() and key())


def ch_key(ch) -> str:
    return f"c{ch['id']}"


def tracked_url(ch, code: str) -> str:
    return f"{base()}/{ch_key(ch)}/{code}"


def new_code() -> str:
    for _ in range(20):
        code = "".join(secrets.choice(_ALPHA) for _ in range(6))
        if not db.one("SELECT 1 FROM ch_links WHERE code=?", (code,)):
            return code
    return secrets.token_hex(4)


def ensure_plan_code(plan_id: int, channel_id: int, label: str = "", idea_id=None, ab_id=None, variant=None) -> str:
    """Reja elementi uchun kod (yo'q bo'lsa yaratadi)."""
    it = db.one("SELECT track_code FROM ch_plan WHERE id=?", (plan_id,))
    if it and it["track_code"]:
        return it["track_code"]
    code = new_code()
    db.ex("INSERT INTO ch_links(channel_id,code,plan_id,idea_id,ab_id,variant,label,created_at) VALUES(?,?,?,?,?,?,?,?)",
          (channel_id, code, plan_id, idea_id, ab_id, variant, (label or "")[:250], db.now()))
    db.ex("UPDATE ch_plan SET track_code=? WHERE id=?", (code, plan_id))
    return code


def plan_link(item, ch) -> str | None:
    """Reja elementi uchun nusha olinadigan havola (Worker sozlangan bo'lsa)."""
    if not enabled() or not ch or not (ch["track_on"] if ch["track_on"] is not None else 1):
        return None
    code = item["track_code"] or ensure_plan_code(item["id"], item["channel_id"], item["title"], item["idea_id"],
                                                   item["ab_id"], item["ab_variant"])
    return tracked_url(ch, code)


def apply(text: str, ch, code: str | None) -> str:
    """Matndagi asl havolani (lead_url) kuzatuv havolasiga almashtiradi; {LINK} belgisini ham."""
    text = text or ""
    lead = (ch["lead_url"] or "").strip()
    link = tracked_url(ch, code) if (code and enabled() and (ch["track_on"] if ch["track_on"] is not None else 1)) else lead
    if "{LINK}" in text:
        text = text.replace("{LINK}", link or "")
    if lead and link and link != lead:
        text = text.replace(lead, link)
    return text


# ---------------------------------------------------------------- Worker bilan aloqa
async def _req(method, path, **kw):
    if not enabled():
        raise TrackError("Kuzatuv havolasi sozlanmagan (Kanallarim → Sozlamalar)")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c:
        try:
            r = await c.request(method, f"{base()}{path}", params={"key": key()}, **kw)
        except httpx.HTTPError as e:
            raise TrackError(f"Worker bilan aloqa yo'q: {type(e).__name__}")
    if r.status_code == 403:
        raise TrackError("Worker SECRET noto'g'ri")
    if r.status_code >= 400:
        raise TrackError(f"Worker xatosi {r.status_code}")
    return r.json()


async def ping() -> dict:
    return await _req("GET", "/_ping")


async def push_target(ch) -> bool:
    lead = (ch["lead_url"] or "").strip()
    if not lead.startswith("https://"):
        return False
    await _req("POST", "/_set", json={"ch": ch_key(ch), "to": lead})
    return True


async def push_all() -> int:
    n = 0
    for ch in db.q("SELECT * FROM ch_channels WHERE status='active'"):
        try:
            if await push_target(ch):
                n += 1
        except TrackError as e:
            log.warning("Worker maqsadi yuborilmadi (%s): %s", ch["title"], e)
            break
    return n


async def pull() -> int:
    """Worker'dan bosishlarni olib, bazaga yozadi. Qaytaradi: yangilangan qatorlar soni."""
    data = await _req("GET", "/_stats")
    ids = {f"c{r['id']}": r["id"] for r in db.q("SELECT id FROM ch_channels")}
    rows = []
    for chk, codes in (data or {}).items():
        cid = ids.get(chk)
        if not cid:
            continue
        for code, days in codes.items():
            for d, n in days.items():
                rows.append((cid, code, d, int(n)))
    if rows:
        db.many("INSERT OR REPLACE INTO ch_clicks(channel_id,code,day,n) VALUES(?,?,?,?)", rows)
    db.set_setting("cf_last_pull", db.now())
    return len(rows)


# ---------------------------------------------------------------- hisobotlar
def clicks_by_code(channel_id: int) -> dict[str, int]:
    return {r["code"]: int(r["n"] or 0) for r in
            db.q("SELECT code, SUM(n) n FROM ch_clicks WHERE channel_id=? GROUP BY code", (channel_id,))}


def clicks_daily(channel_id: int, days: int = 30) -> list[tuple[str, int]]:
    from datetime import datetime, timedelta
    cut = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return [(r["day"], int(r["n"] or 0)) for r in
            db.q("SELECT day, SUM(n) n FROM ch_clicks WHERE channel_id=? AND day>=? GROUP BY day ORDER BY day", (channel_id, cut))]


def top_links(channel_id: int, limit: int = 15) -> list[dict]:
    rows = db.q("SELECT l.code, l.label, l.variant, l.plan_id, p.status, p.sent_at, COALESCE(SUM(c.n),0) n "
                "FROM ch_links l LEFT JOIN ch_clicks c ON c.channel_id=l.channel_id AND c.code=l.code "
                "LEFT JOIN ch_plan p ON p.id=l.plan_id WHERE l.channel_id=? GROUP BY l.code, l.label, l.variant, l.plan_id, p.status, p.sent_at "
                "ORDER BY n DESC LIMIT ?", (channel_id, limit))
    return [dict(r) for r in rows]


async def loop():
    await asyncio.sleep(120)
    while True:
        try:
            if enabled() and db.one("SELECT 1 FROM ch_channels WHERE status='active' LIMIT 1"):
                await pull()
        except asyncio.CancelledError:
            raise
        except TrackError as e:
            log.info("Bosishlar olinmadi: %s", e)
        except Exception:
            log.exception("ch_track.loop")
        await asyncio.sleep(900)
