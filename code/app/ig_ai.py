"""Instagram: AI maqsad taklifi, g'oya, ssenariy, caption tahlili, reja nazorati, tahlil va target tavsiyasi (OpenAI)."""
import json
from datetime import datetime, timedelta

from . import ai, db, ig_collect, ig_data, ig_plan, ig_stats

FMT = "%Y-%m-%d %H:%M:%S"
LANG_NAMES = {"uz": "Uzbek (Latin script, natural modern Uzbek as spoken in Uzbekistan)",
              "ru": "Russian (natural, as spoken in Uzbekistan)"}
UZ = "Write all explanations in Uzbek (Latin script)."


def _clip(t, n):
    t = " ".join((t or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def _cid(acc) -> int:
    return -int(acc["id"])


def _langs(lang, acc) -> list[str]:
    if lang == "both" or (not lang and (acc["lang"] or "") == "mixed"):
        return ["uz", "ru"]
    if lang in ("uz", "ru"):
        return [lang]
    return ["ru"] if (acc["lang"] or "uz") == "ru" else ["uz"]


# ---------------------------------------------------------------- kontekst
def profile_text(acc) -> str:
    lines = [f"INSTAGRAM ACCOUNT: @{acc['username']} ({acc['account_type'] or '?'}), followers {acc['followers']}, following {acc['follows']}, "
             f"posts {acc['media_count']}", f"Bio: {_clip(acc['bio'], 300)}"]
    for label, v in (("Niche / what the page is about", acc["niche"]), ("Target audience", acc["audience"]), ("Tone of voice", acc["tone"]),
                     ("Offer / what the page sells or promotes", acc["offer"]), ("Extra notes", acc["extra"])):
        if (v or "").strip():
            lines.append(f"{label}: {_clip(v, 500)}")
    if (acc["goal_text"] or "").strip():
        lines.append(f"CURRENT GOAL (set by owner): {acc['goal_text']}" + (f" | target followers {acc['goal_target']} by {acc['goal_date']}" if acc["goal_target"] else ""))
    return "\n".join(lines)


def _m_line(m) -> str:
    cap = _clip(m["caption"], 140)
    return (f"- {m['ts'][:10]} {ig_stats.FMT_L.get(ig_stats.fmt_of(m), m['mtype'])}: reach {m['reach']}, views {m['views']}, likes {m['likes']}, "
            f"comments {m['comments']}, saves {m['saved']}, shares {m['shares']}, ER {ig_stats.er(m)}% | {cap}")


def stats_context(acc, days=30) -> str:
    aid = acc["id"]
    s = ig_stats.summary(aid, days)
    parts = [f"LAST {days} DAYS: followers change {s['growth']}, posts {s['posts']} ({s['per_week']}/week), avg reach/post {s['avg_reach']}, avg ER {s['er']}%"]
    fm = ig_stats.by_format(aid, 60)
    if fm:
        parts.append("BY FORMAT (60 days): " + "; ".join(f"{f['label']}: {f['n']} posts, avg reach {f['reach']}, ER {f['er']}%, saves {f['saves']}, shares {f['shares']}" for f in fm))
    tops = ig_stats.top(aid, 90, 5)
    if tops:
        parts.append("BEST POSTS (by reach):\n" + "\n".join(_m_line(m) for m in tops))
    worst = [m for m in ig_stats.media(aid, 60) if m["reach"]]
    worst = sorted(worst, key=lambda m: m["reach"])[:3]
    if worst:
        parts.append("WEAKEST RECENT POSTS:\n" + "\n".join(_m_line(m) for m in worst))
    hs = ig_stats.hour_scores(aid)
    if hs:
        best = sorted(hs.items(), key=lambda kv: -kv[1])[:4]
        parts.append("BEST POSTING SLOTS (avg reach): " + ", ".join(f"{ig_plan.DAYS_UZ[d]} {h:02d}:00 ({int(v)})" for (d, h), v in best))
    demo = ig_collect.demo(aid)
    for k in ("age", "gender", "city", "country"):
        if demo.get(k):
            parts.append(f"AUDIENCE by {k}: " + ", ".join(f"{x['k']} {x['v']}" for x in demo[k][:6]))
    return "\n".join(parts)


def schedule_context(acc, exclude_id=None, days=14) -> str:
    now = datetime.now()
    rows = ig_plan.busy_slots(acc["id"], now, now + timedelta(days=days), exclude_id)
    if not rows:
        return f"ALREADY SCHEDULED POSTS (next {days} days): none"
    lines = []
    for r in rows[:20]:
        p = db.one("SELECT caption, mtype FROM ig_plan WHERE id=?", (r["id"],))
        lines.append(f"- #{r['id']} {r['t'].strftime('%Y-%m-%d %H:%M')} [{p['mtype'] if p else ''}] {r['title']}: {_clip(p['caption'] if p else '', 140)}")
    return f"ALREADY SCHEDULED POSTS (next {days} days):\n" + "\n".join(lines)


def ideas_history(acc, n=12) -> str:
    rows = db.q("SELECT title, status, fmt, created_at FROM ig_ideas WHERE account_id=? AND kind='idea' ORDER BY id DESC LIMIT ?", (acc["id"], n))
    return "PREVIOUS IDEAS:\n" + "\n".join(f"- {r['created_at'][:10]} [{r['status']}] {r['fmt'] or ''} {r['title']}" for r in rows) if rows else "PREVIOUS IDEAS: none"


def full_context(acc, days=30) -> str:
    return "\n\n".join((profile_text(acc), stats_context(acc, days), schedule_context(acc)))


def _need_profile(acc):
    if not ig_data.profile_complete(acc):
        raise ai.AIError("Avval «Profil va maqsad» sahifasida sahifa nima haqida ekanini (soha) kiriting: AI aniq tavsiya berishi uchun kerak.")


def _dict(data):
    if not isinstance(data, dict):
        raise ai.AIError("AI kutilgan formatda javob bermadi. Qayta urinib ko'ring.")
    return data


# ---------------------------------------------------------------- maqsad
GOAL_SYSTEM = """You are a senior Instagram growth strategist for a small team in Uzbekistan that manages the Instagram page below.
The MAIN aim is to grow this Instagram page (followers, reach and engagement that lead to real growth). Using the page profile, real statistics
and what has worked, propose ONE realistic, measurable goal for the next 30-90 days. Do not promise miracles: base the target on the observed growth
rate (if little data exists, say so and be conservative). """ + UZ + """ Return ONLY JSON:
{"goal": "one-sentence goal in Uzbek", "target_followers": integer (absolute number of followers to reach), "days": integer (30-90),
 "why": "2-4 sentences: why this target is realistic given the data",
 "kpis": [{"name": "...", "now": "current value", "target": "target value"}],
 "strategy": ["4-6 concrete actions for this page"], "risks": ["1-3 risks or assumptions"]}"""


async def propose_goal(acc) -> dict:
    _need_profile(acc)
    user = f"{full_context(acc, 60)}\n\nToday: {datetime.now().strftime('%Y-%m-%d')}"
    data, meta = await ai.chat(_cid(acc), "analysis", GOAL_SYSTEM, user, max_out=2500, note="IG maqsad taklifi")
    data = _dict(data)
    try:
        data["target_followers"] = max(int(acc["followers"] or 0), int(data.get("target_followers") or 0))
        data["days"] = max(14, min(180, int(data.get("days") or 60)))
    except (TypeError, ValueError):
        data["target_followers"], data["days"] = int(acc["followers"] or 0) + 100, 60
    data["cost"] = meta.get("cost")
    return data


# ---------------------------------------------------------------- g'oya va ssenariy
IDEA_SYSTEM = """You are a senior Instagram content strategist and copywriter for a page run by a small team in Uzbekistan.
The main aim is to GROW the page (followers, reach, saves, shares), not vanity. Use the page profile, its goal, real statistics (what formats and posts
worked), the audience and the already scheduled posts. Rules: do not repeat previous ideas or scheduled posts; vary formats according to what the data
shows works (Reels for reach, carousels for saves, etc.); give concrete, shootable ideas a small team can produce; hooks must grab attention in the
first 2 seconds / first line. Captions: natural, with a clear call-to-action; hashtags: 3-8, niche-specific, no banned/spammy tags.
Languages: {langs}. Fill each language key you were asked for. """ + UZ + """ Return ONLY JSON:
{"ideas": [{"title": "short working title (Uzbek)", "format": "reel|carousel|photo|story", "hook": "first line / first 2 seconds",
 "concept": "what exactly to shoot/design, step by step (3-6 sentences, Uzbek)", "why": "why this should work for THIS page, citing its data",
 "captions": {"uz": "ready caption", "ru": "ready caption"}, "hashtags": ["#..."], "cta": "call to action", "best_time_hint": "e.g. Tuesday evening",
 "effort": "easy|medium|hard"}]}
Create exactly {n} ideas that differ clearly."""


async def new_ideas(acc, lang="uz", n=3, note="", fmt="") -> tuple[list[dict], dict]:
    _need_profile(acc)
    langs = _langs(lang, acc)
    n = max(1, min(6, n))
    system = IDEA_SYSTEM.replace("{langs}", " + ".join(LANG_NAMES[x] for x in langs)).replace("{n}", str(n))
    user = (f"{full_context(acc)}\n\n{ideas_history(acc)}\n\nOwner note: {note or '-'}\nRequired format: {fmt or 'your choice, mix by data'}")
    data, meta = await ai.chat(_cid(acc), "idea", system, user, max_out=6000, note="IG g'oya")
    ideas = [i for i in (_dict(data).get("ideas") or []) if isinstance(i, dict) and i.get("title")][:n]
    if not ideas:
        raise ai.AIError("AI g'oya yozib bera olmadi. Qayta urinib ko'ring.")
    return ideas, meta


SCRIPT_SYSTEM = """You are a short-video scriptwriter (Instagram Reels / Stories) for a small team in Uzbekistan who shoot on a phone.
The aim is to grow the page: strong hook in the first 2 seconds, one clear idea, fast pacing, a reason to save/share/follow at the end.
Use the page profile and what worked for it. Language of voiceover/on-screen text/caption: {lang}. """ + UZ + """ Return ONLY JSON:
{"title": "...", "duration_sec": integer, "goal": "what this video should achieve", "hook": "exact words/visual of the first 2-3 seconds",
 "scenes": [{"t": "0-3s", "visual": "what is on screen / what to shoot", "voiceover": "exact words (or empty)", "onscreen": "short on-screen text (or empty)"}],
 "caption": "ready caption", "hashtags": ["#..."], "music": "music/sound suggestion (generic type, not a specific copyrighted track)",
 "cta": "...", "shooting_tips": ["3-5 practical tips: light, angle, props, editing"], "needs": ["items/people/locations needed"]}"""


async def new_script(acc, topic: str, duration: int = 30, lang="uz", style="", idea_body: dict | None = None) -> tuple[dict, dict]:
    _need_profile(acc)
    lg = _langs(lang, acc)[0]
    system = SCRIPT_SYSTEM.replace("{lang}", LANG_NAMES[lg])
    user = (f"{profile_text(acc)}\n\n{stats_context(acc, 60)}\n\nTOPIC: {topic}\nTarget length: {duration} seconds\nStyle/notes: {style or '-'}"
            + (f"\nBased on this approved idea:\n{json.dumps(idea_body, ensure_ascii=False)[:1500]}" if idea_body else ""))
    data, meta = await ai.chat(_cid(acc), "idea", system, user, max_out=4500, note="IG ssenariy")
    data = _dict(data)
    if not data.get("scenes"):
        raise ai.AIError("AI ssenariy yozib bera olmadi. Qayta urinib ko'ring.")
    return data, meta


# ---------------------------------------------------------------- caption tahlili
REVIEW_SYSTEM = """You are an Instagram editor. The owner wrote a draft caption for a post. NEVER rewrite it silently: give analysis and suggestions next
to it; the owner's text stays untouched. Use the page profile, goal, what worked, and the scheduled posts. Check: hook in the first line (only ~125
characters show before "more"), clarity, one clear call-to-action, readability, hashtags (3-8, relevant), fit with the format and the page's tone.
Score 0-10. """ + UZ + """ Return ONLY JSON:
{"score": {"hook": n, "clarity": n, "cta": n, "structure": n, "fit": n, "overall": n},
 "summary": "2-3 sentences",
 "suggestions": [{"area": "hook|cta|length|structure|tone|hashtags|format", "priority": "high|medium|low", "issue": "...", "fix": "..."}],
 "improved": "a full improved caption, same language as the draft", "hashtags": ["#..."], "best_format": "reel|carousel|photo|story and one short reason",
 "warnings": ["problems, e.g. caption too long (limit 2200), more than 30 hashtags; empty if none"]}
Give 3-6 suggestions, most important first."""


async def review_caption(acc, caption: str, title="", mtype="IMAGE", media_n=0, plan_id=None, when="") -> dict:
    caption = (caption or "").strip()
    if len(caption) < 15:
        raise ai.AIError("Tahlil uchun avval tavsif (caption) yozing")
    user = (f"{profile_text(acc)}\n\n{stats_context(acc, 60)}\n\n{schedule_context(acc, plan_id)}\n\n=== DRAFT ===\nTitle: {title or '-'}\n"
            f"Format: {mtype}, media files: {media_n}\nPlanned time: {when or 'not set'}\nLength: {len(caption)} chars\n---\n{caption[:2400]}")
    data, meta = await ai.chat(_cid(acc), "analysis", REVIEW_SYSTEM, user, max_out=3000, note="IG caption tahlili")
    data = _dict(data)
    sc = data.get("score") if isinstance(data.get("score"), dict) else {}
    data["score"] = {k: max(0, min(10, float(sc.get(k, 0) or 0))) for k in ("hook", "clarity", "cta", "structure", "fit", "overall")}
    data["suggestions"] = [s for s in (data.get("suggestions") or []) if isinstance(s, dict)][:8]
    data["improved"] = str(data.get("improved") or "")
    if len(caption) > 2200:
        data.setdefault("warnings", []).insert(0, f"Tavsif {len(caption)} belgi: Instagram limiti 2200")
    data["times"] = ig_plan.suggest_times(acc["id"], plan_id)
    data["cost"] = meta.get("cost")
    return data


# ---------------------------------------------------------------- reja nazorati
CONTROL_SYSTEM = """You are the content-plan controller for an Instagram page whose main aim is growth. You get the page profile, its goal, real results,
the already scheduled posts (with ids) for the next 14 days, and recent published posts. Evaluate the plan against the goal and the data: is the cadence
enough (posts/week), is the format mix right (what formats earned reach/saves/shares), are there empty days or clusters, do topics repeat, is anything
missing for the goal (e.g. no Reels, no CTA, no offer post)? Then propose the NEXT content ideas that fill the gaps and say when to post them.
Do not invent data. """ + UZ + """ Return ONLY JSON:
{"verdict": "good|ok|weak", "summary": "3-4 sentences", "cadence": {"planned_per_week": number, "recommended_per_week": number, "comment": "..."},
 "format_balance": [{"format": "Reels|Karusel|Rasm|Stories", "planned": n, "recommended": n, "comment": "..."}],
 "gaps": ["what is missing or weak in the plan"],
 "changes": [{"plan_id": id or null, "action": "keep|move|replace|add_cta|drop", "reason": "..."}],
 "next_ideas": [{"title": "...", "format": "reel|carousel|photo|story", "when": "YYYY-MM-DD HH:MM (a free slot, avoid scheduled ones)", "why": "..."}],
 "warnings": ["..."]}
Give 3-5 next_ideas."""


async def control_plan(acc) -> dict:
    _need_profile(acc)
    pub = db.q("SELECT title, caption, mtype, sent_at FROM ig_plan WHERE account_id=? AND status='published' ORDER BY id DESC LIMIT 6", (acc["id"],))
    recent = "RECENTLY PUBLISHED FROM PLAN:\n" + "\n".join(f"- {r['sent_at'][:10]} [{r['mtype']}] {r['title']}" for r in pub) if pub else ""
    user = f"{full_context(acc, 30)}\n\n{recent}\n\n{ideas_history(acc)}\n\nToday: {datetime.now().strftime('%A %Y-%m-%d %H:%M')}"
    data, meta = await ai.chat(_cid(acc), "analysis", CONTROL_SYSTEM, user, max_out=3500, note="IG reja nazorati")
    data = _dict(data)
    data["next_ideas"] = [i for i in (data.get("next_ideas") or []) if isinstance(i, dict)][:6]
    data["cost"] = meta.get("cost")
    return data


# ---------------------------------------------------------------- umumiy tahlil
ANALYSIS_SYSTEM = """You are an Instagram analyst. Analyse the page using ONLY the data given (profile, goal, daily numbers, per-post results, formats,
audience). Be specific: cite posts/numbers. Separate facts from hypotheses. If data is thin (new account, few posts), say so. """ + UZ + """ Return ONLY JSON:
{"summary": "3-5 sentences: where the page stands", "goal_progress": "how close the page is to its goal and what pace is needed",
 "what_works": [{"point": "...", "evidence": "post/number"}], "what_not": [{"point": "...", "evidence": "..."}],
 "growth_levers": [{"lever": "...", "action": "concrete step", "impact": "high|medium|low"}],
 "audience_note": "what the audience data suggests (or that it is not available)", "next_7_days": ["7 concrete actions"]}"""


async def analyse(acc) -> dict:
    _need_profile(acc)
    d = ig_stats.daily(acc["id"], 30)
    rows = "\n".join(f"{x['day']}: followers {x.get('followers')}, reach {x.get('reach')}, views {x.get('views')}, interactions {x.get('interactions')}"
                     for x in d if x.get("followers") is not None or x.get("reach"))
    gp = ig_stats.goal_progress(acc)
    user = f"{full_context(acc, 60)}\n\nDAILY (30d):\n{rows[:2500]}\n\nGOAL PROGRESS: {json.dumps(gp) if gp else 'no numeric target'}"
    data, meta = await ai.chat(_cid(acc), "analysis", ANALYSIS_SYSTEM, user, max_out=3500, note="IG tahlil")
    data = _dict(data)
    data["cost"] = meta.get("cost")
    return data


# ---------------------------------------------------------------- target (reklama) tavsiyasi
def boost_candidates(aid, days=45, n=5) -> list[dict]:
    """AI'siz: qaysi postlar reklama (Boost) uchun mos — qamrov kichik, lekin jalb qilish baland (organik isbotlangan)."""
    ms = [m for m in ig_stats.media(aid, days) if m["reach"] and m["reach"] >= 30]
    if not ms:
        return []
    avg_reach = sum(m["reach"] for m in ms) / len(ms)
    out = []
    for m in ms:
        quality = ((m["saved"] or 0) * 3 + (m["shares"] or 0) * 4 + (m["comments"] or 0) * 2 + (m["likes"] or 0)) / m["reach"]
        age = (datetime.now() - datetime.strptime(m["ts"][:19], FMT)).days if m["ts"] else 99
        score = quality * (1.0 if age <= 21 else 0.7)
        out.append({"id": m["id"], "ig_id": m["ig_id"], "permalink": m["permalink"], "caption": _clip(m["caption"], 90), "fmt": ig_stats.FMT_L.get(ig_stats.fmt_of(m), ""),
                    "reach": m["reach"], "er": ig_stats.er(m), "saved": m["saved"], "shares": m["shares"], "age": age,
                    "score": round(score, 3), "underreached": m["reach"] < avg_reach})
    out.sort(key=lambda x: -x["score"])
    return out[:n]


TARGET_SYSTEM = """You advise a small Uzbek team on which of THEIR Instagram posts to promote with paid ads (Instagram "Boost"/Meta Ads), to grow the page.
Rules: promote only posts that already prove themselves organically (high saves/shares/comments per reach), fit the page goal, and have a clear CTA
or profile-visit value. For each pick give: objective (profile visits / followers / reach / messages / website clicks — match to the goal), audience
(use the page's real audience data if present, otherwise a cautious guess marked as a guess: Uzbekistan cities, age range, interests), a small test budget
in USD per day and days, and what to watch. State clearly that ads are created manually in the Instagram app (Professional dashboard → Ad tools → Boost
post) because this platform does not run ads. Warn when data is too thin for a reliable recommendation (then recommend waiting/posting more). """ + UZ + """
Return ONLY JSON:
{"summary": "2-3 sentences", "picks": [{"media_id": integer (the id given), "why": "...", "objective": "...", "audience": "...",
 "budget_usd_per_day": number, "days": integer, "watch": "what metric to watch and when to stop", "creative_note": "tweak before boosting, if any"}],
 "skip": "why other posts should not be boosted, or empty", "cautions": ["..."]}
Pick at most 3."""


async def boost_advice(acc) -> dict:
    _need_profile(acc)
    cands = boost_candidates(acc["id"])
    if not cands:
        raise ai.AIError("Reklama tavsiyasi uchun qamrov ma'lumoti bor postlar kerak (kamida bir nechta post, ma'lumot yig'ilgach). «Yangilash» ni bosing.")
    lines = "\n".join(f"- media_id {c['id']} ({c['fmt']}, {c['age']} kun oldin): reach {c['reach']}, ER {c['er']}%, saves {c['saved']}, shares {c['shares']}, "
                      f"quality-score {c['score']} | {c['caption']}" for c in cands)
    user = f"{profile_text(acc)}\n\n{stats_context(acc, 60)}\n\nCANDIDATES (pre-selected by score):\n{lines}"
    data, meta = await ai.chat(_cid(acc), "analysis", TARGET_SYSTEM, user, max_out=3000, note="IG target tavsiyasi")
    data = _dict(data)
    ids = {c["id"] for c in cands}
    data["picks"] = [p for p in (data.get("picks") or []) if isinstance(p, dict) and p.get("media_id") in ids][:3]
    data["cands"] = cands
    data["cost"] = meta.get("cost")
    return data


def save_report(aid, kind, body: dict, cost=None, model=""):
    return db.ex("INSERT INTO ig_reports(account_id,kind,created_at,body_json,model,cost) VALUES(?,?,?,?,?,?)",
                 (aid, kind, db.now(), json.dumps(body, ensure_ascii=False), model, cost or 0))


def last_report(aid, kind):
    r = db.one("SELECT * FROM ig_reports WHERE account_id=? AND kind=? ORDER BY id DESC LIMIT 1", (aid, kind))
    if not r:
        return None, None
    try:
        return json.loads(r["body_json"]), r
    except Exception:
        return None, None
