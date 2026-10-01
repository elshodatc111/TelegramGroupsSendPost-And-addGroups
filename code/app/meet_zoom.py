"""Majlislar: Zoom REST API (Server-to-Server OAuth) va Telegram Bot API.

- Faqat httpx ishlatiladi. Har bir Zoom akkaunt o'z Account ID / Client ID / Client Secret bilan ulanadi
  (Zoom Marketplace'da «Server-to-Server OAuth» ilovasi). Domen, Production-publish va 7 kunlik token kerak emas.
- Client Secret secure.py orqali shifrlanadi (bazada ochiq saqlanmaydi).
- Manzillar sinov uchun almashtiriladi: TGP_ZOOM_API, TGP_ZOOM_OAUTH, TGP_BOT_API.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from .config import log

API = os.environ.get("TGP_ZOOM_API", "https://api.zoom.us/v2").rstrip("/")
OAUTH = os.environ.get("TGP_ZOOM_OAUTH", "https://zoom.us/oauth/token")
BOT_API = os.environ.get("TGP_BOT_API", "https://api.telegram.org").rstrip("/")

TZ_NAME = "Asia/Tashkent"
TIMEOUT = httpx.Timeout(25.0, connect=10.0)

# Zoom ilovasiga beriladigan ruxsatlar (Yo'riqnomada ko'rsatiladi)
SCOPES = ["meeting:write:meeting:admin", "meeting:read:meeting:admin", "meeting:update:meeting:admin",
          "meeting:delete:meeting:admin", "user:read:user:admin"]


# ---------------------------------------------------------------- xato
class ZoomError(Exception):
    def __init__(self, msg, status=0, code=None, hint="", body=None):
        super().__init__(msg)
        self.msg, self.status, self.code, self.hint, self.body = msg, status, code, hint, body

    def short(self) -> str:
        return (self.msg + (f" — {self.hint}" if self.hint else ""))[:320]

    def full(self) -> str:
        b = json.dumps(self.body, ensure_ascii=False)[:400] if self.body else ""
        return f"HTTP {self.status} kod={self.code}: {self.msg}. {self.hint} {b}".strip()


def _explain(status: int, data) -> ZoomError:
    code = data.get("code") if isinstance(data, dict) else None
    msg = (data.get("message") or data.get("reason") or data.get("error_description") or f"HTTP {status}") if isinstance(data, dict) else f"HTTP {status}"
    low = str(msg).lower()
    hint = ""
    if isinstance(data, dict) and data.get("error") in ("invalid_client", "unauthorized_client"):
        hint = "Client ID yoki Client Secret noto'g'ri."
    elif isinstance(data, dict) and data.get("error") == "invalid_request" and "account" in low:
        hint = "Account ID noto'g'ri (Zoom ilovasining App Credentials bo'limidan nusxalang)."
    elif "scope" in low:
        hint = "Zoom ilovasida kerakli ruxsat (scope) yoqilmagan: ilova → Scopes bo'limida qo'shing va ilovani Activate qiling."
    elif code == 1001 or "user does not exist" in low or "not found" in low and "user" in low:
        hint = "Zoom email noto'g'ri yoki bu foydalanuvchi shu Zoom akkauntga tegishli emas."
    elif code in (124, 4700) or "invalid access token" in low:
        hint = "Token yaroqsiz: kalitlarni tekshiring."
    elif code == 3161 or "not allow" in low and "meeting" in low:
        hint = "Bu akkaunt/rejada majlis yaratish cheklangan bo'lishi mumkin."
    elif code == 300 or "validation" in low:
        hint = "Zoom so'rov parametrlarini rad etdi."
    elif status == 429:
        hint = "Zoom so'rov limiti: bir ozdan keyin qayta urinib ko'riladi."
    elif status in (401, 403):
        hint = "Ruxsat yo'q: ilova activate qilinganini va scope'lar qo'shilganini tekshiring."
    return ZoomError(str(msg), status, code, hint, data if isinstance(data, dict) else None)


# ---------------------------------------------------------------- vaqt
def to_zoom_local(dt: datetime) -> str:
    """Toshkent vaqti (naive) -> Zoom start_time (timezone=Asia/Tashkent bilan bu mahalliy vaqt)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- token (akkaunt bo'yicha keshlanadi)
_tokens: dict[tuple, tuple[str, float]] = {}


def _secret(acc: dict) -> str:
    from . import secure
    v = acc.get("client_secret") or ""
    if secure.is_encrypted(v):
        d = secure.dec(v)
        return d or ""
    return v


async def access_token(acc: dict, force=False) -> str:
    key = (acc.get("account_id"), acc.get("client_id"))
    t = _tokens.get(key)
    if t and not force and t[1] > time.time() + 60:
        return t[0]
    sec = _secret(acc)
    if not (acc.get("account_id") and acc.get("client_id") and sec):
        raise ZoomError("Kalitlar to'liq emas", hint="Account ID, Client ID va Client Secret kiritilishi kerak.")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(OAUTH, params={"grant_type": "account_credentials", "account_id": acc["account_id"]},
                             auth=(acc["client_id"], sec))
    except httpx.HTTPError as e:
        raise ZoomError(f"Zoom'ga ulanib bo'lmadi: {type(e).__name__}", hint="Internet yoki zoom.us ga kirishni tekshiring.")
    try:
        data = r.json()
    except Exception:
        data = {}
    if r.status_code != 200 or "access_token" not in data:
        raise _explain(r.status_code, data)
    _tokens[key] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
    return data["access_token"]


async def request(acc: dict, method: str, path: str, *, json_body=None, params=None, ok=(200, 201, 204)):
    """Zoom API so'rovi; token eskirsa bir marta yangilaydi, 429 da qisqa kutadi."""
    import asyncio
    last = None
    for attempt in range(3):
        tok = await access_token(acc, force=attempt == 1)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.request(method, API + path, json=json_body, params=params, headers={"Authorization": f"Bearer {tok}"})
        except httpx.HTTPError as e:
            raise ZoomError(f"Zoom'ga ulanib bo'lmadi: {type(e).__name__}", hint="Internet yoki api.zoom.us ga kirishni tekshiring.")
        if r.status_code in ok:
            if r.status_code == 204 or not r.content:
                return {}
            try:
                return r.json()
            except Exception:
                return {}
        try:
            data = r.json()
        except Exception:
            data = {"message": r.text[:200]}
        last = _explain(r.status_code, data)
        if r.status_code == 401 and attempt == 0:
            continue
        if r.status_code == 429 and attempt < 2:
            await asyncio.sleep(2 + attempt * 3)
            continue
        break
    raise last


def _uid(acc: dict) -> str:
    return quote((acc.get("email") or "me").strip(), safe="")


# ---------------------------------------------------------------- majlislar
async def get_user(acc: dict) -> dict:
    return await request(acc, "GET", f"/users/{_uid(acc)}")


async def create_meeting(acc: dict, topic: str, start: datetime, duration: int, agenda: str = "") -> dict:
    """Rejalashtirilgan majlis (type 2). Host kirmasdan oldin ham kirish mumkin; boshlash havolasi o'qituvchiga beriladi."""
    body = {"topic": topic[:200], "type": 2, "start_time": to_zoom_local(start), "duration": max(1, int(duration)),
            "timezone": TZ_NAME, "agenda": agenda[:1000],
            "settings": {"join_before_host": True, "jbh_time": 0, "waiting_room": False, "mute_upon_entry": True,
                         "host_video": True, "participant_video": False, "approval_type": 2, "auto_recording": "none",
                         "meeting_authentication": False, "use_pmi": False, "audio": "both"}}
    d = await request(acc, "POST", f"/users/{_uid(acc)}/meetings", json_body=body)
    if not d.get("id") or not d.get("join_url"):
        raise ZoomError("Zoom javobida majlis havolasi yo'q", body=d)
    return d


async def get_meeting(acc: dict, mid) -> dict:
    return await request(acc, "GET", f"/meetings/{mid}")


async def end_meeting(acc: dict, mid) -> None:
    try:
        await request(acc, "PUT", f"/meetings/{mid}/status", json_body={"action": "end"})
    except ZoomError as e:
        log.info("Zoom majlisni tugatib bo'lmadi (%s): %s", mid, e.short())


async def delete_meeting(acc: dict, mid) -> None:
    try:
        await request(acc, "DELETE", f"/meetings/{mid}", params={"schedule_for_reminder": "false"})
    except ZoomError as e:
        if e.status != 404:
            log.info("Zoom majlisni o'chirib bo'lmadi (%s): %s", mid, e.short())


async def check_account(acc: dict, deep=True) -> list[dict]:
    """Akkauntni bosqichma-bosqich tekshiradi: token, foydalanuvchi, majlis yaratish (so'ng o'chiradi), boshlash havolasi."""
    out = []

    def step(key, title, state, detail="", fix=""):
        out.append({"key": key, "title": title, "state": state, "detail": detail, "fix": fix})
    t0 = time.time()
    try:
        await access_token(acc, force=True)
        step("token", "Token olish (Server-to-Server OAuth)", "ok", f"{int((time.time() - t0) * 1000)} ms")
    except ZoomError as e:
        step("token", "Token olish (Server-to-Server OAuth)", "fail", e.msg, e.hint)
        return out
    try:
        u = await get_user(acc)
        plan = {1: "Basic (bepul)", 2: "Licensed (Pro)", 4: "On-prem"}.get(u.get("type"), str(u.get("type", "?")))
        mm = acc.get("max_min")
        hint = ""
        if u.get("type") == 2 and mm:
            hint = "Bu Pro (Licensed) akkaunt: «Majlis davomiyligi» ni 0 (cheksiz) qilsangiz, dars bo'laklarga bo'linmaydi."
        elif u.get("type") == 1 and mm == 0:
            hint = "Bu bepul akkaunt: «Majlis davomiyligi» ni 40 qiling, aks holda majlis o'rtada uziladi."
        step("user", "Zoom foydalanuvchi", "warn" if hint else "ok", f"{u.get('email', acc.get('email'))} · tarif: {plan}", hint)
    except ZoomError as e:
        step("user", "Zoom foydalanuvchi", "fail", e.msg, e.hint)
        return out
    if not deep:
        return out
    m = None
    try:
        start = datetime.now(timezone(timedelta(hours=5))).replace(tzinfo=None) + timedelta(hours=1)
        m = await create_meeting(acc, "Majlislar sinov (o'chiriladi)", start, 10)
        step("create", "Majlis yaratish", "ok", f"ID {m['id']}")
        step("start_url", "Boshlash havolasi (start_url)", "ok" if m.get("start_url") else "warn",
             "bor" if m.get("start_url") else "Zoom start_url qaytarmadi", "" if m.get("start_url") else "O'qituvchi host bo'lib kira olmasligi mumkin.")
        s = (m.get("settings") or {})
        step("jbh", "Host kirmasdan oldin kirish", "ok" if s.get("join_before_host", True) else "warn",
             "yoqilgan" if s.get("join_before_host", True) else "o'chirilgan",
             "" if s.get("join_before_host", True) else "Zoom akkaunt sozlamalarida (zoom.us → Settings → Meeting) «Join before host» ni ruxsat bering.")
    except ZoomError as e:
        step("create", "Majlis yaratish", "fail", e.msg, e.hint)
    finally:
        if m:
            try:
                await delete_meeting(acc, m["id"])
                step("delete", "Sinov majlisini o'chirish", "ok", "")
            except Exception as e:
                step("delete", "Sinov majlisini o'chirish", "warn", str(e))
    return out


# ================================================================ Telegram Bot API (o'qituvchilar boti)
class BotError(Exception):
    def __init__(self, msg, code=0):
        super().__init__(msg)
        self.msg, self.code = msg, code


async def bot_call(token: str, method: str, params: dict | None = None, timeout: float = 30.0):
    if not token:
        raise BotError("Bot tokeni kiritilmagan")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as c:
            r = await c.post(f"{BOT_API}/bot{token}/{method}", json=params or {})
    except httpx.HTTPError as e:
        raise BotError(f"Telegram'ga ulanib bo'lmadi: {type(e).__name__}")
    try:
        d = r.json()
    except Exception:
        raise BotError(f"Telegram javobi tushunarsiz (HTTP {r.status_code})", r.status_code)
    if not d.get("ok"):
        raise BotError(d.get("description") or f"HTTP {r.status_code}", d.get("error_code") or r.status_code)
    return d["result"]


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def redact(s: str) -> str:
    """Loglarga bot tokeni va start_url tushmasligi uchun."""
    s = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***", str(s))
    return re.sub(r"zak=[^&\s\"']+", "zak=***", s)
