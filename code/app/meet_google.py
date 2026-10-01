"""Majlislar: Google OAuth (localhost), Meet REST API va Calendar API.

- Faqat httpx ishlatiladi (qo'shimcha kutubxona kerak emas).
- OAuth: «Desktop app» mijozi, qaytish manzili http://127.0.0.1:<port>/meet/google/callback (PKCE bilan). Domen kerak emas.
- Tokenlar secure.py orqali shifrlanadi (Windows DPAPI): mt_settings jadvalida ochiq saqlanmaydi.
- Host akkaunt sozlamada almashtiriladi: «Google'ni uzish» + qayta ulash (Gmail yoki Workspace).
"""
import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from .config import PORT, log

# Sinov uchun almashtirish mumkin (haqiqiy ishlashda o'zgarmaydi)
AUTH_URL = os.environ.get("TGP_GOOGLE_AUTH", "https://accounts.google.com/o/oauth2/v2/auth")
TOKEN_URL = os.environ.get("TGP_GOOGLE_TOKEN", "https://oauth2.googleapis.com/token")
USERINFO_URL = os.environ.get("TGP_GOOGLE_USERINFO", "https://openidconnect.googleapis.com/v1/userinfo")
CAL = os.environ.get("TGP_GOOGLE_CAL", "https://www.googleapis.com/calendar/v3")
MEET = os.environ.get("TGP_GOOGLE_MEET", "https://meet.googleapis.com/v2")

SCOPES = ["openid", "email",
          "https://www.googleapis.com/auth/calendar",
          "https://www.googleapis.com/auth/meetings.space.created",
          "https://www.googleapis.com/auth/meetings.space.readonly"]


def _S():
    from . import meet_sched
    return meet_sched


# ---------------------------------------------------------------- xato
class GoogleError(Exception):
    def __init__(self, msg, status=0, reason="", body=None, hint=""):
        super().__init__(msg)
        self.msg, self.status, self.reason, self.body, self.hint = msg, status, reason, body, hint

    def short(self) -> str:
        return (f"{self.msg}" + (f" — {self.hint}" if self.hint else ""))[:300]

    def full(self) -> str:
        b = json.dumps(self.body, ensure_ascii=False)[:500] if self.body else ""
        return f"HTTP {self.status} {self.reason}: {self.msg}. {self.hint} {b}".strip()


def _explain(status: int, data) -> GoogleError:
    err = data.get("error") if isinstance(data, dict) else None
    msg, reason, hint = f"HTTP {status}", "", ""
    if isinstance(err, dict):
        msg = err.get("message") or msg
        reason = err.get("status") or ""
        for d in err.get("details") or []:
            if d.get("reason") in ("SERVICE_DISABLED", "accessNotConfigured"):
                reason = "SERVICE_DISABLED"
                url = (d.get("metadata") or {}).get("activationUrl") or "https://console.cloud.google.com/apis/library"
                hint = f"Google Cloud'da bu API yoqilmagan. Yoqing: {url}"
            if d.get("reason") == "ACCESS_TOKEN_SCOPE_INSUFFICIENT":
                reason, hint = "SCOPE", "Ruxsat (scope) yetarli emas: Sozlamalarda Google'ni uzib, qayta ulang."
        for e in err.get("errors") or []:
            if e.get("reason") == "accessNotConfigured":
                reason, hint = "SERVICE_DISABLED", "Google Cloud'da Calendar API yoqilmagan (Yo'riqnoma 2-qadam)."
            if e.get("reason") in ("insufficientPermissions",):
                reason, hint = "SCOPE", "Ruxsat yetarli emas: Google'ga qayta ulang."
    elif isinstance(err, str):
        msg, reason = data.get("error_description") or err, err
        if err == "invalid_grant":
            hint = "Token eskirgan yoki bekor qilingan. Google'ga qayta ulang (OAuth ilovasi Production holatida bo'lsin)."
        elif err in ("invalid_client", "unauthorized_client"):
            hint = "Client ID/Secret noto'g'ri yoki mijoz turi «Desktop app» emas."
    if not hint:
        hint = {401: "Token yaroqsiz: Google'ga qayta ulang.", 403: "Ruxsat yo'q yoki API yoqilmagan.",
                404: "Topilmadi.", 429: "Google so'rov limiti: bir ozdan keyin urinib ko'riladi."}.get(status, "")
    return GoogleError(msg, status, reason, data if isinstance(data, dict) else None, hint)


# ---------------------------------------------------------------- holat va tokenlar
def configured() -> bool:
    S = _S()
    return bool(S.get("google_client_id") and S.get("google_client_secret"))


def connected() -> bool:
    S = _S()
    return bool(S.get("g_refresh")) and S.get("g_state") == "ok"


def redirect_uri() -> str:
    return f"http://127.0.0.1:{PORT}/meet/google/callback"


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def start_auth() -> str:
    S = _S()
    if not configured():
        raise GoogleError("Client ID / Secret kiritilmagan", hint="Sozlamalarda OAuth kalitini kiriting.")
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(18)
    S.put("oauth_tmp", json.dumps({"state": state, "verifier": verifier, "at": time.time()}))
    q = {"client_id": S.get("google_client_id"), "redirect_uri": redirect_uri(), "response_type": "code",
         "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
         "state": state, "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    return AUTH_URL + "?" + urlencode(q)


async def _token_post(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(TOKEN_URL, data=data)
    try:
        js = r.json()
    except Exception:
        js = {"error": "bad_response", "error_description": r.text[:200]}
    if r.status_code >= 400:
        raise _explain(r.status_code, js)
    return js


def _save_tokens(js: dict):
    S = _S()
    S.put("g_access", js["access_token"])
    S.put("g_exp", str(time.time() + int(js.get("expires_in", 3600)) - 60))
    if js.get("refresh_token"):
        S.put("g_refresh", js["refresh_token"])
    S.put("g_state", "ok")


async def finish_auth(code: str, state: str) -> str:
    """Callback: kodni tokenga almashtiradi. Ulangan host emailini qaytaradi."""
    S = _S()
    try:
        tmp = json.loads(S.get("oauth_tmp") or "{}")
    except Exception:
        tmp = {}
    if not tmp or tmp.get("state") != state or time.time() - tmp.get("at", 0) > 900:
        raise GoogleError("Ulanish so'rovi eskirgan yoki boshqa joydan kelgan", hint="Qaytadan «Google'ga ulash» ni bosing.")
    S.drop("oauth_tmp")
    js = await _token_post({"code": code, "client_id": S.get("google_client_id"), "client_secret": S.get("google_client_secret"),
                            "redirect_uri": redirect_uri(), "grant_type": "authorization_code", "code_verifier": tmp["verifier"]})
    if not js.get("refresh_token"):
        raise GoogleError("Google yangilash tokenini bermadi", hint="Google akkaunt sozlamalarida ilovaga ruxsatni o'chirib, qayta ulang.")
    _save_tokens(js)
    S.put("g_connected_at", S.fmt(S.now()))
    info = await userinfo()
    S.put("g_email", info.get("email", ""))
    S.put("g_hd", info.get("hd", ""))
    domain = (info.get("email", "").split("@") + [""])[1].lower()
    S.put("host_type", "workspace" if info.get("hd") or domain not in ("gmail.com", "googlemail.com") else "gmail")
    S.drop("last_poll_err")
    return info.get("email", "")


def disconnect():
    S = _S()
    for k in ("g_access", "g_exp", "g_refresh", "g_email", "g_hd", "g_connected_at"):
        S.drop(k)
    S.put("g_state", "none")


async def access_token(force=False) -> str:
    S = _S()
    rt = S.get("g_refresh")
    if not rt:
        raise GoogleError("Google ulanmagan", hint="Majlislar → Sozlamalar → «Google'ga ulash».")
    if not force and S.get("g_access") and float(S.get("g_exp", "0") or 0) > time.time():
        return S.get("g_access")
    try:
        js = await _token_post({"client_id": S.get("google_client_id"), "client_secret": S.get("google_client_secret"),
                                "refresh_token": rt, "grant_type": "refresh_token"})
    except GoogleError as e:
        if e.reason == "invalid_grant":
            S.put("g_state", "expired")
            S.add_alert("google_token", "Google token eskirdi (invalid_grant). Majlislar → Sozlamalar → «Google'ga ulash». "
                        "OAuth ilovasi «Testing» holatida bo'lsa token 7 kundan keyin o'chadi: Production'ga o'tkazing.",
                        level="error", dedupe=f"tok:{S.fmt(S.now())[:10]}")
        raise
    _save_tokens(js)
    return js["access_token"]


async def userinfo() -> dict:
    return await request("GET", USERINFO_URL)


# ---------------------------------------------------------------- umumiy so'rov
async def request(method: str, url: str, params=None, body=None) -> dict:
    tok = await access_token()
    r = None
    async with httpx.AsyncClient(timeout=25) as c:
        for attempt in range(3):
            r = await c.request(method, url, params=params, json=body, headers={"Authorization": f"Bearer {tok}"})
            if r.status_code == 401 and attempt == 0:
                tok = await access_token(force=True)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            break
    if r.status_code >= 400:
        try:
            js = r.json()
        except Exception:
            js = {"error": {"message": r.text[:200]}}
        raise _explain(r.status_code, js)
    try:
        return r.json() if r.content else {}
    except Exception:
        return {}


# ---------------------------------------------------------------- Meet REST API (v2)
async def create_space(moderation="ON") -> dict:
    """Yangi Meet xonasi. accessType=OPEN: havolasi borlar kutmasdan kiradi (havola ochiq)."""
    return await request("POST", f"{MEET}/spaces", body={"config": {"accessType": "OPEN", "entryPointAccess": "ALL",
                                                                    "moderation": "ON" if moderation == "ON" else "OFF"}})


async def get_space(name: str) -> dict:
    return await request("GET", f"{MEET}/{name}")


async def add_cohost(space: str, email: str) -> dict:
    return await request("POST", f"{MEET}/{space}/members", body={"email": email, "role": "COHOST"})


async def list_members(space: str) -> list[dict]:
    return (await request("GET", f"{MEET}/{space}/members")).get("members", [])


async def delete_member(member_name: str):
    await request("DELETE", f"{MEET}/{member_name}")


async def _paged(url: str, key: str, params=None) -> list[dict]:
    out, p = [], dict(params or {})
    for _ in range(20):
        js = await request("GET", url, params=p)
        out += js.get(key, [])
        if not js.get("nextPageToken"):
            break
        p["pageToken"] = js["nextPageToken"]
    return out


async def conference_records(space: str) -> list[dict]:
    return await _paged(f"{MEET}/conferenceRecords", "conferenceRecords", {"filter": f'space.name = "{space}"', "pageSize": 20})


async def recent_conference_records(n=5) -> list[dict]:
    return (await request("GET", f"{MEET}/conferenceRecords", params={"pageSize": n})).get("conferenceRecords", [])


async def participants(conf: str) -> list[dict]:
    return await _paged(f"{MEET}/{conf}/participants", "participants", {"pageSize": 250})


async def participant_sessions(part: str) -> list[dict]:
    return await _paged(f"{MEET}/{part}/participantSessions", "participantSessions", {"pageSize": 250})


# ---------------------------------------------------------------- Calendar API (v3)
async def create_calendar(summary: str) -> dict:
    return await request("POST", f"{CAL}/calendars", body={"summary": summary[:250], "timeZone": "Asia/Tashkent",
                                                           "description": "Telegram Group Post · Majlislar"})


async def delete_calendar(cal_id: str):
    await request("DELETE", f"{CAL}/calendars/{cal_id}")


async def share_calendar(cal_id: str, email: str, role="reader"):
    await request("POST", f"{CAL}/calendars/{cal_id}/acl", params={"sendNotifications": "false"},
                  body={"role": role, "scope": {"type": "user", "value": email}})


async def insert_event(cal_id: str, body: dict) -> dict:
    return await request("POST", f"{CAL}/calendars/{cal_id}/events", body=body)


async def patch_event(cal_id: str, event_id: str, body: dict) -> dict:
    return await request("PATCH", f"{CAL}/calendars/{cal_id}/events/{event_id}", body=body)


async def delete_event(cal_id: str, event_id: str):
    try:
        await request("DELETE", f"{CAL}/calendars/{cal_id}/events/{event_id}")
    except GoogleError as e:
        if e.status not in (404, 410):
            raise


# ---------------------------------------------------------------- diagnostika
def _step(key, title, state, detail="", fix="", ms=0):
    return {"key": key, "title": title, "state": state, "detail": detail, "fix": fix, "ms": ms}


async def diagnose(test_email: str = "") -> list[dict]:
    """Test majlis yaratib, har imkoniyatni tekshiradi: token, space, access, co-host, calendar, participants API, 60 daqiqa."""
    S = _S()
    out: list[dict] = []

    async def run(key, title, fn, skip=False):
        if skip:
            out.append(_step(key, title, "skip", "Oldingi bosqich o'tmadi"))
            return None
        t0 = time.time()
        try:
            res = await fn()
            st, detail = (res if isinstance(res, tuple) else ("ok", res or ""))
            out.append(_step(key, title, st, detail, ms=int((time.time() - t0) * 1000)))
            return True
        except GoogleError as e:
            out.append(_step(key, title, "fail", f"HTTP {e.status} {e.msg}"[:400], e.hint, int((time.time() - t0) * 1000)))
        except Exception as e:
            out.append(_step(key, title, "fail", f"{type(e).__name__}: {e}"[:400], "", int((time.time() - t0) * 1000)))
        return False

    if not configured():
        return [_step("cfg", "OAuth kalit", "fail", "Client ID / Secret kiritilmagan", "Sozlamalar → Google bo'limi va Yo'riqnoma.")]
    if S.get("g_state") != "ok" or not S.get("g_refresh"):
        return [_step("token", "Google token", "fail", "Google'ga ulanmagan yoki token eskirgan", "Sozlamalar → «Google'ga ulash».")]

    info = {}

    async def t_token():
        nonlocal info
        await access_token(force=True)
        info = await userinfo()
        return ("ok", f"{info.get('email', '?')} (token yangilandi)")
    ok = await run("token", "Token va akkaunt", t_token)
    state = {"space": None, "uri": None, "cal": None, "member": None}

    async def t_space():
        sp = await create_space(S.cfg("moderation"))
        state["space"], state["uri"] = sp["name"], sp.get("meetingUri")
        return ("ok", f"{sp['name']} · {sp.get('meetingUri', '?')}")
    conn_ok = ok
    ok = await run("space", "Meet xonasi yaratish (spaces.create)", t_space, skip=not ok)
    sp_ok = ok

    async def t_access():
        sp = await get_space(state["space"])
        at = (sp.get("config") or {}).get("accessType")
        if at == "OPEN":
            return ("ok", "accessType = OPEN: havolasi bor odam kutmasdan kiradi")
        return ("warn", f"accessType = {at}: kutish xonasi bo'lishi mumkin")
    await run("access", "Havola ochiqligi (accessType)", t_access, skip=not sp_ok)

    async def t_cohost():
        email = (test_email or "").strip().lower()
        if not email:
            ts = [t for t in S.teachers(True) if t.get("gmail")]
            email = ts[0]["gmail"] if ts else ""
        if not email:
            return ("warn", "Tekshirish uchun Gmail yo'q: o'qituvchi qo'shing yoki quyiga test Gmail kiriting")
        if email == (info.get("email") or "").lower():
            return ("warn", "Host o'zini co-host qila olmaydi: boshqa Gmail kiriting")
        m = await add_cohost(state["space"], email)
        state["member"] = m.get("name")
        mems = await list_members(state["space"])
        roles = [f"{x.get('email', '?')}={x.get('role', '?')}" for x in mems]
        return ("ok", f"{email} COHOST qilindi; a'zolar: {', '.join(roles)}")
    await run("cohost", "O'qituvchini co-host qilish (spaces.members)", t_cohost, skip=not sp_ok)

    async def t_cal():
        cal = await create_calendar("TGP diagnostika (o'chiriladi)")
        state["cal"] = cal["id"]
        s = S.now() + timedelta(hours=2)
        body = {"summary": "Diagnostika", "location": state["uri"] or "",
                "start": {"dateTime": s.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tashkent"},
                "end": {"dateTime": (s + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Tashkent"}}
        ev = await insert_event(cal["id"], body)
        await patch_event(cal["id"], ev["id"], {"summary": "Diagnostika (yangilandi)"})
        await delete_event(cal["id"], ev["id"])
        return ("ok", "alohida kalendar yaratildi, tadbir qo'shildi, yangilandi va o'chirildi")
    await run("calendar", "Google Calendar (kalendar + tadbir)", t_cal, skip=not conn_ok)

    async def t_cal_del():
        await delete_calendar(state["cal"])
        return ("ok", "test kalendar o'chirildi")
    if state["cal"]:
        await run("calendar_clean", "Test kalendarni o'chirish", t_cal_del)

    async def t_part():
        recs = await conference_records(state["space"])
        detail = f"conferenceRecords ishlaydi (test xonada hozircha {len(recs)} ta konferensiya)"
        gen = await recent_conference_records(3)
        if gen:
            conf = gen[0]["name"]
            ps = await participants(conf)
            ses = await participant_sessions(ps[0]["name"]) if ps else []
            detail += f"; oxirgi konferensiya: {len(ps)} ishtirokchi, {len(ses)} sessiya o'qildi"
        else:
            detail += "; ishtirokchilar API'si haqiqiy darsdan keyin to'liq tekshiriladi (hozir konferensiya yo'q)"
        return ("ok", detail)
    await run("participants", "Davomat API (conferenceRecords / participants / participantSessions)", t_part, skip=not sp_ok)

    async def t_limit():
        host = S.cfg("host_type")
        long_n = len([1 for r in S.db.q("SELECT 1 FROM mt_schedule WHERE active=1 AND duration_min>?", (S.GMAIL_LIMIT_MIN,))])
        if host == "gmail":
            extra = f" Hozir {long_n} ta jadval qatori 60 daqiqadan uzun." if long_n else ""
            return ("warn" if long_n else "ok", "Meet limiti: 60 daqiqa (Gmail)." + extra)
        return ("ok", "Workspace akkaunt: 60 daqiqalik Gmail cheklovi yo'q (tarif limitiga qarang)")
    await run("limit60", "60 daqiqa cheklovi", t_limit)
    out.append(_step("cleanup", "Test xonalar", "info",
                     f"{state['space'] or '—'} xonasi qoldi (Meet API xonani o'chirishni bermaydi; ishlatilmasa o'zi eskiradi)"))
    return out
