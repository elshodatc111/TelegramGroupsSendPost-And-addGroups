"""Bitta Telegram akkaunt bilan ishlash (Telethon, user account / MTProto)."""
import asyncio
import json
import re
from datetime import datetime

from telethon import TelegramClient, errors, events
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.account import GetNotifySettingsRequest, UpdateNotifySettingsRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, GetFullChatRequest, ImportChatInviteRequest
from telethon.tl.types import (Channel, Chat, ChatInviteAlready, DocumentAttributeVideo, InputNotifyPeer, InputPeerNotifySettings, User)

from . import db
from .config import CAPTION_LIMIT, MEDIA_DIR, SESSION_DIR, log


class NotAGroup(Exception):
    """Username guruh/kanalga emas, foydalanuvchi yoki botga tegishli."""


_REFRESH_ERRORS = ("FileReferenceExpiredError", "FileReferenceInvalidError", "MediaInvalidError",
                   "FileIdInvalidError", "MediaEmptyError", "WebpageMediaEmptyError")

_ADS_PATTERNS = [
    re.compile(r"(reklama|реклама|ads?|advertis\w*|promo\w*|spam|savdo)\s*(taqiqlan\w*|mumkin emas|man etil\w*|yo'?q|"
               r"запрещ\w*|не допуска\w*|not allowed|forbidden|prohibited|banned|is not permitted)", re.I),
    re.compile(r"(no|без|yo'?q|yoq|taqiqlanadi|запрет)\s*(reklama|реклама|ads?|advertis\w*|spam|promo\w*)", re.I),
]


def video_attributes(path: str):
    """Video davomiyligi/o'lchamini aniqlaydi (Telegram to'g'ri video sifatida ko'rsatishi uchun)."""
    try:
        from hachoir.metadata import extractMetadata
        from hachoir.parser import createParser
        parser = createParser(path)
        if not parser:
            return None
        with parser:
            md = extractMetadata(parser)
        if not md:
            return None
        dur = int(md.get("duration").total_seconds()) if md.has("duration") else 0
        w = int(md.get("width")) if md.has("width") else 0
        h = int(md.get("height")) if md.has("height") else 0
        return DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)
    except Exception:
        log.warning("Video metadata o'qib bo'lmadi: %s", path, exc_info=True)
        return None


def ads_prohibited(*texts: str) -> bool:
    """Tavsif / mahkamlangan xabar / qoida matnida reklama taqiqi borligini aniqlaydi."""
    return any(t and any(p.search(t) for p in _ADS_PATTERNS) for t in texts)


async def _pinned_text(c, e, fc) -> str:
    """Guruhning mahkamlangan (pinned) xabari matni: qoidalar ko'pincha shu yerda yoziladi."""
    pid = getattr(fc, "pinned_msg_id", None)
    if not pid:
        return ""
    try:
        m = await c.get_messages(e, ids=pid)
        return (getattr(m, "message", "") or "")[:1500]
    except Exception:
        return ""


class TelegramService:
    def __init__(self, account_id: int, session_name: str):
        self.account_id = account_id
        self.session_path = str(SESSION_DIR / session_name)
        self.client: TelegramClient | None = None
        self._creds = None
        self._phone = None
        self._code_hash = None
        self._handlers_on = False
        self.info: dict | None = None
        self._media_cache: dict = {}

    def _api(self):
        r = db.one("SELECT user_id FROM accounts WHERE id=?", (self.account_id,))
        uid = (r["user_id"] if r else None) or 1
        return db.uget(uid, "api_id"), db.uget(uid, "api_hash")

    # ---------- ulanish ----------
    async def _get_client(self) -> TelegramClient:
        api_id, api_hash = self._api()
        if not api_id or not api_hash:
            raise RuntimeError("API ID va API HASH hali kiritilmagan (Sozlamalar)")
        creds = (api_id, api_hash)
        if self.client is not None and self._creds != creds:
            await self.client.disconnect()
            self.client, self._handlers_on = None, False
        if self.client is None:
            self.client = TelegramClient(self.session_path, int(api_id), api_hash)
            self._creds = creds
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def shutdown(self):
        if self.client and self.client.is_connected():
            await self.client.disconnect()

    async def status(self) -> dict:
        if not all(self._api()):
            self.info = None
            return {"configured": False, "authorized": False}
        try:
            c = await self._get_client()
            if await c.is_user_authorized():
                me = await c.get_me()
                name = " ".join(x for x in (me.first_name, me.last_name) if x)
                self.info = {"name": name or (me.username or "Akkaunt"), "phone": me.phone, "username": me.username}
                db.ex("UPDATE accounts SET phone=?, username=? WHERE id=?", (me.phone, me.username, self.account_id))
                self._register(c)
                return {"configured": True, "authorized": True, **self.info}
            self.info = None
            return {"configured": True, "authorized": False}
        except Exception as e:
            log.warning("status xatosi (akkaunt %s): %s", self.account_id, e)
            return {"configured": True, "authorized": False, "error": str(e)}

    # ---------- kirish ----------
    async def send_code(self, phone: str):
        c = await self._get_client()
        r = await c.send_code_request(phone)
        self._phone, self._code_hash = phone, r.phone_code_hash

    async def sign_in_code(self, code: str) -> str:
        c = await self._get_client()
        try:
            await c.sign_in(phone=self._phone, code=code, phone_code_hash=self._code_hash)
            return "ok"
        except errors.SessionPasswordNeededError:
            return "password"

    async def sign_in_password(self, password: str):
        c = await self._get_client()
        await c.sign_in(password=password)

    async def logout(self):
        c = await self._get_client()
        try:
            await c.log_out()
        finally:
            self.client, self.info, self._handlers_on = None, None, False

    # ---------- inbox (kiruvchi xabarlar) ----------
    def _register(self, client):
        if self._handlers_on:
            return
        self._handlers_on = True
        ws = db.one("SELECT workspace FROM accounts WHERE id=?", (self.account_id,))
        if ws and ws["workspace"] == "channels":
            return          # Telegram SMM akkaunti Telegram Guruhlar inbox'iga yozmaydi
        aid = self.account_id

        @client.on(events.NewMessage(incoming=True))
        async def _incoming(event):
            try:
                msg = event.message
                if not msg or not (msg.message or "").strip():
                    return
                kind, chat_title = None, None
                if event.is_private:
                    sender = await event.get_sender()
                    if getattr(sender, "bot", False) or isinstance(sender, Channel):
                        return
                    kind, chat_title = "private", None
                elif msg.is_reply:
                    replied = await msg.get_reply_message()
                    if replied is not None and replied.out:
                        kind = "reply"
                    else:
                        return
                elif getattr(msg, "mentioned", False):
                    kind = "mention"
                else:
                    return
                sender = await event.get_sender()
                chat = await event.get_chat()
                sname = " ".join(x for x in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if x) \
                    or getattr(sender, "title", None) or "Noma'lum"
                db.ex("INSERT OR IGNORE INTO inbox(account_id,chat_id,chat_title,sender_id,sender_name,sender_username,"
                      "text,msg_id,is_private,kind,date) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (aid, event.chat_id, getattr(chat, "title", None), getattr(sender, "id", None), sname,
                       getattr(sender, "username", None), msg.message[:4000], msg.id, int(bool(event.is_private)), kind,
                       msg.date.astimezone().strftime("%Y-%m-%d %H:%M:%S") if msg.date else db.now()))
            except Exception:
                log.warning("inbox handler xatosi", exc_info=True)

    async def reply(self, chat_id: int, text: str, reply_to: int | None):
        c = await self._get_client()
        entity = await c.get_input_entity(chat_id)
        await c.send_message(entity, text, reply_to=reply_to)

    # ---------- guruhlar ----------
    async def fetch_groups(self) -> int:
        c = await self._get_client()
        stamp, day = db.now(), datetime.now().strftime("%Y-%m-%d")
        rows = []
        async for d in c.iter_dialogs():
            e = d.entity
            can, kind = True, "group"
            if isinstance(e, Channel):
                if getattr(e, "left", False):
                    continue
                if e.broadcast:
                    # Kanallar ham saqlanadi (yozish huquqi bo'lmasa can_post=0): Guruh auditi ulardan chiqib keta olishi uchun
                    kind = "channel"
                    can = bool(e.creator or (e.admin_rights and e.admin_rights.post_messages))
                else:
                    kind = "supergroup"
                    br = e.default_banned_rights
                    mine = getattr(e, "banned_rights", None)
                    can = bool(e.creator or e.admin_rights or not ((br and br.send_messages) or (mine and mine.send_messages)))
            elif isinstance(e, Chat):
                if getattr(e, "deactivated", False) or getattr(e, "left", False):
                    continue
                br = e.default_banned_rights
                can = bool(e.creator or e.admin_rights or not (br and br.send_messages))
            else:
                continue
            rows.append((d.id, d.name or "Nomsiz", getattr(e, "username", None), kind,
                         getattr(e, "participants_count", None), int(can)))
        aid = self.account_id
        for tg_id, title, uname, kind, members, can in rows:
            db.ex("INSERT INTO groups(account_id,tg_id,title,username,kind,members,can_post,synced_at) "
                  "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_id,tg_id) DO UPDATE SET title=excluded.title,"
                  "username=excluded.username,kind=excluded.kind,members=excluded.members,can_post=excluded.can_post,"
                  "synced_at=excluded.synced_at", (aid, tg_id, title, uname, kind, members, can, stamp))
            if members:
                db.ex("INSERT OR REPLACE INTO group_members_log(account_id,tg_id,day,members) VALUES(?,?,?,?)",
                      (aid, tg_id, day, members))
        db.ex("DELETE FROM groups WHERE account_id=? AND synced_at!=?", (aid, stamp))
        db.ex("DELETE FROM group_list_items WHERE list_id IN (SELECT id FROM group_lists WHERE account_id=?) "
              "AND tg_id NOT IN (SELECT tg_id FROM groups WHERE account_id=?)", (aid, aid))
        return len(rows)

    async def group_info(self, tg_id: int) -> dict:
        """Guruh qoidalari: tavsif, sekin rejim, media/havola cheklovlari, reklama taqiqi."""
        c = await self._get_client()
        e = await c.get_entity(tg_id)
        about, slow, members, pinned = "", 0, None, ""
        rights = getattr(e, "default_banned_rights", None)
        if isinstance(e, Channel):
            full = await c(GetFullChannelRequest(e))
            about = full.full_chat.about or ""
            pinned = await _pinned_text(c, e, full.full_chat)
            slow = getattr(full.full_chat, "slowmode_seconds", 0) or 0
            members = getattr(full.full_chat, "participants_count", None)
        elif isinstance(e, Chat):
            full = await c(GetFullChatRequest(e.id))
            about = full.full_chat.about or ""
            members = len(getattr(full.full_chat.participants, "participants", []) or []) or None
        no_media = bool(rights and (getattr(rights, "send_media", False) or
                                    (getattr(rights, "send_photos", False) and getattr(rights, "send_videos", False))))
        no_links = bool(rights and getattr(rights, "embed_links", False))
        return {"members": members, "about": about[:600], "slowmode": slow, "no_media": int(no_media), "no_links": int(no_links),
                "ads_flag": int(ads_prohibited(about, pinned))}

    async def search_public(self, q: str, strict: bool = False, min_members: int = 0, check_ads: bool = False,
                            stats: dict | None = None) -> list[dict]:
        """Ommaviy guruh/kanallarni qidiradi. strict=True: kanallar, yozib bo'lmaydigan, a'zosi kam va reklama taqiqlangan
        guruhlar natijadan chiqarib tashlanadi (stats['hidden'] da sabablar bo'yicha soni)."""
        c = await self._get_client()
        r = await c(SearchRequest(q=q, limit=40))
        out, hidden = [], {}

        def hide(why):
            hidden[why] = hidden.get(why, 0) + 1

        deep = 0
        for ch in r.chats:
            if not (isinstance(ch, Channel) and ch.username):
                continue
            cnt = getattr(ch, "participants_count", None)
            if strict:
                if ch.broadcast:
                    hide("kanal (reklama yozib bo'lmaydi)")
                    continue
                br = ch.default_banned_rights
                if br and br.send_messages:
                    hide("yozish taqiqlangan")
                    continue
                if cnt is not None and cnt < min_members:
                    hide(f"a'zolar {min_members} dan kam")
                    continue
                if check_ads and deep < 30:
                    deep += 1
                    try:
                        full = await c(GetFullChannelRequest(ch))
                        fc = full.full_chat
                        cnt = getattr(fc, "participants_count", cnt)
                        if cnt is not None and cnt < min_members:
                            hide(f"a'zolar {min_members} dan kam")
                            continue
                        if ads_prohibited(fc.about or "", await _pinned_text(c, ch, fc)):
                            hide("reklama taqiqlangan")
                            continue
                    except errors.FloodWaitError:
                        check_ads = False
                    except Exception:
                        pass
                    await asyncio.sleep(0.3)
            out.append({"username": ch.username, "title": ch.title, "members": cnt,
                        "kind": "channel" if ch.broadcast else "group", "joined": not getattr(ch, "left", True)})
        if stats is not None:
            stats["hidden"] = hidden
        return out

    async def check_target(self, kind: str, key: str, min_members: int = 0, check_ads: bool = True) -> dict:
        """A'zo bo'lishdan oldin: reklama yuborish mumkin bo'lgan guruhmi? {'ok': bool, 'reason': str}"""
        c = await self._get_client()
        if kind == "invite":
            info = await c(CheckChatInviteRequest(key))
            if isinstance(info, ChatInviteAlready):
                return {"ok": True, "reason": ""}
            if getattr(info, "broadcast", False):
                return {"ok": False, "reason": "Kanal: reklama yozib bo'lmaydi"}
            cnt = getattr(info, "participants_count", None)
            if cnt is not None and cnt < min_members:
                return {"ok": False, "reason": f"A'zolar kam ({cnt} < {min_members})"}
            if check_ads and ads_prohibited(getattr(info, "about", "") or ""):
                return {"ok": False, "reason": "Guruhda reklama taqiqlangan"}
            return {"ok": True, "reason": ""}
        e = await c.get_entity(key)
        if isinstance(e, User):
            return {"ok": True, "reason": ""}      # NotAGroup keyin join() da aniqlanadi
        if not isinstance(e, Channel):
            return {"ok": True, "reason": ""}
        if e.left is False:
            return {"ok": True, "reason": ""}      # allaqachon a'zo
        if e.broadcast:
            return {"ok": False, "reason": "Kanal: reklama yozib bo'lmaydi"}
        br = e.default_banned_rights
        if br and br.send_messages:
            return {"ok": False, "reason": "Guruhda a'zolar xabar yoza olmaydi"}
        full = await c(GetFullChannelRequest(e))
        fc = full.full_chat
        cnt = getattr(fc, "participants_count", None)
        if cnt is not None and cnt < min_members:
            return {"ok": False, "reason": f"A'zolar kam ({cnt} < {min_members})"}
        if check_ads and ads_prohibited(fc.about or "", await _pinned_text(c, e, fc)):
            return {"ok": False, "reason": "Guruhda reklama taqiqlangan"}
        return {"ok": True, "reason": ""}

    # ---------- a'zo bo'lish ----------
    async def join(self, kind: str, key: str) -> tuple[str, str | None]:
        c = await self._get_client()
        if kind == "invite":
            info = await c(CheckChatInviteRequest(key))
            if isinstance(info, ChatInviteAlready):
                return "already", getattr(info.chat, "title", None)
            title = getattr(info, "title", None)
            await c(ImportChatInviteRequest(key))
            return "joined", title
        entity = await c.get_entity(key)
        if isinstance(entity, User):
            raise NotAGroup("Bu foydalanuvchi yoki bot, guruh/kanal emas")
        title = getattr(entity, "title", None)
        if isinstance(entity, Channel):
            if entity.left is False:
                return "already", title
            await c(JoinChannelRequest(entity))
            return "joined", title
        raise NotAGroup("Username orqali qo'shilib bo'lmaydigan chat turi")

    # ---------- yuborish ----------
    def clear_media_cache(self):
        self._media_cache.clear()

    async def send(self, tg_id: int, text: str, parse_mode: str, media_names: list[str],
                   media_type: str | None, progress=None) -> list[int]:
        """Xabar(lar) yuboradi va message id'lar ro'yxatini qaytaradi."""
        c = await self._get_client()
        entity = await c.get_input_entity(tg_id)
        pm = "html" if parse_mode == "html" else None
        if not media_names:
            m = await c.send_message(entity, text, parse_mode=pm)
            return [m.id]

        paths = [str(MEDIA_DIR / n) for n in media_names]
        key = tuple(media_names)
        caption_ok = bool(text) and len(text) <= CAPTION_LIMIT
        cached = self._media_cache.get(key)
        single = len(paths) == 1

        async def do_send(fresh: bool):
            kwargs = {}
            if fresh:
                kwargs["progress_callback"] = progress if single else None
                if media_type == "video":
                    kwargs["supports_streaming"] = True
                    attr = video_attributes(paths[0])
                    if attr:
                        kwargs["attributes"] = [attr]
            if caption_ok:
                kwargs.update(caption=text, parse_mode=pm)
            src = paths if fresh else cached
            if single:
                src = src[0]
            return await c.send_file(entity, src, **kwargs)

        try:
            msgs = await do_send(cached is None)
        except Exception as e:
            if cached is not None and type(e).__name__ in _REFRESH_ERRORS:
                self._media_cache.pop(key, None)
                cached = None
                msgs = await do_send(True)
            else:
                raise
        msgs = msgs if isinstance(msgs, list) else [msgs]
        ids = [m.id for m in msgs if m is not None]
        if key not in self._media_cache and all(getattr(m, "media", None) for m in msgs if m is not None):
            self._media_cache[key] = [m.media for m in msgs if m is not None]
        if text and not caption_ok:
            t = await c.send_message(entity, text, parse_mode=pm)
            ids.append(t.id)
        return ids

    async def delete_messages(self, tg_id: int, ids: list[int]):
        c = await self._get_client()
        entity = await c.get_input_entity(tg_id)
        await c.delete_messages(entity, ids)

    async def edit_message(self, tg_id: int, msg_id: int, text: str, parse_mode: str):
        c = await self._get_client()
        entity = await c.get_input_entity(tg_id)
        await c.edit_message(entity, msg_id, text, parse_mode="html" if parse_mode == "html" else None)

    async def fetch_stats(self, tg_id: int, ids: list[int]) -> list[dict]:
        c = await self._get_client()
        entity = await c.get_input_entity(tg_id)
        msgs = await c.get_messages(entity, ids=ids)
        out = []
        for mid, m in zip(ids, msgs):
            if m is None or type(m).__name__ == "MessageEmpty":
                out.append({"id": mid, "deleted": True})
                continue
            react = sum(r.count for r in m.reactions.results) if getattr(m, "reactions", None) else 0
            repl = m.replies.replies if getattr(m, "replies", None) else 0
            out.append({"id": mid, "deleted": False, "views": m.views or 0, "forwards": m.forwards or 0,
                        "reactions": react, "replies": repl})
        return out

    async def leave(self, tg_id: int):
        """Guruh/kanaldan chiqadi."""
        c = await self._get_client()
        await c.delete_dialog(tg_id)

    async def mute(self, tg_id: int) -> str:
        """Guruh bildirishnomalarini butunlay o'chiradi (qo'lda unmute qilmaguncha). 'already' yoki 'muted' qaytaradi."""
        c = await self._get_client()
        peer = InputNotifyPeer(peer=await c.get_input_entity(tg_id))
        cur = await c(GetNotifySettingsRequest(peer=peer))
        until = getattr(cur, "mute_until", None)
        if until is not None and getattr(until, "timestamp", None):
            until = int(until.timestamp())
        if until and until > 2_000_000_000:
            return "already"
        await c(UpdateNotifySettingsRequest(peer=peer, settings=InputPeerNotifySettings(
            mute_until=2_147_483_647, show_previews=False, silent=True)))
        return "muted"

    async def inspect_public(self, username: str) -> dict:
        """Ommaviy guruhni a'zo bo'lmasdan tekshiradi: tavsif, a'zolar, faollik, matn namunalari."""
        c = await self._get_client()
        e = await c.get_entity(username)
        if not isinstance(e, Channel):
            return {"skip": "Guruh emas"}
        if e.broadcast:
            return {"skip": "Kanal (reklama yozib bo'lmaydi)"}
        full = await c(GetFullChannelRequest(e))
        fc = full.full_chat
        now = datetime.now(e.date.tzinfo) if getattr(e, "date", None) else datetime.now()
        texts, week = [], 0
        async for m in c.iter_messages(e, limit=100):
            if getattr(m, "message", None):
                texts.append(m.message[:240])
                if m.date and (datetime.now(m.date.tzinfo) - m.date).days < 7:
                    week += 1
        br = e.default_banned_rights
        pinned = await _pinned_text(c, e, fc)
        return {"title": e.title, "about": fc.about or "", "pinned": pinned, "ads_flag": ads_prohibited(fc.about or "", pinned),
                "members": getattr(fc, "participants_count", None),
                "slowmode": getattr(fc, "slowmode_seconds", 0) or 0, "per_day": round(week / 7, 1), "texts": texts[:60],
                "sample_count": len(texts), "scam": bool(getattr(e, "scam", False) or getattr(e, "fake", False)),
                "restricted": bool(getattr(e, "restricted", False)), "join_request": bool(getattr(e, "join_request", False)),
                "can_send": not (br and br.send_messages), "joined": not getattr(e, "left", True), "tg_id": int("-100" + str(e.id))}
