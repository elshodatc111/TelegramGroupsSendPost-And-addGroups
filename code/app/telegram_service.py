"""Telegram bilan ishlash (Telethon, user account / MTProto)."""
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import Channel, Chat, ChatInviteAlready, DocumentAttributeVideo, User

from . import db
from .config import CAPTION_LIMIT, MEDIA_DIR, SESSION_PATH, log


class NotAGroup(Exception):
    """Username guruh/kanalga emas, foydalanuvchi yoki botga tegishli."""


_REFRESH_ERRORS = ("FileReferenceExpiredError", "FileReferenceInvalidError", "MediaInvalidError",
                   "FileIdInvalidError", "MediaEmptyError", "WebpageMediaEmptyError")


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


class TelegramService:
    def __init__(self):
        self.client: TelegramClient | None = None
        self._creds = None
        self._phone = None
        self._code_hash = None
        self.info: dict | None = None        # oxirgi ma'lum akkaunt holati (sidebar uchun)
        self._media_cache: dict = {}         # bir marta yuklab, boshqa guruhlarga qayta ishlatish

    # ---------- ulanish ----------
    async def _get_client(self) -> TelegramClient:
        api_id = db.get_setting("api_id")
        api_hash = db.get_setting("api_hash")
        if not api_id or not api_hash:
            raise RuntimeError("API ID va API HASH hali kiritilmagan")
        creds = (api_id, api_hash)
        if self.client is not None and self._creds != creds:
            await self.client.disconnect()
            self.client = None
        if self.client is None:
            self.client = TelegramClient(str(SESSION_PATH), int(api_id), api_hash)
            self._creds = creds
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def shutdown(self):
        if self.client and self.client.is_connected():
            await self.client.disconnect()

    async def status(self) -> dict:
        if not (db.get_setting("api_id") and db.get_setting("api_hash")):
            self.info = None
            return {"configured": False, "authorized": False}
        try:
            c = await self._get_client()
            if await c.is_user_authorized():
                me = await c.get_me()
                name = " ".join(x for x in (me.first_name, me.last_name) if x)
                self.info = {"name": name or (me.username or "Akkaunt"), "phone": me.phone,
                             "username": me.username}
                return {"configured": True, "authorized": True, **self.info}
            self.info = None
            return {"configured": True, "authorized": False}
        except Exception as e:  # tarmoq xatosi va h.k.
            log.warning("status xatosi: %s", e)
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
            self.client = None
            self.info = None

    # ---------- guruhlar ----------
    async def fetch_groups(self) -> int:
        c = await self._get_client()
        stamp = db.now()
        rows = []
        async for d in c.iter_dialogs():
            e = d.entity
            can, kind = True, "group"
            if isinstance(e, Channel):
                if getattr(e, "left", False):
                    continue
                if e.broadcast:
                    kind = "channel"
                    can = bool(e.creator or (e.admin_rights and e.admin_rights.post_messages))
                    if not can:
                        continue  # o'zimiz post yoza olmaydigan kanallar kerak emas
                else:
                    kind = "supergroup"
                    br = e.default_banned_rights
                    can = bool(e.creator or e.admin_rights or not (br and br.send_messages))
            elif isinstance(e, Chat):
                if getattr(e, "deactivated", False) or getattr(e, "left", False):
                    continue
                br = e.default_banned_rights
                can = bool(e.creator or e.admin_rights or not (br and br.send_messages))
            else:
                continue  # shaxsiy chat/botlar
            rows.append((d.id, d.name or "Nomsiz", getattr(e, "username", None), kind,
                         getattr(e, "participants_count", None), int(can), stamp))
        db.ex("DELETE FROM groups")
        db.many("INSERT INTO groups(tg_id,title,username,kind,members,can_post,synced_at) "
                "VALUES(?,?,?,?,?,?,?)", rows)
        db.ex("DELETE FROM group_list_items WHERE tg_id NOT IN (SELECT tg_id FROM groups)")
        return len(rows)

    # ---------- a'zo bo'lish ----------
    async def join(self, kind: str, key: str) -> tuple[str, str | None]:
        """('joined'|'already', guruh_nomi) qaytaradi yoki xato ko'taradi."""
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

    async def send(self, tg_id: int, text: str, parse_mode: str,
                   media_path: str | None, media_type: str | None, progress=None):
        c = await self._get_client()
        entity = await c.get_input_entity(tg_id)
        pm = "html" if parse_mode == "html" else None
        if not media_path:
            await c.send_message(entity, text, parse_mode=pm)
            return

        path = str(MEDIA_DIR / media_path)
        caption_ok = bool(text) and len(text) <= CAPTION_LIMIT
        cached = self._media_cache.get(media_path)
        src = cached if cached is not None else path

        async def do_send(source, fresh_upload: bool):
            kwargs = {}
            if fresh_upload:
                kwargs["progress_callback"] = progress
                if media_type == "video":
                    kwargs["supports_streaming"] = True
                    attr = video_attributes(path)
                    if attr:
                        kwargs["attributes"] = [attr]
            if caption_ok:
                kwargs.update(caption=text, parse_mode=pm)
            return await c.send_file(entity, source, **kwargs)

        try:
            msg = await do_send(src, cached is None)
        except Exception as e:
            if cached is not None and type(e).__name__ in _REFRESH_ERRORS:
                self._media_cache.pop(media_path, None)
                msg = await do_send(path, True)
            else:
                raise
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if msg is not None and getattr(msg, "media", None) and media_path not in self._media_cache:
            self._media_cache[media_path] = msg.media  # keyingi guruhlarga qayta yuklamasdan yuboriladi
        if text and not caption_ok:
            await c.send_message(entity, text, parse_mode=pm)
