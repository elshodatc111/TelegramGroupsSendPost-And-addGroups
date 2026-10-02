"""Zoom Online: o'qituvchilar uchun Telegram boti (Bot API, uzoq so'rov / long polling).

Bot faqat o'qituvchilar uchun:
  /start             — @username bo'yicha o'qituvchini taniydi va chat'ini bog'laydi
  📅 Bugungi darslar  — bugungi darslar (vaqt, guruh, holat)
  🗓 Haftalik jadval  — 7 kunlik jadval
  👥 Guruhlarim       — biriktirilgan guruhlar va ularning jadvali
  🔗 Hozirgi dars     — faol (yoki eng yaqin) darsning «Darsni boshlash» va o'quvchilar havolasi
Avtomatik xabarlar (meet_sched yuboradi): dars boshlanishidan 20 va 10 daqiqa oldin eslatma, boshlash havolasi,
majlis tugashiga 3 daqiqa qolganda «Davom etish / Bekor qilish», bekor qilish va ko'chirish xabarlari.
"""
import asyncio
import re
from datetime import timedelta

from . import db, meet_sched as S, meet_zoom as Z
from .config import log

MENU = {"keyboard": [[{"text": "📅 Bugungi darslar"}, {"text": "🗓 Haftalik jadval"}],
                     [{"text": "👥 Guruhlarim"}, {"text": "🔗 Hozirgi dars"}], [{"text": "❓ Yordam"}]],
        "resize_keyboard": True, "is_persistent": True}
ICON = {"planned": "🕒", "live": "🟢", "done": "✅", "missed": "⚠️", "cancelled": "❌"}


class BotFail(Exception):
    pass


def token() -> str:
    return (S.get("bot_token") or "").strip()


async def call(method: str, params: dict | None = None, timeout: float = 30.0):
    try:
        return await Z.bot_call(token(), method, params, timeout)
    except Z.BotError as e:
        raise BotFail(Z.redact(e.msg)) from e


# ---------------------------------------------------------------- yuborish
async def send_teacher(t: dict, html: str, buttons: list | None = None) -> dict:
    """O'qituvchiga bot orqali xabar yuboradi. Yetmasa BotFail (sababi bilan)."""
    t = S.teacher(t["id"]) or t
    if not token():
        raise BotFail("bot tokeni kiritilmagan (Sozlamalar)")
    if not t.get("chat_id"):
        raise BotFail(f"{t['name']} botga ulanmagan (botga /start yuborishi kerak)")
    params = {"chat_id": t["chat_id"], "text": html, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    try:
        return await Z.bot_call(token(), "sendMessage", params)
    except Z.BotError as e:
        msg = e.msg
        if e.code == 403 or "blocked" in msg.lower() or "deactivated" in msg.lower():
            db.ex("UPDATE zoom_teachers SET chat_id=NULL, bot_linked_at=NULL WHERE id=?", (t["id"],))
            raise BotFail(f"{t['name']} botni bloklagan yoki akkaunti yo'q (qayta /start bosishi kerak)")
        if buttons and ("BUTTON_URL_INVALID" in msg or "button" in msg.lower() or "too long" in msg.lower()):
            # URL tugma qabul qilinmadi (masalan, juda uzun): havolalarni matn ichiga qo'yamiz
            extra = "\n\n" + "\n".join(f'• <a href="{Z.esc(b["url"])}">{Z.esc(b["text"])}</a>' for row in buttons for b in row if b.get("url"))
            params.pop("reply_markup", None)
            keep = [row for row in buttons if any("callback_data" in b for b in row)]
            if keep:
                params["reply_markup"] = {"inline_keyboard": keep}
            params["text"] = html + extra
            try:
                return await Z.bot_call(token(), "sendMessage", params)
            except Z.BotError as e2:
                raise BotFail(Z.redact(e2.msg))
        raise BotFail(Z.redact(msg))


async def _reply(chat_id, html, keyboard=True, buttons=None):
    p = {"chat_id": chat_id, "text": html, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": buttons}
    elif keyboard:
        p["reply_markup"] = MENU
    try:
        await Z.bot_call(token(), "sendMessage", p)
    except Z.BotError as e:
        log.info("Bot javob yubora olmadi: %s", Z.redact(e.msg))


# ---------------------------------------------------------------- o'qituvchi menyulari
def _mine(t: dict, a: str, b: str) -> list[dict]:
    return [l for l in S.lessons_between(a, b) if (S.lesson_teacher(l) or {}).get("id") == t["id"]]


def _line(l: dict) -> str:
    return f"{ICON.get(l['status'], '•')} <b>{l['hhmm']}–{l['end_hhmm']}</b> · {Z.esc(l['group_title'])} ({l['duration_min']} daq){' · ' + l['status_uz'] if l['status'] in ('cancelled', 'missed') else ''}"


def txt_today(t) -> str:
    d = S.now().replace(hour=0, minute=0, second=0, microsecond=0)
    ls = _mine(t, S.fmt(d), S.fmt(d + timedelta(days=1)))
    if not ls:
        return "📅 Bugun sizda dars yo'q."
    return f"📅 <b>Bugungi darslar</b> ({d:%d.%m})\n\n" + "\n".join(_line(l) for l in ls)


def txt_week(t) -> str:
    d = S.now().replace(hour=0, minute=0, second=0, microsecond=0)
    ls = _mine(t, S.fmt(d), S.fmt(d + timedelta(days=7)))
    if not ls:
        return "🗓 Keyingi 7 kunda sizda dars yo'q."
    out, cur = ["🗓 <b>Haftalik jadval</b>"], None
    for l in ls:
        if l["day"] != cur:
            cur = l["day"]
            out.append(f"\n<b>{S.WEEKDAYS[S.parse(l['start_at']).weekday()]}, {l['day'][8:]}.{l['day'][5:7]}</b>")
        out.append(_line(l))
    return "\n".join(out)


def txt_groups(t) -> str:
    gs = [g for g in S.groups(True) if g.get("teacher_id") == t["id"]]
    sched_gs = {r["group_id"] for r in db.q("SELECT DISTINCT group_id FROM zoom_schedule WHERE teacher_id=? AND active=1", (t["id"],))}
    for g in S.groups(True):
        if g["id"] in sched_gs and g not in gs:
            gs.append(g)
    if not gs:
        return "👥 Sizga hali guruh biriktirilmagan."
    out = ["👥 <b>Guruhlarim</b>"]
    for g in gs:
        rows = [f"{S.WD_SHORT[s['weekday']]} {s['start_time']} ({s['duration_min']} daq)" for s in S.schedules(g["id"]) if s["active"]]
        out.append(f"\n<b>{Z.esc(g['title'])}</b>\n" + (", ".join(rows) if rows else "jadval kiritilmagan"))
    return "\n".join(out)


async def cmd_current(t, chat_id):
    now = S.now()
    ls = _mine(t, S.fmt(now - timedelta(hours=3)), S.fmt(now + timedelta(hours=3)))
    live = [l for l in ls if l["status"] in ("live", "planned") and S.parse(l["end_at"]) >= now and l.get("cur")]
    if not live:
        nxt = [l for l in ls if l["status"] == "planned" and S.parse(l["start_at"]) >= now]
        if nxt:
            await _reply(chat_id, f"Hozir faol dars yo'q.\nEng yaqin dars:\n{_line(nxt[0])}\nBoshlash havolasi dars boshlanishidan oldin yuboriladi.")
        else:
            await _reply(chat_id, "Hozir faol dars yo'q.")
        return
    l = sorted(live, key=lambda x: x["start_at"])[0]
    grp = S.group(l["group_id"])
    txt, btn = S.msg_teacher_link(l, grp, l["cur"], max(0, int((S.parse(l["start_at"]) - now).total_seconds() // 60)), cont=l["cur"]["seq"] > 1)
    try:
        await send_teacher(t, txt, btn)
    except BotFail as e:
        await _reply(chat_id, f"Havolani yuborib bo'lmadi: {Z.esc(str(e))}")


HELP = ("❓ <b>Yordam</b>\n\nBu bot dars vaqtida Zoom havolalarini avtomatik beradi:\n"
        "• dars boshlanishidan 20 va 10 daqiqa oldin eslatma;\n"
        "• 10 daqiqa oldin «Darsni boshlash» tugmasi: u sizni Zoom'da host qilib kiritadi (alohida Zoom akkaunt kerak emas);\n"
        "• bepul Zoom majlisi 40 daqiqada tugaydi: tugashiga 3 daqiqa qolganda «Davom etasizmi?» deb so'raladi. "
        "<b>Davom etish</b> bosilsa yangi havola sizga va guruhga yuboriladi; <b>Bekor qilish</b> bosilsa yoki javob bermasangiz, dars shu majlis bilan tugaydi.\n\n"
        "Menyu: bugungi darslar, haftalik jadval, guruhlaringiz va hozirgi dars havolasi.")


# ---------------------------------------------------------------- yangilanishlarni qayta ishlash
async def handle_start(msg: dict):
    chat = msg["chat"]["id"]
    frm = msg.get("from") or {}
    un = frm.get("username") or ""
    if not un:
        await _reply(chat, "Telegram profilingizda <b>@username</b> yo'q. Sozlamalar → Username orqali o'rnating va /start ni qayta yuboring.", keyboard=False)
        return
    t = S.teacher_by_username(un)
    if not t:
        await _reply(chat, f"@{Z.esc(un)} o'qituvchilar ro'yxatida topilmadi. Administratorga ayting: «Zoom Online → O'qituvchilar» bo'limiga sizni "
                           f"<b>@{Z.esc(un)}</b> bilan qo'shsin, so'ng /start ni qayta yuboring.", keyboard=False)
        return
    db.ex("UPDATE zoom_teachers SET chat_id=?, bot_linked_at=? WHERE id=?", (chat, S.fmt(S.now()), t["id"]))
    await _reply(chat, f"✅ Salom, <b>{Z.esc(t['name'])}</b>! Bot ulandi. Endi dars vaqti kelganda havolalar shu yerga keladi.\n\n" + txt_today(t))


async def handle_message(msg: dict):
    chat = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if msg["chat"].get("type") != "private":
        return
    if text.startswith("/start"):
        return await handle_start(msg)
    r = db.one("SELECT * FROM zoom_teachers WHERE chat_id=?", (chat,))
    if not r:
        await _reply(chat, "Avval /start ni yuboring.", keyboard=False)
        return
    t = dict(r)
    low = text.lower()
    if low.startswith(("/help", "❓")):
        await _reply(chat, HELP)
    elif "bugungi" in low or low.startswith("/today"):
        await _reply(chat, txt_today(t))
    elif "haftalik" in low or low.startswith("/week"):
        await _reply(chat, txt_week(t))
    elif "guruh" in low or low.startswith("/groups"):
        await _reply(chat, txt_groups(t))
    elif "hozirgi" in low or low.startswith("/now"):
        await cmd_current(t, chat)
    else:
        await _reply(chat, "Menyudan tanlang yoki /help ni yuboring.")


async def handle_callback(cq: dict):
    cid = cq["id"]
    data = cq.get("data") or ""
    chat = (cq.get("message") or {}).get("chat", {}).get("id")
    mid_msg = (cq.get("message") or {}).get("message_id")
    m = re.fullmatch(r"z:([yn]):(\d+)", data)

    async def ans(text, alert=False):
        try:
            await Z.bot_call(token(), "answerCallbackQuery", {"callback_query_id": cid, "text": text[:190], "show_alert": alert})
        except Z.BotError:
            pass

    async def edit(text):
        if chat and mid_msg:
            try:
                await Z.bot_call(token(), "editMessageText", {"chat_id": chat, "message_id": mid_msg, "text": text, "parse_mode": "HTML",
                                                              "reply_markup": {"inline_keyboard": []}})
            except Z.BotError:
                pass
    if not m:
        return await ans("Noma'lum tugma")
    yes, seg_id = m.group(1) == "y", int(m.group(2))
    seg = S.meeting(seg_id)
    les = S.lesson(seg["lesson_id"]) if seg and seg.get("lesson_id") else None
    t = db.one("SELECT * FROM zoom_teachers WHERE chat_id=?", (chat,))
    owner = S.lesson_teacher(les) if les else None
    if not seg or not les or not t or not owner or owner["id"] != t["id"]:
        return await ans("Bu so'rov sizga tegishli emas", True)
    if seg["prompt"] in ("yes", "no") and seg["prompt"] == ("yes" if yes else "no"):
        return await ans("Allaqachon qabul qilingan")
    grp = S.group(les["group_id"])
    if yes:
        await ans("Yangi majlis yaratilmoqda…")
        ok, why = await S.continue_lesson(seg_id, "teacher")
        if ok:
            await edit(f"✅ <b>{Z.esc(grp['title'])}</b>: dars davom etmoqda. Yangi havola yuborildi.")
        else:
            await edit(f"⚠️ <b>{Z.esc(grp['title'])}</b>: {Z.esc(why.split(': ', 1)[-1])}\nAvtomatik qayta uriniladi, administratorga xabar yuborildi.")
    else:
        await S.stop_lesson(seg_id)
        await ans("Dars davom ettirilmaydi")
        await edit(f"❌ <b>{Z.esc(grp['title'])}</b>: dars bu majlis bilan tugaydi.")


async def handle_update(u: dict):
    if u.get("callback_query"):
        await handle_callback(u["callback_query"])
    elif u.get("message"):
        await handle_message(u["message"])


# ---------------------------------------------------------------- sikl
async def verify() -> dict:
    """Bot tokenini tekshiradi (getMe) va natijani saqlaydi."""
    try:
        me = await Z.bot_call(token(), "getMe", None, 15)
        S.put("bot_username", me.get("username", ""))
        S.drop("bot_err")
        return me
    except Z.BotError as e:
        S.put("bot_err", Z.redact(e.msg))
        raise BotFail(Z.redact(e.msg))


async def loop():
    """Fon sikli (supervise ostida): o'qituvchi xabarlarini qabul qiladi."""
    await asyncio.sleep(5)
    offset, verified_for, fails = 0, None, 0
    while True:
        try:
            S.ready()
            tok = token()
            if not tok:
                verified_for = None
                await asyncio.sleep(15)
                continue
            if verified_for != tok:
                await verify()
                verified_for = tok
                offset = 0
            ups = await Z.bot_call(tok, "getUpdates", {"offset": offset, "timeout": 20, "allowed_updates": ["message", "callback_query"]}, timeout=40)
            fails = 0
            for u in ups:
                offset = max(offset, u["update_id"] + 1)
                try:
                    await handle_update(u)
                except Exception:
                    log.exception("meet_bot.handle_update")
            S.put("bot_beat", S.fmt(S.now()))
            if not ups:
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except (Z.BotError, BotFail) as e:
            fails += 1
            msg = Z.redact(getattr(e, "msg", None) or str(e))
            S.put("bot_err", msg)
            if getattr(e, "code", 0) in (401, 404):          # token noto'g'ri: tez-tez urinmaymiz
                verified_for = None
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(min(60, 3 * fails))
        except Exception as e:
            log.exception("meet_bot.loop")
            S.put("bot_err", Z.redact(f"{type(e).__name__}: {e}"))
            await asyncio.sleep(10)
