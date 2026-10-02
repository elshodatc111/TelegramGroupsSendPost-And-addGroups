"""OpenAI API bilan ishlash (httpx) va har bir kanal uchun token / xarajat hisobi.

Modellar va narxlar Sozlamalarda o'zgartiriladi. Model topilmasa, zaxira modellarga o'tadi.
Token har doim hisoblanadi; narx jadvalida model bo'lmasa, token yoziladi, xarajat "narx kiritilmagan" bo'ladi.
"""
import asyncio
import json
import re
from datetime import datetime

import httpx

from . import db
from .config import log

API = "https://api.openai.com/v1"
DEFAULT_MODELS = {"idea": "gpt-5", "analysis": "gpt-5-mini"}
FALLBACKS = ["gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"]
REASONING = ("gpt-5", "o1", "o3", "o4")


class AIError(Exception):
    """Foydalanuvchiga ko'rsatiladigan xato matni."""


def api_key() -> str | None:
    return db.get_setting("openai_key") or None


def configured() -> bool:
    return bool(api_key())


def model_for(purpose: str) -> str:
    key = "ai_model_idea" if purpose == "idea" else "ai_model_analysis"
    return db.get_setting(key) or DEFAULT_MODELS["idea" if purpose == "idea" else "analysis"]


# ---------------------------------------------------------------- narx va hisob
def price_row(model: str):
    rows = db.q("SELECT * FROM ch_prices")
    exact = [r for r in rows if r["model"] == model]
    if exact:
        return exact[0]
    cand = [r for r in rows if model.startswith(r["model"])]
    return max(cand, key=lambda r: len(r["model"])) if cand else None


def calc_cost(model: str, tin: int, tout: int, cached: int = 0, audio_sec: float = 0.0):
    """(xarajat USD, narx_topildimi)"""
    p = price_row(model)
    if not p:
        return 0.0, 0
    fresh = max(0, tin - cached)
    cost = (fresh * (p["in_per_m"] or 0) + cached * (p["cached_per_m"] or p["in_per_m"] or 0) + tout * (p["out_per_m"] or 0)) / 1e6
    cost += (audio_sec / 60.0) * (p["audio_per_min"] or 0)
    return round(cost, 6), 1


def record_usage(channel_id, purpose, model, tin=0, tout=0, cached=0, audio_sec=0.0, ok=True, note=""):
    cost, priced = calc_cost(model, tin, tout, cached, audio_sec)
    title = None
    if channel_id and channel_id < 0:                  # manfiy id = Instagram akkaunti (-ig_accounts.id)
        r = db.one("SELECT username FROM ig_accounts WHERE id=?", (-channel_id,))
        title = ("IG @" + r["username"]) if r and r["username"] else "Instagram"
    elif channel_id:
        r = db.one("SELECT title FROM ch_channels WHERE id=?", (channel_id,))
        title = r["title"] if r else None
    now = datetime.now()
    db.ex("INSERT INTO ch_usage(channel_id,ts,day,purpose,model,tokens_in,tokens_out,cached_in,audio_sec,cost,priced,ok,note,ch_title) "
          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (channel_id or 0, now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d"), purpose, model, int(tin), int(tout),
           int(cached), float(audio_sec), cost, priced, int(ok), (note or "")[:255], title))
    return cost


def month_cost(channel_id: int) -> float:
    m = datetime.now().strftime("%Y-%m")
    r = db.one("SELECT COALESCE(SUM(cost),0) c FROM ch_usage WHERE channel_id=? AND day LIKE ?", (channel_id, m + "%"))
    return float(r["c"] or 0)


def check_cap(channel_id: int):
    cap = float(db.get_setting("ai_cap_usd", 0) or 0)
    if cap > 0 and channel_id and month_cost(channel_id) >= cap:
        raise AIError(f"Bu kanal uchun oylik AI chegarasi (${cap:g}) to'ldi. Sozlamalarda oshirishingiz mumkin.")


# ---------------------------------------------------------------- so'rov
def _err_text(resp: httpx.Response) -> tuple[str, str]:
    try:
        e = resp.json().get("error", {})
        return e.get("message", resp.text[:300]), e.get("code") or e.get("type") or ""
    except Exception:
        return resp.text[:300], ""


async def _post(client, path, payload, *, files=None, data=None):
    hdr = {"Authorization": f"Bearer {api_key()}"}
    for attempt in range(4):
        try:
            if files is not None:
                r = await client.post(API + path, headers=hdr, files=files, data=data)
            else:
                r = await client.post(API + path, headers=hdr, json=payload)
        except httpx.HTTPError as e:
            if attempt == 3:
                raise AIError(f"OpenAI bilan aloqa yo'q: {type(e).__name__}")
            await asyncio.sleep(2 + attempt * 3)
            continue
        if r.status_code == 429:
            msg, code = _err_text(r)
            if "quota" in (code + msg).lower() or "billing" in msg.lower():
                raise AIError("OpenAI hisobida mablag' yetarli emas (billing/quota). platform.openai.com da hisobni tekshiring.")
            await asyncio.sleep(5 + attempt * 10)
            continue
        if r.status_code >= 500:
            await asyncio.sleep(3 + attempt * 5)
            continue
        return r
    raise AIError("OpenAI javob bermadi (qayta urinishlar tugadi)")


async def chat(channel_id, purpose: str, system: str, user: str, *, model: str | None = None, json_mode: bool = True,
               max_out: int = 6000, effort: str | None = None, note: str = "") -> tuple[object, dict]:
    """Chat Completions. JSON rejimida dict, aks holda matn qaytaradi. (natija, meta) — meta: model, tokens."""
    if not configured():
        raise AIError("OpenAI kaliti kiritilmagan. Kanallarim → Sozlamalar bo'limida kiriting.")
    check_cap(channel_id)
    tier = "idea" if purpose == "idea" else "analysis"
    chain = [model or model_for(tier)] + [m for m in FALLBACKS if m != (model or model_for(tier))]
    last_err = "noma'lum xato"
    async with httpx.AsyncClient(timeout=httpx.Timeout(240.0, connect=15.0)) as client:
        for mdl in chain:
            payload = {"model": mdl, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                       "max_completion_tokens": max_out}
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            if mdl.startswith(REASONING):
                payload["reasoning_effort"] = effort or ("medium" if tier == "idea" else "low")
            else:
                payload["temperature"] = 0.8 if tier == "idea" else 0.3
            for _ in range(4):                       # qo'llab-quvvatlanmaydigan parametrlarni olib tashlab qayta urinish
                r = await _post(client, "/chat/completions", payload)
                if r.status_code == 200:
                    break
                msg, code = _err_text(r)
                m = re.search(r"['\"]([a-z_]+)['\"]", msg) if r.status_code == 400 else None
                if m and m.group(1) in payload and m.group(1) not in ("model", "messages"):
                    payload.pop(m.group(1), None)
                    continue
                break
            if r.status_code == 401:
                raise AIError("OpenAI kaliti noto'g'ri yoki bekor qilingan (401).")
            if r.status_code in (400, 403, 404) and ("model" in (msg := _err_text(r)[0]).lower() or "access" in msg.lower()):
                last_err = f"{mdl}: {msg[:160]}"
                log.warning("OpenAI model ishlamadi: %s", last_err)
                continue                              # zaxira modelga o'tish
            if r.status_code != 200:
                raise AIError(f"OpenAI xatosi {r.status_code}: {_err_text(r)[0][:300]}")
            js = r.json()
            u = js.get("usage", {}) or {}
            tin, tout = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
            cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
            choice = js["choices"][0]
            text = (choice["message"].get("content") or "").strip()
            cost = record_usage(channel_id, purpose, mdl, tin, tout, cached, ok=bool(text), note=note)
            meta = {"model": mdl, "tokens_in": tin, "tokens_out": tout, "cost": cost}
            if not text:
                last_err = f"{mdl}: bo'sh javob ({choice.get('finish_reason')})"
                if choice.get("finish_reason") == "length" and max_out < 16000:
                    max_out *= 2
                    payload["max_completion_tokens"] = max_out
                continue
            if not json_mode:
                return text, meta
            try:
                return json.loads(text), meta
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", text, re.S)
                if m:
                    try:
                        return json.loads(m.group(0)), meta
                    except json.JSONDecodeError:
                        pass
                last_err = f"{mdl}: JSON formatida javob kelmadi"
    raise AIError(f"AI javob bera olmadi: {last_err}")


async def list_models() -> list[str]:
    if not configured():
        raise AIError("OpenAI kaliti kiritilmagan")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(API + "/models", headers={"Authorization": f"Bearer {api_key()}"})
    if r.status_code == 401:
        raise AIError("OpenAI kaliti noto'g'ri (401)")
    if r.status_code != 200:
        raise AIError(f"Modellar ro'yxati olinmadi: {r.status_code}")
    ids = [m["id"] for m in r.json().get("data", [])]
    bad = ("embedding", "audio", "tts", "whisper", "transcribe", "image", "realtime", "moderation", "dall", "search", "codex", "instruct")
    chat_ids = [i for i in ids if re.match(r"^(gpt-|o\d)", i) and not any(b in i for b in bad)]
    return sorted(set(chat_ids))


async def transcribe_openai(channel_id, path: str, lang: str | None, duration: float) -> str:
    """Audio faylni OpenAI orqali matnga aylantiradi (xarajat ham hisoblanadi)."""
    if not configured():
        raise AIError("OpenAI kaliti kiritilmagan")
    check_cap(channel_id)
    model = db.get_setting("stt_openai_model") or "whisper-1"
    data = {"model": model}
    if lang:
        data["language"] = lang
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0)) as client:
        with open(path, "rb") as f:
            r = await _post(client, "/audio/transcriptions", None, files={"file": (path.split("/")[-1].split("\\")[-1], f, "audio/wav")}, data=data)
    if r.status_code != 200:
        raise AIError(f"Transkripsiya xatosi {r.status_code}: {_err_text(r)[0][:200]}")
    record_usage(channel_id, "stt", model, audio_sec=duration, note="openai")
    return (r.json().get("text") or "").strip()
