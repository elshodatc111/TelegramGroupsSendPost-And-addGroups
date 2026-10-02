"""Kanallarim: post yozishda yordamchi — AI tahlil (matn va dizayn takliflari), vaqt taklifi, rasm uchun AI maslahat.

Muhim: foydalanuvchi yozgan matn HECH QACHON o'zgartirilmaydi. AI faqat taklif beradi, almashtirishni foydalanuvchi o'zi bosadi.
Vaqt taklifi AI'siz, kanalning haqiqiy statistikasi va Kontent rejadagi band vaqtlar asosida hisoblanadi.
"""
import json
import re
from datetime import datetime, timedelta

from . import ai, ch_ai, ch_data, ch_stats, db

FMT = "%Y-%m-%d %H:%M:%S"
WD = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
DEFAULT_HOURS = {9: 0.6, 12: 0.7, 13: 0.7, 18: 0.9, 19: 1.0, 20: 1.0, 21: 0.9}
ALLOWED = ("b", "strong", "i", "em", "u", "s", "code", "pre", "a")


def _j(v):
    try:
        return json.loads(v) if isinstance(v, str) else (v or {})
    except Exception:
        return {}


def max_per_day() -> int:
    try:
        return min(max(int(db.get_setting("ch_max_per_day", 2) or 2), 1), 10)
    except ValueError:
        return 2


# ---------------------------------------------------------------- Kontent rejadagi band vaqtlar
def busy_slots(cid: int, start: datetime, end: datetime, exclude_id: int | None = None) -> list[dict]:
    """Berilgan oraliqda rejalashtirilgan (yoki vaqti qo'yilgan qoralama) va yuborilgan postlar."""
    rows = db.q("SELECT id, title, text, scheduled_at, status FROM ch_plan WHERE channel_id=? AND scheduled_at>=? AND scheduled_at<=? "
                "AND status IN ('scheduled','draft','sent') ORDER BY scheduled_at",
                (cid, start.strftime(FMT), end.strftime(FMT)))
    out = []
    for r in rows:
        if exclude_id and r["id"] == exclude_id:
            continue
        try:
            t = datetime.strptime(r["scheduled_at"], FMT)
        except (TypeError, ValueError):
            continue
        out.append({"id": r["id"], "title": r["title"], "status": r["status"], "t": t})
    return out


def _hour_grid(cid: int):
    h = ch_stats.heatmap(cid, 0, 90)
    return h["grid"], h["max"]


def suggest_times(cid: int, exclude_id: int | None = None, days: int = 7, n: int = 3, min_gap_h: float = 3.0) -> dict:
    """Eng yaxshi n ta vaqt: kanal soatlari bo'yicha ko'rish + band vaqtlar bilan to'qnashmaslik + kunlik me'yor."""
    now = datetime.now()
    cap = max_per_day()
    grid, mx = _hour_grid(cid)
    has_data = mx > 0
    busy = busy_slots(cid, now - timedelta(hours=2), now + timedelta(days=days + 1), exclude_id)
    by_day: dict[str, list[datetime]] = {}
    for b in busy:
        by_day.setdefault(b["t"].strftime("%Y-%m-%d"), []).append(b["t"])
    cands = []
    for d in range(0, days + 1):
        day = (now + timedelta(days=d)).replace(minute=0, second=0, microsecond=0)
        key = day.strftime("%Y-%m-%d")
        slots = by_day.get(key, [])
        if len(slots) >= cap:
            continue
        for hour in range(8, 23):
            t = day.replace(hour=hour)
            if t < now + timedelta(minutes=45):
                continue
            if any(abs((t - s).total_seconds()) < min_gap_h * 3600 for s in slots):
                continue
            wd = t.weekday()
            base = (grid[wd][hour] / mx) if has_data else DEFAULT_HOURS.get(hour, 0.15)
            score = base * (0.96 ** d) * (1.0 - 0.15 * len(slots))
            cands.append((score, t, len(slots), base))
    cands.sort(key=lambda x: -x[0])
    picked, used_days = [], set()
    for score, t, cnt, base in cands:                       # avval turli kunlardan
        if t.strftime("%Y-%m-%d") in used_days:
            continue
        picked.append((score, t, cnt, base))
        used_days.add(t.strftime("%Y-%m-%d"))
        if len(picked) >= n:
            break
    for c in cands:                                         # yetmasa, qolganlardan
        if len(picked) >= n:
            break
        if all(c[1] != p[1] for p in picked) and all(abs((c[1] - p[1]).total_seconds()) >= min_gap_h * 3600 for p in picked):
            picked.append(c)
    top = max([p[0] for p in picked], default=1) or 1
    out = []
    for score, t, cnt, base in sorted(picked, key=lambda x: x[1]):
        avg = int(grid[t.weekday()][t.hour]) if has_data else None
        reason = (f"{WD[t.weekday()]} soat {t.hour:02d}:00 da kanal postlari o'rtacha {avg:,} ko'rish olgan".replace(",", " ") if avg
                  else "Kanal statistikasi yetarli emas: odatda ko'p faol soatlar tanlandi")
        reason += f"; shu kuni rejada {cnt} ta post" + (" (bo'sh)" if not cnt else "")
        out.append({"when": t.strftime("%Y-%m-%dT%H:%M"), "label": f"{t.strftime('%d.%m')} {WD[t.weekday()][:3]} {t.hour:02d}:00",
                    "score": int(round(score / top * 100)), "reason": reason})
    load = [{"when": b["t"].strftime("%d.%m %H:%M"), "title": b["title"][:50], "status": b["status"]} for b in busy if b["t"] >= now][:12]
    return {"times": out, "load": load, "data": has_data, "cap": cap}


# ---------------------------------------------------------------- AI tahlil (matn + dizayn)
REVIEW_SYSTEM = """You are a senior Telegram copy editor and conversion designer for a business channel (goal: sales and leads).
The channel owner wrote a draft post. NEVER change the owner's text yourself: you only evaluate it and propose improvements.
Judge from the real channel data given (top posts, formats, audience, tone, rubrics, already scheduled posts) and Telegram best practice
(strong first line, short paragraphs, scannable structure, one clear call to action, right length for the format).
Keep the owner's meaning and facts; never invent prices, guarantees, dates or contacts. Do not add links or phone numbers (the system appends the lead link).
If the post has media, a caption above 1024 characters is cut by Telegram: keep proposals within about 900 characters in that case.
Avoid repeating the topic/angle of the already scheduled posts. Write all commentary in Uzbek (Latin). The proposed texts stay in the language of the draft.
Return ONLY JSON:
{"score": {"hook": 0-10, "clarity": 0-10, "cta": 0-10, "structure": 0-10, "fit": 0-10 (fit with this channel's tone and audience), "overall": 0-10},
 "summary": "2-3 sentences: main strength and the main thing to fix",
 "suggestions": [{"area": "hook|length|cta|structure|tone|design|offer", "priority": "high|medium|low", "issue": "what is weak (quote a short fragment)", "fix": "exactly what to change"}],
 "improved": "a full improved version of the post as plain text, same language",
 "improved_html": "the same improved post with Telegram HTML formatting: <b> for the hook/key facts, <i> for secondary notes, short paragraphs, a few fitting emojis (not more than 4), list lines with simple symbols; NO other tags except <b>,<i>,<u>,<s>,<code>,<a href>",
 "hashtags": ["up to 4 fitting hashtags or empty"],
 "best_format": "text|photo|video and one short reason in Uzbek",
 "warnings": ["limit/format problems, e.g. too long for a caption; empty if none"]}
Give 3-6 suggestions, most important first. If the draft is already strong, say so and keep the suggestions minor."""


def _schedule_context(cid: int, exclude_id: int | None) -> str:
    now = datetime.now()
    rows = busy_slots(cid, now, now + timedelta(days=14), exclude_id)
    if not rows:
        return "ALREADY SCHEDULED POSTS (next 14 days): none"
    lines = []
    for r in rows[:15]:
        t = db.one("SELECT text FROM ch_plan WHERE id=?", (r["id"],))
        lines.append(f"- {r['t'].strftime('%Y-%m-%d %H:%M')} [{r['status']}] {r['title']}: {ch_ai._clip(t['text'] if t else '', 160)}")
    return "ALREADY SCHEDULED POSTS (next 14 days):\n" + "\n".join(lines)


async def review_post(cid: int, text: str, title: str = "", media_n: int = 0, parse_mode: str = "none", plan_id: int | None = None,
                      when: str = "") -> dict:
    text = (text or "").strip()
    if len(text) < 15:
        raise ai.AIError("Tahlil uchun avval post matnini yozing (kamida bir-ikki jumla)")
    ch = ch_data.get(cid)
    ctx = ch_ai.build_context(ch, days=30, top_n=4, recent_n=2)
    user = (f"{ctx}\n\n{_schedule_context(cid, plan_id)}\n\n=== DRAFT POST (owner's text) ===\n"
            f"Title (internal): {title or '-'}\nMedia attached: {media_n or 'none'}\nFormatting mode: {parse_mode}\n"
            f"Planned time: {when or 'not set'}\nLength: {len(text)} characters\n---\n{text[:3500]}")
    data, meta = await ai.chat(cid, "analysis", REVIEW_SYSTEM, user, max_out=3500, note="post tahlili")
    if not isinstance(data, dict):
        raise ai.AIError("AI kutilgan formatda javob bermadi. Qayta urinib ko'ring.")
    data["improved_html"] = sanitize_html(str(data.get("improved_html") or ""))
    data["improved"] = str(data.get("improved") or "")
    sc = data.get("score") if isinstance(data.get("score"), dict) else {}
    data["score"] = {k: max(0, min(10, float(sc.get(k, 0) or 0))) for k in ("hook", "clarity", "cta", "structure", "fit", "overall")}
    data["suggestions"] = [s for s in (data.get("suggestions") or []) if isinstance(s, dict)][:8]
    limit = 1024 if media_n else 4096
    if len(text) > limit:
        data.setdefault("warnings", []).insert(0, f"Matn {len(text)} belgi: {'rasm/video ostida' if media_n else 'post uchun'} limit {limit}")
    data["times"] = suggest_times(cid, plan_id)
    data["cost"] = meta.get("cost")
    return data


def sanitize_html(s: str) -> str:
    """Telegram uchun ruxsat etilgan teglardan boshqasini olib tashlaydi."""
    def keep(m):
        tag = m.group(2).lower()
        if tag not in ALLOWED:
            return ""
        if m.group(1) == "/":
            return f"</{tag}>"
        if tag == "a":
            h = re.search(r'href\s*=\s*"(https?://[^"\s<>]+)"', m.group(0))
            return f'<a href="{h.group(1)}">' if h else ""
        return f"<{tag}>"
    return re.sub(r"<(/?)([a-zA-Z0-9]+)[^>]*>", keep, s or "")


# ---------------------------------------------------------------- rasm uchun AI maslahat
IMAGE_SYSTEM = """You are an art director for a business Telegram channel in Uzbekistan (goal: sales and leads).
Using the channel's real data (what formats and posts got the most views, audience, tone, brand colours, image style) and, if given, the post text,
explain what kind of post image will work and write ready prompts for a LOCAL text-to-image model (Stable Diffusion Turbo).
The model draws ONLY the background picture: all text (headline, phone, address) is added later by the system, so prompts must contain NO text, letters, numbers,
logos, watermarks and no close-up real faces. Prompts: English, under 60 words, comma-separated phrases, one clear subject, simple composition with empty space for text,
lighting and mood that fit the brand colours. Headline max 7 words, subline max 12 words, in the language of the post (or Uzbek Latin if no post is given).
Write the analysis in Uzbek (Latin). Return ONLY JSON:
{"channel_insight": "3-4 sentences: what the channel's data says about visuals/formats that work and what the audience responds to",
 "visual_direction": "2-3 sentences: the recommended look for this post (subject, mood, colours, composition)",
 "options": [{"name": "short Uzbek name", "image_prompt": "...", "headline": "...", "subline": "...", "layout": "bottom|center|split", "why": "1-2 sentences in Uzbek"}],
 "avoid": ["what to avoid in the image"]}
Give exactly 3 options that differ clearly in concept."""


async def image_advice(cid: int, text: str = "", title: str = "") -> dict:
    ch = ch_data.get(cid)
    style = (ch["img_style"] or "").strip()
    cols = ", ".join(ch_data.colors(ch))
    ctx = ch_ai.build_context(ch, days=30, top_n=5, recent_n=2)
    user = (f"{ctx}\n\nBRAND COLOURS: {cols}\n" + (f"PREFERRED IMAGE STYLE: {style}\n" if style else "") +
            (f"\n=== POST ===\nTitle: {title}\n{text[:1800]}" if (text or "").strip() else "\n(No post selected: advise for the channel in general.)"))
    data, meta = await ai.chat(cid, "analysis", IMAGE_SYSTEM, user, max_out=2800, note="rasm maslahati")
    if not isinstance(data, dict) or not data.get("options"):
        raise ai.AIError("AI kutilgan formatda javob bermadi. Qayta urinib ko'ring.")
    opts = []
    for o in data["options"][:3]:
        if not isinstance(o, dict) or not o.get("image_prompt"):
            continue
        lay = o.get("layout") if o.get("layout") in ("bottom", "center", "split") else "bottom"
        opts.append({"name": str(o.get("name", ""))[:60], "image_prompt": str(o["image_prompt"])[:600], "headline": str(o.get("headline", ""))[:80],
                     "subline": str(o.get("subline", ""))[:120], "layout": lay, "why": str(o.get("why", ""))[:300]})
    if not opts:
        raise ai.AIError("AI rasm variantlarini yozib bera olmadi")
    data["options"] = opts
    data["cost"] = meta.get("cost")
    return data
