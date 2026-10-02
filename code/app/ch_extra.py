"""Telegram SMM: viral post tahlili, A/B sinov, izohlar tahlili va oylik kontent-reja (OpenAI)."""
import json
import re
import statistics
from datetime import datetime, timedelta

from . import ai, ch_ai, ch_assist, ch_data, ch_stats, ch_tg, ch_track, db
from .config import log

FMT = "%Y-%m-%d %H:%M:%S"
_clip = ch_ai._clip


def _lang_key(ch) -> str:
    return "ru" if (ch["lang"] or "uz") == "ru" else "uz"


def _lang_name(ch) -> str:
    k = "uz_cyrl" if (ch["lang"] or "") == "uz_cyrl" else _lang_key(ch)
    return ch_ai.LANG_NAMES[k]


def _j(v):
    try:
        return json.loads(v) if isinstance(v, str) else (v or {})
    except Exception:
        return {}


# ================================================================ viral post tahlili
ALERT_SYSTEM = """You are a Telegram content analyst. A post suddenly gets far more views than usual. Explain, from the real text and numbers only,
why it probably works, and how the owner's channel can use the same mechanics WITHOUT copying the text. Write in Uzbek (Latin).
Return ONLY JSON: {"why": ["2-4 concrete reasons (hook, format, offer, timing, topic, length)"], "use_for_us": "what exactly to do in our channel",
"post_idea": "a short outline of our own original post using this mechanic (3-6 lines)"}"""


async def analyze_alert(alert_id: int) -> dict:
    a = db.one("SELECT * FROM ch_alerts WHERE id=?", (alert_id,))
    if not a:
        raise ai.AIError("Ogohlantirish topilmadi")
    ch = ch_data.get(a["channel_id"])
    p = db.one("SELECT * FROM ch_posts WHERE channel_id=? AND comp_id=? AND msg_id=?", (a["channel_id"], a["comp_id"], a["msg_id"]))
    if not p:
        raise ai.AIError("Post ma'lumoti topilmadi")
    comp = db.one("SELECT title FROM ch_competitors WHERE id=?", (a["comp_id"],)) if a["comp_id"] else None
    user = (f"{ch_ai.profile_text(ch)}\n\n=== POST ({'own channel' if not a['comp_id'] else 'competitor ' + (comp['title'] if comp else '')}) ===\n"
            f"date {p['date']}, format {p['media']} {p['duration'] or ''}s, views {p['views']}, reactions {p['reactions']}, forwards {p['forwards']}\n"
            f"{_clip(p['text'], 1500)}" + (f"\nvideo transcript: {_clip(p['transcript'], 800)}" if p["transcript"] else "") +
            f"\n\nSignal: {a['title']}")
    data, _ = await ai.chat(ch["id"], "analysis", ALERT_SYSTEM, user, max_out=1800, note="viral tahlil")
    db.ex("UPDATE ch_alerts SET analysis=? WHERE id=?", (json.dumps(data, ensure_ascii=False), alert_id))
    return data


# ================================================================ A/B sinov
AB_SYSTEM = """You design a fair A/B test for a Telegram channel post. Both variants are published on different days at the same hour
(Telegram cannot split the audience), so they must differ in ONE clear element only (hook, offer, CTA wording, length or format),
everything else stays equal. Keep each variant ready to publish, natural in the requested language, max 900 characters when it carries media.
Do not add contact links: the system appends the tracked link. Return ONLY JSON:
{"hypothesis": "what we test and why it may win", "element": "hook|offer|cta|length|format", "a": "variant A text", "b": "variant B text",
 "how_to_read": "how the owner should judge the result (views, reactions, link clicks)"}"""


async def propose_ab(cid: int, base_text: str = "", idea_id=None, note: str = "") -> int:
    ch = ch_data.get(cid)
    idea_txt = ""
    if idea_id:
        row = db.one("SELECT * FROM ch_ideas WHERE id=? AND channel_id=?", (idea_id, cid))
        if row:
            b = ch_ai.idea_body(row)
            post = b.get("post") or {}
            idea_txt = post.get(_lang_key(ch)) or next(iter(post.values()), "")
    a_text = (base_text or idea_txt or "").strip()
    own = "Variant A is FIXED (owner's text): keep it exactly; write only the best possible variant B." if a_text else \
        "Write both variants from scratch for the channel's next post."
    user = (f"{ch_ai.build_context(ch, days=30, top_n=4, recent_n=2)}\n\n=== TASK ===\n{own}\nLanguage: {_lang_name(ch)}.\n" +
            (f"Variant A text:\n{a_text}\n" if a_text else "") + (f"Owner's note: {_clip(note, 400)}\n" if note.strip() else ""))
    data, meta = await ai.chat(cid, "idea", AB_SYSTEM, user, max_out=3000, note="A/B variant")
    if not isinstance(data, dict) or not data.get("b"):
        raise ai.AIError("AI kutilgan formatda javob bermadi. Qayta urinib ko'ring.")
    if a_text:
        data["a"] = a_text
    return db.ex("INSERT INTO ch_ab(channel_id,title,idea_id,hypothesis,status,result_json,created_at) VALUES(?,?,?,?,'draft',?,?)",
                 (cid, _clip(data.get("hypothesis") or "A/B sinov", 200), idea_id, data.get("hypothesis", ""),
                  json.dumps(data, ensure_ascii=False), db.now()))


def schedule_ab(ab_id: int, a_text: str, b_text: str, when_a: str, gap_days: int = 2, media_names=None, mtype=None) -> tuple[int, int]:
    """Ikkala variantni reja(ga) qo'shadi: B — A dan gap_days kun keyin, xuddi shu soatda."""
    ab = db.one("SELECT * FROM ch_ab WHERE id=?", (ab_id,))
    ch = ch_data.get(ab["channel_id"])
    foot = ch_data.lead_footer(ch)
    t_a = datetime.strptime(when_a, FMT)
    t_b = t_a + timedelta(days=max(1, gap_days))
    ids = []
    for var, txt, t in (("A", a_text, t_a), ("B", b_text, t_b)):
        txt = txt.strip()
        if foot and (ch["lead_url"] or "") not in txt:
            txt = txt.rstrip() + "\n\n" + foot
        pid = db.ex("INSERT INTO ch_plan(channel_id,title,text,media_json,media_type,parse_mode,scheduled_at,status,idea_id,created_at,ab_id,ab_variant) "
                    "VALUES(?,?,?,?,?,'none',?,'scheduled',?,?,?,?)",
                    (ch["id"], f"A/B #{ab_id} — {var}", txt, json.dumps(media_names or []), mtype, t.strftime(FMT), ab["idea_id"],
                     db.now(), ab_id, var))
        ch_track.ensure_plan_code(pid, ch["id"], f"A/B #{ab_id} {var}", ab_id=ab_id, variant=var)
        ids.append(pid)
    db.ex("UPDATE ch_ab SET status='running' WHERE id=?", (ab_id,))
    return ids[0], ids[1]


def _variant_metrics(ch, item) -> dict:
    m = {"plan_id": item["id"], "status": item["status"], "sent_at": item["sent_at"], "views": None, "reactions": None, "forwards": None,
         "clicks": ch_track.clicks_by_code(ch["id"]).get(item["track_code"], 0) if item["track_code"] else 0, "age_h": None}
    if item["status"] != "sent" or not item["msg_id"]:
        return m
    p = db.one("SELECT * FROM ch_posts WHERE channel_id=? AND comp_id=0 AND msg_id=?", (ch["id"], item["msg_id"]))
    try:
        m["age_h"] = round((datetime.now() - datetime.strptime(item["sent_at"], FMT)).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        pass
    if p:
        m.update(views=p["views"] or 0, reactions=p["reactions"] or 0, forwards=p["forwards"] or 0)
    return m


def evaluate_ab(ab_id: int) -> dict:
    ab = db.one("SELECT * FROM ch_ab WHERE id=?", (ab_id,))
    ch = ch_data.get(ab["channel_id"])
    items = {r["ab_variant"]: r for r in db.q("SELECT * FROM ch_plan WHERE ab_id=?", (ab_id,))}
    met = {v: _variant_metrics(ch, items[v]) for v in ("A", "B") if v in items}
    out = {"metrics": met, "winner": None, "verdict": "Natija hali yo'q: ikkala post yuborilgach kamida 24 soat kuting.", "ready": False}
    a, b = met.get("A"), met.get("B")
    if a and b and a["views"] is not None and b["views"] is not None and (a["age_h"] or 0) >= 24 and (b["age_h"] or 0) >= 24:
        out["ready"] = True
        mem = max(ch["members"] or 1, 1)
        sc = {}
        for v, m in (("A", a), ("B", b)):
            ctr = (m["clicks"] / m["views"]) if m["views"] else 0
            eng = (m["reactions"] + 2 * m["forwards"]) / max(m["views"], 1)
            sc[v] = {"er": m["views"] / mem * 100, "ctr": ctr * 100, "eng": eng * 100}
        out["scores"] = {v: {k: round(x, 2) for k, x in d.items()} for v, d in sc.items()}
        use_clicks = (a["clicks"] + b["clicks"]) >= 10
        key = "ctr" if use_clicks else "er"
        hi, lo = ("A", "B") if sc["A"][key] >= sc["B"][key] else ("B", "A")
        diff = (sc[hi][key] - sc[lo][key]) / max(sc[lo][key], 0.0001) * 100
        basis = "havola bosilish ulushi (CTR)" if use_clicks else "ko'rishlar / obunachilar (ER)"
        if diff >= 20:
            out["winner"] = hi
            out["verdict"] = (f"{hi} variant {diff:.0f}% yaxshi ({basis}). Bitta sinov yo'nalishni ko'rsatadi, xulosani "
                              f"3–4 sinovdan keyin mustahkamlang.")
        else:
            out["verdict"] = f"Farq sezilarli emas ({diff:.0f}%, {basis}). Ikki variant deyarli teng ishladi."
    status = "done" if out["ready"] else ab["status"]
    db.ex("UPDATE ch_ab SET status=?, winner=?, result_json=? WHERE id=?",
          (status, out["winner"], json.dumps({**_j(ab["result_json"]), "eval": out}, ensure_ascii=False), ab_id))
    return out


def ab_list(cid: int) -> list[dict]:
    rows = []
    for r in db.q("SELECT * FROM ch_ab WHERE channel_id=? ORDER BY id DESC LIMIT 40", (cid,)):
        d = dict(r)
        d["data"] = _j(r["result_json"])
        d["eval"] = d["data"].get("eval")
        rows.append(d)
    return rows


# ================================================================ izohlar
def comments_state(cid: int) -> dict:
    v = db.get_setting(f"ch_linked_{cid}", "")
    return {"checked": bool(v), "enabled": v not in ("", "0")}


async def collect_comments(cid: int) -> dict:
    ch = ch_data.get(cid)
    info = await ch_tg.channel_info(ch["account_id"], ch["tg_id"])
    linked = info.get("linked_chat")
    db.set_setting(f"ch_linked_{cid}", str(linked) if linked else "0")
    if not linked:
        return {"enabled": False, "n": 0}
    cut = (datetime.now() - timedelta(days=30)).strftime(FMT)
    posts = db.q("SELECT msg_id, replies FROM ch_posts WHERE channel_id=? AND comp_id=0 AND date>=? AND replies>0 ORDER BY replies DESC LIMIT 25",
                 (cid, cut))
    rows = await ch_tg.comments_for(ch["account_id"], ch["tg_id"], [p["msg_id"] for p in posts])
    if rows:
        db.many("INSERT OR IGNORE INTO ch_comments(channel_id,post_msg_id,cmsg_id,date,text) VALUES(?,?,?,?,?)",
                [(cid, r["post"], r["id"], r["date"], r["text"]) for r in rows])
    return {"enabled": True, "n": len(rows)}


COMMENTS_SYSTEM = """You analyse audience comments under a business Telegram channel's posts. The owner wants more sales and leads.
Extract only what the comments really say (no invented quotes; paraphrase; never include names or usernames). Write in Uzbek (Latin).
Return ONLY JSON: {"summary": "3-5 sentences", "sentiment": {"positive": %, "neutral": %, "negative": %},
"questions": [{"q": "frequent question", "count": n, "answer_idea": "how to answer in a post"}],
"objections": [{"text": "objection/doubt (price, trust, timing...)", "count": n, "reply_idea": "how to handle it"}],
"requests": ["what people ask for"], "topics": [{"topic": "...", "count": n}], "content_ideas": ["post ideas that answer these comments"]}"""


async def analyze_comments(cid: int) -> dict:
    ch = ch_data.get(cid)
    rows = db.q("SELECT post_msg_id, date, text FROM ch_comments WHERE channel_id=? ORDER BY date DESC LIMIT 350", (cid,))
    if len(rows) < 5:
        raise ai.AIError("Tahlil uchun izoh yetarli emas (kamida 5 ta kerak)")
    lines = "\n".join(f"- {_clip(r['text'], 220)}" for r in rows)
    user = f"{ch_ai.profile_text(ch)}\n\n=== COMMENTS ({len(rows)}, newest first) ===\n{lines}"
    data, meta = await ai.chat(cid, "analysis", COMMENTS_SYSTEM, user, max_out=3500, note="izoh tahlili")
    db.ex("INSERT INTO ch_ideas(channel_id,created_at,kind,lang,title,body_json,status,model,tokens_in,tokens_out,cost) "
          "VALUES(?,?,'comments','uz',?,?,'report',?,?,?,?)",
          (cid, db.now(), "Izohlar tahlili " + datetime.now().strftime("%Y-%m-%d"), json.dumps(data, ensure_ascii=False),
           meta["model"], meta["tokens_in"], meta["tokens_out"], meta["cost"]))
    return data


def last_comments_report(cid: int):
    r = db.one("SELECT * FROM ch_ideas WHERE channel_id=? AND kind='comments' ORDER BY id DESC LIMIT 1", (cid,))
    return (_j(r["body_json"]), r["created_at"]) if r else (None, None)


# ================================================================ oylik kontent-reja
MONTH_SYSTEM = """You are a Telegram content strategist for a business channel whose goal is sales and maximum leads.
Build a one-month content calendar from the provided REAL data: own best posts and posting hours, competitor patterns, knowledge memo,
audience comments (questions/objections), lead link clicks by post, finished A/B results and recent viral signals.
Rules: follow the channel's posting frequency and rubrics; put posts at the hours that performed best; mix formats using the data
(video share as the data suggests); answer real audience questions/objections in posts; alternate selling posts with value posts;
never invent facts, prices or guarantees; every post ends with a natural call to action (the system appends the lead link).
Return ONLY JSON: {"theme": "month theme", "strategy": "4-6 sentences: what the plan does and why, citing the data",
"items": [{"date": "YYYY-MM-DD", "time": "HH:MM", "rubric": "...", "format": "text|photo|video|album|poll", "topic": "short title",
"brief": "2-4 sentences: angle, key points, hook", "goal": "sell|lead|trust|engage", "why": "the data point behind it"}]}
Dates must be inside the requested month and in ascending order."""


def month_schedule_text(cid: int, month: str) -> str:
    """Kontent rejada shu oyga allaqachon qo'yilgan postlar (AI ularga to'qnashmasligi uchun)."""
    y, m = int(month[:4]), int(month[5:7])
    start = datetime(y, m, 1)
    end = datetime(y + (m == 12), (m % 12) + 1, 1) - timedelta(seconds=1)
    rows = ch_assist.busy_slots(cid, start, end)
    if not rows:
        return f"ALREADY IN THE CONTENT PLAN for {month}: nothing yet."
    lines = []
    for r in rows:
        t = db.one("SELECT text, media_type FROM ch_plan WHERE id=?", (r["id"],))
        lines.append(f"- {r['t'].strftime('%Y-%m-%d %H:%M')} [{r['status']}] {r['title']} ({(t['media_type'] if t else '') or 'text'}): "
                     f"{_clip(t['text'] if t else '', 140)}")
    return (f"ALREADY IN THE CONTENT PLAN for {month} ({len(rows)} posts; they stay as they are). Fit the new plan AROUND them: "
            f"do not duplicate their topics, do not put a new post within 2 hours of them, and keep at most {ch_assist.max_per_day()} posts per day in total "
            f"(existing + new). Fill the free days and balance rubrics against what is already planned.\n" + "\n".join(lines))


def month_context(ch, month: str | None = None) -> str:
    cid = ch["id"]
    parts = [ch_ai.build_context(ch, days=30, top_n=5, recent_n=3)]
    if month:
        parts.append(month_schedule_text(cid, month))
    s = ch_stats.source_stats(cid, 0, 30)
    parts.append("OWN POSTING PATTERN: " + ch_ai.stats_text(s))
    top = ch_track.top_links(cid, 8)
    if top and any(t["n"] for t in top):
        parts.append("LEAD LINK CLICKS BY POST (best first):\n" + "\n".join(f"- {t['label'] or t['code']}: {t['n']} clicks" for t in top if t["n"]))
    rep, _ = last_comments_report(cid)
    if rep:
        parts.append("AUDIENCE COMMENTS INSIGHTS: " + _clip(json.dumps({k: rep.get(k) for k in ("questions", "objections", "requests", "topics")},
                                                                      ensure_ascii=False), 1800))
    abr = [a for a in ab_list(cid) if a.get("winner")][:4]
    if abr:
        parts.append("A/B RESULTS:\n" + "\n".join(f"- {a['title']}: winner {a['winner']} ({a['eval']['verdict'] if a.get('eval') else ''})" for a in abr))
    vs = db.q("SELECT title FROM ch_alerts WHERE channel_id=? ORDER BY id DESC LIMIT 5", (cid,))
    if vs:
        parts.append("RECENT VIRAL SIGNALS:\n" + "\n".join(f"- {v['title']}" for v in vs))
    return "\n\n".join(parts)


def resolve_conflicts(cid: int, items: list[dict], month: str, gap_h: float = 2.0) -> int:
    """Yangi reja postlarini Kontent rejadagi mavjud postlar bilan to'qnashtirmaydi:
    kunlik me'yordan oshsa yoki 2 soatdan yaqin bo'lsa, soatni siljitadi yoki eng yaqin bo'sh kunga ko'chiradi.
    Qaytaradi: rejada allaqachon turgan postlar soni."""
    y, m = int(month[:4]), int(month[5:7])
    start = datetime(y, m, 1)
    end = datetime(y + (m == 12), (m % 12) + 1, 1) - timedelta(seconds=1)
    cap = ch_assist.max_per_day()
    occ: dict[str, list[datetime]] = {}
    existing = ch_assist.busy_slots(cid, start, end)
    for b in existing:
        occ.setdefault(b["t"].strftime("%Y-%m-%d"), []).append(b["t"])

    def free(t: datetime) -> bool:
        day = occ.get(t.strftime("%Y-%m-%d"), [])
        return len(day) < cap and all(abs((t - o).total_seconds()) >= gap_h * 3600 for o in day)

    for it in items:
        t0 = datetime.strptime(f"{it['date']} {it['time']}", "%Y-%m-%d %H:%M")
        cand = None
        if free(t0):
            cand = t0
        else:
            for dh in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5):          # shu kunning o'zida soatni siljitish
                t = t0 + timedelta(hours=dh)
                if t.date() == t0.date() and 8 <= t.hour <= 22 and free(t):
                    cand = t
                    break
            if cand is None:
                for dd in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6, 7, -7):   # eng yaqin bo'sh kun
                    t = t0 + timedelta(days=dd)
                    if t.month == m and free(t):
                        cand = t
                        break
        if cand is None:
            it["conflict"] = True
            it["moved_note"] = "Kontent rejada shu kunlar band: vaqtni o'zingiz tanlang"
            cand = t0
        elif cand != t0:
            it["moved_from"] = f"{it['date']} {it['time']}"
            it["moved_note"] = f"Kontent rejada band bo'lgani uchun {it['moved_from']} dan ko'chirildi"
            it["date"], it["time"] = cand.strftime("%Y-%m-%d"), cand.strftime("%H:%M")
        occ.setdefault(cand.strftime("%Y-%m-%d"), []).append(cand)
    return len(existing)


async def generate_month(cid: int, month: str, note: str = "") -> int:
    ch = ch_data.get(cid)
    y, m = int(month[:4]), int(month[5:7])
    days = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days
    user = (f"{month_context(ch, month)}\n\n=== TASK ===\nMonth: {month} ({days} days). Language of topics/briefs: Uzbek (Latin).\n"
            f"Channel post frequency: {ch['post_freq'] or 'about 1 post per day'}.\n" + (f"Owner's note: {_clip(note, 500)}\n" if note.strip() else ""))
    data, meta = await ai.chat(cid, "idea", MONTH_SYSTEM, user, max_out=12000, note="oylik reja")
    items = (data or {}).get("items") if isinstance(data, dict) else None
    if not items:
        raise ai.AIError("AI oylik reja tuzib bera olmadi. Qayta urinib ko'ring.")
    clean = []
    for it in items:
        d = str(it.get("date", ""))[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d) or not d.startswith(month):
            continue
        t = str(it.get("time", "19:00"))[:5]
        it["time"] = t if re.match(r"^\d{2}:\d{2}$", t) else "19:00"
        it["date"] = d
        clean.append(it)
    clean.sort(key=lambda x: (x["date"], x["time"]))
    data["existing"] = resolve_conflicts(cid, clean, month)
    clean.sort(key=lambda x: (x["date"], x["time"]))
    data["items"] = clean
    return db.ex("INSERT INTO ch_month(channel_id,month,created_at,body_json,model,cost) VALUES(?,?,?,?,?,?)",
                 (cid, month, db.now(), json.dumps(data, ensure_ascii=False), meta["model"], meta["cost"]))


def month_get(mid: int):
    r = db.one("SELECT * FROM ch_month WHERE id=?", (mid,))
    return (r, _j(r["body_json"])) if r else (None, None)


POST_SYSTEM = """You write one ready-to-publish Telegram post for a business channel from a brief. Match the channel's tone and rubrics, never invent
prices/guarantees/facts not in the profile, end with a natural call to action. Do NOT add links or contact numbers (the system appends the
lead link). Max 900 characters if the post carries a photo/video, otherwise up to 3000. Also write a concise English prompt for an AI image
generator: a single clear scene/background WITHOUT any text, letters, logos or watermarks, matching the post topic.
Return ONLY JSON: {"post": "full text", "image_prompt": "English prompt"}"""


async def write_item(mid: int, idx: int) -> dict:
    row, body = month_get(mid)
    it = body["items"][idx]
    ch = ch_data.get(row["channel_id"])
    user = (f"{ch_ai.profile_text(ch)}\n\n=== BRIEF ===\nDate {it['date']}, rubric: {it.get('rubric')}, format: {it.get('format')}, goal: {it.get('goal')}\n"
            f"Topic: {it.get('topic')}\nBrief: {it.get('brief')}\nLanguage: {_lang_name(ch)}.")
    data, _ = await ai.chat(ch["id"], "idea", POST_SYSTEM, user, max_out=2500, note="oylik reja: post matni")
    text = str((data or {}).get("post") or "").strip()
    if not text:
        raise ai.AIError("Post matni olinmadi")
    text = ch_ai._append_footer(text, ch)
    it["text"], it["image_prompt"] = text, str((data or {}).get("image_prompt") or "")
    db.ex("UPDATE ch_month SET body_json=? WHERE id=?", (json.dumps(body, ensure_ascii=False), mid))
    return it


def push_to_plan(mid: int, idxs: list[int]) -> int:
    row, body = month_get(mid)
    ch = ch_data.get(row["channel_id"])
    n = 0
    for i in idxs:
        if i < 0 or i >= len(body["items"]):
            continue
        it = body["items"][i]
        if it.get("plan_id"):
            continue
        has_text = bool((it.get("text") or "").strip())
        text = it["text"] if has_text else f"[Qoralama: matn yozilmagan]\n{it.get('topic')}\n{it.get('brief')}"
        when = f"{it['date']} {it['time']}:00"
        pid = db.ex("INSERT INTO ch_plan(channel_id,title,text,media_json,media_type,parse_mode,scheduled_at,status,created_at,image_prompt) "
                    "VALUES(?,?,?,?,?,'none',?,?,?,?)",
                    (ch["id"], _clip(it.get("topic") or "Post", 240), text, "[]", None, when, "draft",
                     db.now(), it.get("image_prompt")))
        ch_track.ensure_plan_code(pid, ch["id"], it.get("topic") or "")
        it["plan_id"] = pid
        n += 1
    db.ex("UPDATE ch_month SET body_json=? WHERE id=?", (json.dumps(body, ensure_ascii=False), mid))
    return n
