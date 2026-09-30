"""Kanallarim: Telegram amallari (faqat o'qish + o'z kanaliga post qo'yish).

Muhim: bu modul hech qaysi kanal yoki guruhga QO'SHILMAYDI va raqobatchi kanallarga hech narsa yubormaydi.
Raqobatchilar — akkaunt allaqachon a'zo bo'lgan (telefondan qo'shilgan) kanallar ro'yxatidan tanlanadi.
"""
import asyncio
import json
import re
import time
from datetime import datetime

from telethon import errors
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import (Channel, DocumentAttributeAudio, DocumentAttributeVideo)

from .config import log
from .core import manager

_LINK = re.compile(r"https?://|t\.me/|@\w{4,}")


class TgError(Exception):
    pass


async def client(aid: int):
    svc = manager.get(aid)
    c = await svc._get_client()
    if not await c.is_user_authorized():
        raise TgError("Akkaunt ulanmagan. Kanallarim → Akkaunt bo'limida qayta ulang.")
    return c


def _rights(e) -> dict:
    r = getattr(e, "admin_rights", None)
    if getattr(e, "creator", False):
        return {"creator": True, "post": True, "edit": True, "delete": True, "invite": True, "stats": True}
    if not r:
        return {}
    return {"post": bool(r.post_messages), "edit": bool(r.edit_messages), "delete": bool(r.delete_messages),
            "invite": bool(r.invite_users), "manage": bool(getattr(r, "change_info", False))}


async def dialogs(aid: int):
    """Akkaunt dialoglaridan kanallarni (broadcast) ikki guruhga ajratadi: admin bo'lganlar va oddiy a'zo bo'lganlar."""
    c = await client(aid)
    admin, member = [], []
    async for d in c.iter_dialogs():
        e = d.entity
        if not (isinstance(e, Channel) and e.broadcast) or getattr(e, "left", False):
            continue
        info = {"tg_id": d.id, "title": d.name or "Nomsiz", "username": getattr(e, "username", None),
                "members": getattr(e, "participants_count", None), "is_creator": bool(e.creator), "rights": _rights(e),
                "private": not getattr(e, "username", None)}
        (admin if (e.creator or e.admin_rights) else member).append(info)
    admin.sort(key=lambda x: x["title"].lower())
    member.sort(key=lambda x: x["title"].lower())
    return admin, member


async def _entity(c, tg_id: int):
    try:
        return await c.get_entity(tg_id)
    except (ValueError, TypeError, KeyError):
        await c.get_dialogs()                      # sessiya keshini yangilash
        return await c.get_entity(tg_id)


async def channel_info(aid: int, tg_id: int) -> dict:
    c = await client(aid)
    e = await _entity(c, tg_id)
    full = await c(GetFullChannelRequest(e))
    return {"title": e.title, "username": getattr(e, "username", None), "about": (full.full_chat.about or "")[:1500],
            "members": getattr(full.full_chat, "participants_count", None),
            "linked_chat": getattr(full.full_chat, "linked_chat_id", None)}


def _media_kind(m):
    if m.photo:
        return "photo", 0
    doc = m.document
    if doc is not None:
        for a in doc.attributes:
            if isinstance(a, DocumentAttributeVideo):
                return "video", int(a.duration or 0)
            if isinstance(a, DocumentAttributeAudio):
                return ("voice" if a.voice else "audio"), int(a.duration or 0)
        return "doc", 0
    if getattr(m, "poll", None):
        return "poll", 0
    return "text", 0


def _row(m) -> dict:
    kind, dur = _media_kind(m)
    react = sum(r.count for r in m.reactions.results) if getattr(m, "reactions", None) and m.reactions.results else 0
    repl = m.replies.replies if getattr(m, "replies", None) else 0
    text = m.message or ""
    return {"msg_id": m.id, "date": m.date.astimezone().strftime("%Y-%m-%d %H:%M:%S") if m.date else None, "text": text,
            "media": kind, "duration": dur, "views": m.views or 0, "forwards": m.forwards or 0, "reactions": react,
            "replies": repl or 0, "grouped_id": m.grouped_id, "has_link": int(bool(_LINK.search(text)))}


def merge_albums(rows: list[dict]) -> list[dict]:
    """Albomdagi xabarlar bitta postga birlashtiriladi (matn, eng katta ko'rishlar, media='album')."""
    out, groups = [], {}
    for r in rows:
        g = r["grouped_id"]
        if not g:
            out.append(r)
            continue
        if g not in groups:
            groups[g] = dict(r, media="album", msg_id=r["msg_id"], _n=1)
            out.append(groups[g])
        else:
            a = groups[g]
            a["_n"] += 1
            a["msg_id"] = min(a["msg_id"], r["msg_id"])
            a["text"] = a["text"] or r["text"]
            a["has_link"] = a["has_link"] or r["has_link"]
            for k in ("views", "forwards", "reactions", "replies"):
                a[k] = max(a[k], r[k])
            if r["media"] == "video" and a["media"] == "album":
                a["duration"] = max(a["duration"], r["duration"])
    for a in out:
        a.pop("_n", None)
    return out


async def recent_posts(aid: int, tg_id: int, limit: int = 100) -> list[dict]:
    c = await client(aid)
    e = await _entity(c, tg_id)
    rows = []
    try:
        async for m in c.iter_messages(e, limit=limit):
            if m is None or type(m).__name__ in ("MessageService", "MessageEmpty"):
                continue
            rows.append(_row(m))
    except errors.FloodWaitError as fw:
        log.warning("FloodWait %ss (kanal %s)", fw.seconds, tg_id)
        if fw.seconds > 300:
            raise TgError(f"Telegram cheklovi: {fw.seconds} soniya kuting")
        await asyncio.sleep(fw.seconds + 1)
    except (errors.ChannelPrivateError, errors.ChannelInvalidError, errors.UsernameInvalidError):
        raise TgError("Kanalga kirish yo'q (yopilgan yoki akkaunt undan chiqqan)")
    return merge_albums(rows)


async def download_video(aid: int, tg_id: int, msg_id: int, dest: str, max_mb: int = 120) -> str | None:
    """Videoni vaqtinchalik papkaga yuklaydi (transkripsiya uchun). Juda katta bo'lsa None."""
    c = await client(aid)
    e = await _entity(c, tg_id)
    m = await c.get_messages(e, ids=msg_id)
    if not m or not (m.video or m.document or m.voice or m.audio):
        return None
    size = getattr(getattr(m, "document", None), "size", 0) or 0
    if size > max_mb * 1048576:
        return None
    return await c.download_media(m, file=dest)


async def own_stats(aid: int, tg_id: int) -> dict | None:
    """Telegram'ning rasmiy statistikasi (faqat 500+ obunachili kanallarda ochiladi). Yo'q bo'lsa None."""
    try:
        c = await client(aid)
        e = await _entity(c, tg_id)
        from telethon.tl.functions.stats import GetBroadcastStatsRequest
        st = await c(GetBroadcastStatsRequest(e))
        def pair(x):
            return {"cur": getattr(x, "current", None), "prev": getattr(x, "previous", None)}
        return {"followers": pair(st.followers), "views_per_post": pair(st.views_per_post),
                "shares_per_post": pair(st.shares_per_post), "enabled_notifications": getattr(st.enabled_notifications, "part", None)}
    except Exception as ex:
        log.info("Kanal statistikasi mavjud emas (%s): %s", tg_id, type(ex).__name__)
        return None


async def send_post(aid: int, tg_id: int, text: str, parse_mode: str, media_names: list[str], media_type: str | None) -> list[int]:
    """O'z kanalimizga post qo'yadi (Group Post'dagi send() bilan bir xil: rasm/video/albom, uzun matn alohida)."""
    await client(aid)
    return await manager.get(aid).send(tg_id, text, parse_mode, media_names, media_type)


# ---------------------------------------------------------------- rasmiy statistika (500+ obunachi)
async def _stats_call(c, request):
    """Statistika boshqa DC'da turishi mumkin: StatsMigrateError bo'lsa o'sha DC orqali so'raydi."""
    from telethon.errors import StatsMigrateError
    try:
        return await c(request)
    except StatsMigrateError as e:
        sender = await c._borrow_exported_sender(e.new_dc)
        try:
            return await sender.send(request)
        finally:
            await c._return_exported_sender(sender)


GRAPHS = ("growth_graph", "followers_graph", "mute_graph", "top_hours_graph", "interactions_graph",
          "views_by_source_graph", "new_followers_by_source_graph", "languages_graph")


async def official_graphs(aid: int, tg_id: int) -> dict:
    """Telegram rasmiy statistikasi: grafiklar (JSON) va xulosa. Mavjud bo'lmasa TgError."""
    from telethon.tl.functions.stats import GetBroadcastStatsRequest, LoadAsyncGraphRequest
    from telethon.tl.types import StatsGraph, StatsGraphAsync
    c = await client(aid)
    e = await _entity(c, tg_id)
    try:
        st = await _stats_call(c, GetBroadcastStatsRequest(e))
    except errors.ChatAdminRequiredError:
        raise TgError("Statistikani ko'rish uchun akkauntga admin huquqi (statistika) kerak")
    except errors.RPCError as ex:
        raise TgError(f"Rasmiy statistika mavjud emas ({type(ex).__name__}). Kanalda 500+ obunachi bo'lishi kerak")

    def pair(x):
        return {"cur": getattr(x, "current", None), "prev": getattr(x, "previous", None)} if x is not None else None
    out = {"_summary": {"followers": pair(getattr(st, "followers", None)), "views_per_post": pair(getattr(st, "views_per_post", None)),
                        "shares_per_post": pair(getattr(st, "shares_per_post", None)),
                        "reactions_per_post": pair(getattr(st, "reactions_per_post", None)),
                        "notifications": {"part": getattr(getattr(st, "enabled_notifications", None), "part", None),
                                          "total": getattr(getattr(st, "enabled_notifications", None), "total", None)}}}
    for name in GRAPHS:
        g = getattr(st, name, None)
        try:
            if isinstance(g, StatsGraphAsync):
                g = await _stats_call(c, LoadAsyncGraphRequest(token=g.token))
            if isinstance(g, StatsGraph):
                out[name] = json.loads(g.json.data)
        except Exception as ex:
            log.info("Grafik olinmadi %s: %s", name, type(ex).__name__)
    return out


# ---------------------------------------------------------------- auditoriya (faqat yig'ma raqamlar, ism saqlanmaydi)
def _bucket(u) -> str:
    from datetime import datetime, timezone
    from telethon.tl.types import (UserStatusLastMonth, UserStatusLastWeek, UserStatusOffline, UserStatusOnline,
                                   UserStatusRecently)
    st = u.status
    if isinstance(st, (UserStatusOnline, UserStatusRecently)):
        return "online"
    if isinstance(st, UserStatusOffline):
        days = (datetime.now(timezone.utc) - st.was_online).days
        return "online" if days < 1 else "week" if days < 7 else "month" if days < 30 else "long"
    if isinstance(st, UserStatusLastWeek):
        return "week"
    if isinstance(st, UserStatusLastMonth):
        return "month"
    return "long"


async def audience_sample(aid: int, tg_id: int, max_seconds: int = 240) -> dict:
    """Obunachilarni sanab, yig'ma raqam qaytaradi (premium, bot, faollik toifalari). Shaxsiy ma'lumot saqlanmaydi."""
    c = await client(aid)
    e = await _entity(c, tg_id)
    try:
        full = await c(GetFullChannelRequest(e))
    except errors.RPCError as ex:
        raise TgError(f"Kanal ma'lumoti olinmadi: {type(ex).__name__}")
    total = getattr(full.full_chat, "participants_count", 0) or 0
    cnt = {"online": 0, "week": 0, "month": 0, "long": 0}
    prem = bots = dele = n = 0
    deadline = time.monotonic() + max_seconds
    try:
        async for u in c.iter_participants(e, limit=10000 if total <= 10000 else 1000, aggressive=total > 200):
            n += 1
            if getattr(u, "deleted", False):
                dele += 1
                continue
            if getattr(u, "bot", False):
                bots += 1
                continue
            prem += 1 if getattr(u, "premium", False) else 0
            cnt[_bucket(u)] += 1
            if n % 200 == 0 and time.monotonic() > deadline:
                break
    except errors.ChatAdminRequiredError:
        raise TgError("Obunachilar ro'yxatini ko'rish uchun admin huquqi kerak")
    except errors.FloodWaitError as fw:
        log.warning("Auditoriya: FloodWait %ss, qisman natija bilan to'xtadi", fw.seconds)
    except errors.RPCError as ex:
        if n == 0:
            raise TgError(f"Obunachilar ro'yxati olinmadi: {type(ex).__name__}")
    if n == 0:
        raise TgError("Obunachilar ro'yxati bo'sh qaytdi (Telegram cheklovi)")
    return {"total": total, "sampled": n, "premium": prem, "bots": bots, "deleted": dele,
            "a_online": cnt["online"], "a_week": cnt["week"], "a_month": cnt["month"], "a_long": cnt["long"]}


# ---------------------------------------------------------------- izohlar
async def comments_for(aid: int, tg_id: int, msg_ids: list[int], per_post: int = 50) -> list[dict]:
    """Postlar ostidagi izohlar matni (muallif ma'lumotisiz). Izoh yoqilmagan postlar o'tkazib yuboriladi."""
    c = await client(aid)
    e = await _entity(c, tg_id)
    out = []
    for mid in msg_ids:
        try:
            async for m in c.iter_messages(e, reply_to=mid, limit=per_post):
                if m and m.message:
                    out.append({"post": mid, "id": m.id, "date": m.date.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                                "text": m.message[:500]})
        except (errors.MsgIdInvalidError, errors.MessageIdInvalidError):
            continue
        except errors.FloodWaitError as fw:
            if fw.seconds > 120:
                break
            await asyncio.sleep(fw.seconds + 1)
        except errors.RPCError:
            continue
        await asyncio.sleep(0.6)
    return out
