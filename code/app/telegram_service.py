"""Telegram bilan ishlash (Telethon, user account / MTProto)."""
from telethon import TelegramClient, errors
from telethon.tl.types import Channel, Chat

from . import db
from .config import CAPTION_LIMIT, MEDIA_DIR, SESSION_PATH


class TelegramService:
    def __init__(self):
        self.client: TelegramClient | None = None
        self._creds = None
        self._phone = None
        self._code_hash = None
        self._me = None

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
            return {"configured": False, "authorized": False}
        try:
            c = await self._get_client()
            if await c.is_user_authorized():
                me = await c.get_me()
                self._me = me
                name = " ".join(x for x in (me.first_name, me.last_name) if x)
                return {"configured": True, "authorized": True, "name": name,
                        "phone": me.phone, "username": me.username}
            return {"configured": True, "authorized": False}
        except Exception as e:  # tarmoq xatosi va h.k.
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
            self._me = None

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

    # ---------- yuborish ----------
    async def send(self, tg_id: int, text: str, parse_mode: str,
                   media_path: str | None, media_type: str | None):
        c = await self._get_client()
        entity = await c.get_input_entity(tg_id)
        pm = "html" if parse_mode == "html" else None
        if media_path:
            f = str(MEDIA_DIR / media_path)
            kwargs = dict(supports_streaming=True) if media_type == "video" else {}
            if text and len(text) <= CAPTION_LIMIT:
                await c.send_file(entity, f, caption=text, parse_mode=pm, **kwargs)
            else:
                await c.send_file(entity, f, **kwargs)
                if text:
                    await c.send_message(entity, text, parse_mode=pm)
        else:
            await c.send_message(entity, text, parse_mode=pm)
