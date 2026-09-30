"""Kanallarim: AI tahlil, kunlik g'oya, post taklifi, video ssenariy va bilimni boyitib borish (OpenAI)."""
import asyncio
import json
from datetime import datetime, timedelta

from . import ai, ch_data, ch_stats, db, transcribe
from .config import log

FMT = "%Y-%m-%d %H:%M:%S"

LANG_NAMES = {"uz": "Uzbek (Latin script, natural modern Uzbek as spoken in Uzbekistan)",
              "uz_cyrl": "Uzbek (Cyrillic script)", "ru": "Russian (natural, as spoken in Uzbekistan)"}


def _clip(t: str, n: int) -> str:
    t = " ".join((t or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def _langs(lang: str, ch) -> list[str]:
    """So'ralgan til -> kalitlar ro'yxati. 'both' = uz + ru. Kanal uz_cyrl bo'lsa, 'uz' kalit kirillda yoziladi."""
    if lang == "both" or (lang in ("", None) and (ch["lang"] or "") == "mixed"):
        return ["uz", "ru"]
    if lang in ("uz", "ru"):
        return [lang]
    return ["ru"] if (ch["lang"] or "uz") == "ru" else ["uz"]


def _uz_script(ch) -> str:
    return "uz_cyrl" if (ch["lang"] or "") == "uz_cyrl" else "uz"


# ---------------------------------------------------------------- kontekst
def profile_text(ch) -> str:
    b = ch_data.biz(ch)
    lines = [f"CHANNEL: {ch['title']} (@{ch['username'] or 'private'}), subscribers: {ch['members'] or '?'}",
             f"About (Telegram bio): {_clip(ch['about'], 400)}"]
    for label, val in (("Purpose", ch["purpose"]), ("Audience", ch["audience"]), ("Tone of voice", ch["tone"]),
                       ("Posting frequency", ch["post_freq"]), ("Rubrics", "; ".join(ch_data.rubrics(ch))),
                       ("Standard CTA", ch["cta"]), ("Forbidden topics/words", ch["bans"]),
                       ("Manual description of the channel's video format", ch["video_format"])):
        if (val or "").strip():
            lines.append(f"{label}: {_clip(val, 500)}")
    for k, label in ch_data.BIZ_FIELDS:
        if b.get(k):
            lines.append(f"Business / {label}: {_clip(b[k], 400)}")
    if (ch["samples"] or "").strip():
        lines.append("Sample posts written by the channel owner (style reference):\n" + _clip(ch["samples"], 1500))
    return "\n".join(lines)


def stats_text(s: dict) -> str:
    mix = ", ".join(f"{ch_stats.MEDIA_LABELS.get(k, k)} {v['pct']}%" for k, v in s["mix"].items())
    bh = ", ".join(f"{h['hour']:02d}:00" for h in s["best_hours"]) or "-"
    return (f"posts/day {s['per_day']}, avg views {s['avg_views']} (ER {s['er']}%), formats: {mix or '-'}, "
            f"avg video {s['avg_video_sec']}s, links in {s['link_share']}% posts, avg text length {s['avg_len']}, best hours {bh}")


def _post_line(p, with_tr=False) -> str:
    kind = ch_stats.MEDIA_LABELS.get(p["media"], p["media"])
    dur = f" {p['duration']}s" if p["media"] == "video" and p["duration"] else ""
    s = f"- [{p['date'][:10]}] {kind}{dur} | {p['views']} views, ER {p.get('err', '?')}% | {_clip(p['text'], 320)}"
    if with_tr and p["transcript"]:
        s += f"\n    (video transcript: {_clip(p['transcript'], 450)})"
    return s


def build_context(ch, days=30, top_n=5, recent_n=4) -> str:
    cid = ch["id"]
    parts = [profile_text(ch)]
    mem = ch_data.ensure_memory(cid)
    if (mem["memo"] or "").strip():
        parts.append("ACCUMULATED KNOWLEDGE ABOUT THIS NICHE/COMPETITORS (from earlier analyses):\n" + mem["memo"])
    if (mem["video_summary"] or "").strip():
        parts.append("THIS CHANNEL'S VIDEO FORMAT (learned from its own videos):\n" + mem["video_summary"])
    own = ch_stats.source_stats(cid, 0, days)
    parts.append(f"OWN CHANNEL, last {days} days: " + stats_text(own))
    tops = ch_stats.top_posts(cid, 0, days, top_n)
    if tops:
        parts.append("Own best posts:\n" + "\n".join(_post_line(p, True) for p in tops))
    for c in ch_data.competitors(cid):
        s = ch_stats.source_stats(cid, c["id"], days)
        if not s["posts"]:
            continue
        block = [f"COMPETITOR: {c['title']} (@{c['username'] or 'private'}), {c['members'] or '?'} subs — " + stats_text(s)]
        t = ch_stats.top_posts(cid, c["id"], days, top_n)
        if t:
            block.append("Top posts (by views/subscribers):\n" + "\n".join(_post_line(p, True) for p in t))
        rec = [dict(r, err="") for r in ch_stats.posts(cid, c["id"], 3)[:recent_n]]
        if rec:
            block.append("Latest posts (what they publish right now):\n" + "\n".join(_post_line(p) for p in rec))
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def ideas_history(cid, n=15) -> str:
    rows = db.q("SELECT title, status, feedback, created_at FROM ch_ideas WHERE channel_id=? AND kind IN ('daily','manual') "
                "ORDER BY id DESC LIMIT ?", (cid, n))
    if not rows:
        return "(no previous ideas)"
    return "\n".join(f"- {r['created_at'][:10]} [{r['status']}] {r['title']}" + (f" — owner feedback: {_clip(r['feedback'], 160)}" if r["feedback"] else "")
                     for r in rows)


# ---------------------------------------------------------------- g'oya
IDEA_SYSTEM = """You are a senior Telegram content strategist and sales copywriter working for a business owner in Uzbekistan.
The owner runs a Telegram channel whose real goal is to grow the business: more sales and maximum leads. You analyse the
channel's competitors from real data and produce ONE fresh, concrete idea that makes this channel stand out from them.

Rules:
- Ground every claim in the provided data (competitor posts, numbers). Never invent statistics. If the data is thin, say so in "why".
- Differentiate: do what competitors do NOT do or do badly, while keeping what demonstrably works for them (high views/subscribers).
- Do not copy competitor texts. Do not repeat earlier ideas (see the history); follow what the owner accepted, avoid what was rejected.
- Respect the channel's tone, forbidden topics and business facts. Do not make up prices, guarantees, licences, or results.
- The post must drive the reader toward the business goal (sales / leads) with a clear, natural call to action. Do NOT add any
  contact links or platform URLs yourself: the system appends the channel's lead link to the post automatically.
- Write each requested language natively (no literal translation feel). Uzbek and Russian texts must be correct and natural.
- Telegram limits: a post with a photo/video caption must be at most 900 characters; a text-only post at most 3500.
- Video script: it must match the video format this channel already publishes (duration, pace, style, who speaks, text on screen),
  unless the data shows a clearly better-performing format, in which case explain the change in "video.style_note".
  Make the script complete and shootable: hook in the first 3 seconds, scene-by-scene visuals, the exact spoken words (voiceover),
  on-screen text, closing CTA, caption and a simple shooting note. No vague advice.
Return ONLY a JSON object with this exact structure:
{
 "title": "short idea name (in the first requested language)",
 "why": ["2-5 short evidence-based reasons (mention competitor names / numbers)"],
 "differentiation": "how this stands out from the competitors",
 "sales_angle": "how it moves the reader toward a purchase / lead",
 "format": "text | photo | video | album | poll",
 "best_time": "HH:MM plus a one-line reason (based on the data)",
 "post": {"<lang>": "full ready-to-publish post text"},
 "hashtags": ["optional", "hashtags"],
 "video": {
   "needed": true or false,
   "duration_sec": number,
   "style_note": "what format/style and why",
   "hook": {"<lang>": "first 3 seconds"},
   "scenes": [{"time": "0-3s", "visual": "what we see", "voiceover": {"<lang>": "exact words"}, "on_screen": {"<lang>": "text on screen"}}],
   "cta_line": {"<lang>": "closing line"},
   "caption": {"<lang>": "caption under the video"},
   "shooting_note": "props, location, music, editing tips"
 }
}
"<lang>" keys are exactly the language codes requested by the user message (uz and/or ru). If video is not needed, set video.needed=false and leave the other video fields empty."""


def _append_footer(text: str, ch) -> str:
    foot = ch_data.lead_footer(ch)
    if not text or not foot:
        return text
    if ch["lead_url"] and ch["lead_url"] in text:
        return text
    return text.rstrip() + "\n\n" + foot


async def generate_idea(cid: int, lang: str = "", want_video: str = "auto", note: str = "") -> int:
    """So'rov bo'yicha bitta g'oya yaratadi. want_video: auto | yes | no."""
    ch = ch_data.get(cid)
    if not ch:
        raise ai.AIError("Kanal topilmadi")
    langs = _langs(lang, ch)
    names = [f"{k}: {LANG_NAMES['uz_cyrl' if (k == 'uz' and _uz_script(ch) == 'uz_cyrl') else k]}" for k in langs]
    vid_rule = {"yes": "The owner explicitly asks for a VIDEO idea with a full script.",
                "no": "The owner wants a post WITHOUT video (video.needed=false).",
                "auto": "Decide whether a video is the best format for this idea; if the channel regularly publishes videos and "
                        "they perform well, prefer video."}.get(want_video, "")
    ctx = build_context(ch)
    user = (f"{ctx}\n\n=== PREVIOUS IDEAS (do not repeat) ===\n{ideas_history(cid)}\n\n=== TASK ===\n"
            f"Create ONE new idea for today. Languages (JSON keys): {', '.join(names)}.\n{vid_rule}\n"
            f"Today is {datetime.now():%Y-%m-%d, %A}.\n"
            + (f"Owner's note for this request: {_clip(note, 500)}\n" if note.strip() else ""))
    data, meta = await ai.chat(cid, "idea", IDEA_SYSTEM, user, max_out=9000, note="kunlik g'oya")
    if not isinstance(data, dict) or not data.get("title"):
        raise ai.AIError("AI kutilgan formatda javob bermadi. Qayta urinib ko'ring.")
    post = data.get("post") or {}
    if isinstance(post, str):
        post = {langs[0]: post}
    data["post"] = {k: _append_footer(str(v), ch) for k, v in post.items()}
    data["langs"] = langs
    iid = db.ex("INSERT INTO ch_ideas(channel_id,created_at,kind,lang,want_video,user_note,title,body_json,status,model,tokens_in,tokens_out,cost) "
                "VALUES(?,?,?,?,?,?,?,?,'new',?,?,?,?)",
                (cid, db.now(), "manual", ",".join(langs), 1 if want_video == "yes" else 0, note[:1000], str(data["title"])[:400],
                 json.dumps(data, ensure_ascii=False), meta["model"], meta["tokens_in"], meta["tokens_out"], meta["cost"]))
    ch_data.log_event(cid, "idea", f"#{iid} {data['title']}")
    return iid


def idea_body(row) -> dict:
    try:
        return json.loads(row["body_json"] or "{}")
    except Exception:
        return {}


# ---------------------------------------------------------------- tahlil hisoboti
REPORT_SYSTEM = """You are a competitive-intelligence analyst for Telegram channels. From the real data provided, write an analysis report
for the channel owner (business goal: more sales and leads). Be specific and evidence-based; do not invent numbers.
Write in Uzbek (Latin script) unless told otherwise. Return ONLY JSON:
{
 "summary": "4-6 sentence executive summary",
 "competitors": [{"name": "...", "strategy": "what they do", "strengths": ["..."], "weaknesses": ["..."], "best_formats": "..."}],
 "what_works": ["patterns that get high views/subscribers across the niche"],
 "gaps": ["opportunities nobody covers / covers badly"],
 "own_channel": {"strengths": ["..."], "weaknesses": ["..."]},
 "actions": [{"priority": 1, "action": "concrete next step", "why": "evidence"}],
 "content_mix": "recommended weekly mix of formats and rubrics (with video share)"
}"""


async def analyze_report(cid: int) -> int:
    ch = ch_data.get(cid)
    if not ch_data.competitors(cid):
        raise ai.AIError("Avval raqobatchi kanallarni biriktiring")
    ctx = build_context(ch, days=30, top_n=6, recent_n=5)
    lang_note = "Write in Russian." if (ch["lang"] or "") == "ru" else ""
    data, meta = await ai.chat(cid, "analysis", REPORT_SYSTEM, ctx + "\n\n=== TASK ===\nWrite the analysis report. " + lang_note,
                               max_out=7000, note="tahlil hisoboti")
    iid = db.ex("INSERT INTO ch_ideas(channel_id,created_at,kind,lang,title,body_json,status,model,tokens_in,tokens_out,cost) "
                "VALUES(?,?,?,?,?,?,'report',?,?,?,?)",
                (cid, db.now(), "report", "uz", "Tahlil hisoboti " + datetime.now().strftime("%Y-%m-%d"),
                 json.dumps(data, ensure_ascii=False), meta["model"], meta["tokens_in"], meta["tokens_out"], meta["cost"]))
    return iid


# ---------------------------------------------------------------- bilimni boyitib borish
MEMO_SYSTEM = """You maintain a compact, continuously improving knowledge memo about a Telegram channel's niche and its competitors.
You get the previous memo and fresh data. Merge them: keep what is still true and useful, drop stale/contradicted points, add new
evidence-based insights. Be concrete (names, numbers, formats, hooks, posting times, topics). Max ~700 words, in Uzbek (Latin).
Use these sections (Markdown headings): "Raqobatchilar strategiyasi", "Ishlayotgan formatlar va hooklar", "Bo'sh nishalar",
"Bizning kanal: kuchli va zaif tomonlar", "Tavsiya etilgan yo'nalishlar", "Video formatlar".
Return ONLY JSON: {"memo": "<markdown text>"}"""


async def learn(cid: int, force: bool = False) -> bool:
    """Yangi ma'lumotlardan xotirani (memo) yangilaydi. Arzon model ishlatiladi. Yangi post kam bo'lsa o'tkazib yuboradi."""
    ch = ch_data.get(cid)
    if not ch or not ch_data.competitors(cid):
        return False
    mem = ch_data.ensure_memory(cid)
    seen = mem["seen_ts"] or (datetime.now() - timedelta(days=30)).strftime(FMT)
    fresh = db.one("SELECT COUNT(*) c, MAX(date) d FROM ch_posts WHERE channel_id=? AND date>?", (cid, seen))
    if not force and (fresh["c"] or 0) < 10:
        return False
    ctx = build_context(ch, days=30, top_n=4, recent_n=3)
    user = (f"{ctx}\n\n=== TASK ===\nUpdate the knowledge memo. {fresh['c']} new posts appeared since the last update "
            f"(memo version {mem['version']}). The 'ACCUMULATED KNOWLEDGE' above is the previous memo.")
    data, _ = await ai.chat(cid, "memo", MEMO_SYSTEM, user, model=ai.model_for("analysis"), max_out=3500, note="bilim yangilash")
    memo = (data or {}).get("memo") if isinstance(data, dict) else None
    if not memo:
        return False
    db.ex("UPDATE ch_memory SET memo=?, updated_at=?, version=version+1, seen_ts=? WHERE channel_id=?",
          (memo if isinstance(memo, str) else json.dumps(memo, ensure_ascii=False), db.now(), fresh["d"] or db.now(), cid))
    ch_data.log_event(cid, "learn", f"memo v{mem['version'] + 1}")
    return True


VIDEO_SYSTEM = """You study the video format of a Telegram channel from real data: captions, durations, view counts and (when available)
speech transcripts of its own best videos. Describe the channel's established video format so that new scripts can match it.
Write in Uzbek (Latin), max 350 words. Cover: typical duration and pace, structure (hook / body / CTA) with examples, who speaks
and how, language mix (Uzbek/Russian), on-screen text and visual style, what performs best, what is weak.
Return ONLY JSON: {"summary": "<text>"}"""


async def learn_video_format(cid: int, n: int = 4) -> str:
    """O'z kanalning eng yaxshi videolarini (matnga o'girib) o'rganadi va video format xulosasini saqlaydi."""
    ch = ch_data.get(cid)
    vids = db.q("SELECT * FROM ch_posts WHERE channel_id=? AND comp_id=0 AND media='video' ORDER BY views DESC LIMIT ?", (cid, n * 2))
    if not vids:
        raise ai.AIError("Kanalda video topilmadi (avval sinxronlang)")
    done, texts = 0, []
    for v in vids:
        if done >= n:
            break
        if (v["duration"] or 0) > 420:
            continue
        try:
            tr = await transcribe.transcribe_post(ch, v)
            texts.append((v, tr))
            done += 1
        except Exception as e:
            log.warning("Video transkripsiya o'tkazib yuborildi (#%s): %s", v["id"], e)
            texts.append((v, ""))
            done += 1
    stats = ch_stats.source_stats(cid, 0, 90)
    lines = [f"Own channel video stats (90d): videos {stats['mix'].get('video', {}).get('n', 0)}, avg duration {stats['avg_video_sec']}s, "
             f"video share {stats['video_share']}%"]
    for v, tr in texts:
        lines.append(f"- {v['duration']}s, {v['views']} views | caption: {_clip(v['text'], 300)}" +
                     (f"\n  transcript: {_clip(tr, 700)}" if tr else "\n  (no transcript)"))
    data, _ = await ai.chat(cid, "video", VIDEO_SYSTEM, profile_text(ch) + "\n\n" + "\n".join(lines), max_out=2500, note="video format")
    summary = (data or {}).get("summary") if isinstance(data, dict) else None
    if not summary:
        raise ai.AIError("Video format xulosasi olinmadi")
    ch_data.ensure_memory(cid)
    db.ex("UPDATE ch_memory SET video_summary=?, video_at=? WHERE channel_id=?", (str(summary), db.now(), cid))
    return str(summary)


async def learn_loop():
    """Har 12 soatda yangi ma'lumot yetarli bo'lsa, har kanal uchun xotirani boyitadi (AI kaliti va yoqilgan bo'lsa)."""
    await asyncio.sleep(400)
    while True:
        try:
            if ai.configured() and db.get_setting("ch_learn_on", "1") != "0":
                for ch in ch_data.channels():
                    try:
                        await learn(ch["id"])
                    except ai.AIError as e:
                        log.warning("learn (%s): %s", ch["title"], e)
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ch_ai.learn_loop")
        await asyncio.sleep(12 * 3600)
