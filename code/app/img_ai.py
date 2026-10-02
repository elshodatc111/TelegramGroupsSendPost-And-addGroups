"""Image Generator: OpenAI bilan ishlash (FAQAT MATN: rasm/video yaratilmaydi, vision ham yo'q — xarajat minimal).

Hamma promptlar INGLIZ tilida yoziladi; foydalanuvchiga ko'rinadigan matn o'zbekcha.
- ssenariyni tahlil qilish (sahnalar <= 5 s, odamlar, joylar): 1 ta so'rov; mavzu bo'yicha ssenariy yozish
- odam va joylarning qisqa ingliz tavsifi (bir marta, hamma sahnada bir xil ishlatiladi): 1 ta so'rov
- tanlangan rasm platformasi va video platformalar uchun promptlar: sahnalar guruhlab (standart 5 tadan) bitta so'rovda
"""
import math
import re

from . import ai, img_db as D


def is_fatal(e: Exception) -> bool:
    """Butun jarayonni to'xtatadigan xatolar: kalit yo'q/noto'g'ri, hisobda mablag' yo'q."""
    t = str(e).lower()
    return isinstance(e, ai.AIError) and any(w in t for w in ("kaliti", "mablag'", "401"))


def _track(ctx, kind, meta, note=""):
    if ctx and meta:
        D.add_cost(ctx.get("project_id"), ctx.get("scene_id"), kind, meta.get("model", ""), meta.get("cost", 0), note)


# ---------------------------------------------------------------- matn (JSON)
async def text_json(system: str, user: str, *, ctx=None, note="", max_out=7000, creative=False, effort=None):
    model = D.model_script() if creative else D.model_text()
    data, meta = await ai.chat(0, "idea" if creative else "analysis", system, user, model=model, max_out=max_out,
                               effort=effort, note="Image Generator: " + note)
    _track(ctx, "text", meta, note)
    return data


# ---------------------------------------------------------------- ssenariy: mavzu bo'yicha yozish
SCRIPT_SYSTEM = """You are an expert short-video copywriter for Uzbek businesses. Write a short ad/educational video script in UZBEK (Latin script).
Keep EXACTLY this layout (this is how the platform parses it):
Hook: <short hook line in Uzbek>
0-3s: Ko'rinish: <what the viewer sees: person, action, setting, props>
Ovoz: <the spoken voice-over line, Uzbek>
Ekranda: <short on-screen text, Uzbek>
3-7s: Ko'rinish: ...
Ovoz: ...
Ekranda: ...
...
Rules: total length = the requested seconds; EVERY time segment is at most 5 seconds long; about 2-2.5 spoken words per second; the first segment is a strong hook;
the last segment is a call to action that names the brand; the visuals must fit the business activity and tone; use only people roles that make sense
(for example Instruktor, Talaba, Mijoz) or the named people given; do not invent facts, prices or phone numbers.
Return ONLY JSON: {"title": "short project title in Uzbek", "script": "the full script text with line breaks"}"""


async def write_script(brand: dict, topic: str, seconds: int, extra: str, people: list[dict]) -> dict:
    seconds = max(15, min(int(seconds or 30), 60))
    names = "; ".join(f"{p['name']} ({D.KINDS.get(p['kind'], p['kind'])})" for p in people) or "none"
    user = (f"{D.brand_text(brand)}\n\nVideo topic: {topic}\nTotal length: {seconds} seconds\nKnown people of this brand: {names}\n"
            + (f"Extra wishes: {extra}\n" if extra.strip() else ""))
    data = await text_json(SCRIPT_SYSTEM, user, note="ssenariy yozish", creative=True, max_out=5000)
    script = str(data.get("script", "")).strip() if isinstance(data, dict) else ""
    if not script:
        raise ai.AIError("AI ssenariy yozib bera olmadi, qayta urinib ko'ring")
    return {"title": str(data.get("title", "")).strip()[:120], "script": script}


# ---------------------------------------------------------------- ssenariyni tahlil qilish
ANALYZE_SYSTEM = """You analyse a short-video script written in Uzbek and split it into scenes for an AI image-prompt + video-prompt pipeline.
Return ONLY JSON of this shape:
{"title": "short Uzbek title",
 "scenes": [{"t_from": 0, "t_to": 3, "label": "Hook", "visual": "what is seen (Uzbek, from the script's 'Ko'rinish')", "voice": "spoken line (Uzbek, exactly from the script's 'Ovoz')",
             "onscreen": "on-screen text (Uzbek, from 'Ekranda', may be empty)", "roles": ["role_key"], "location": "location_key", "brand_card": false}],
 "roles": [{"key": "role_key", "title": "Uzbek role name, e.g. Instruktor", "description": "Uzbek: who this is and what the person looks like/wears in this video (use the business context)",
            "person_id": null}],
 "locations": [{"key": "location_key", "name": "short Uzbek name", "description": "Uzbek: detailed look of this place suited to the business activity (walls, light, furniture, props, colours)", "library_id": null}]}
Rules:
- Keep the script's order and wording; do NOT invent new content. Each scene is one still image + one video clip of AT MOST 5 seconds. If a script segment is longer than 5 s, split it into several scenes.
  If the script has no time ranges, assign them so that the voice lines fit (about 2.3 Uzbek words per second).
- roles: every distinct real person that appears (for example the instructor, a student, the owner). Keys: short lowercase latin words. A scene lists the keys of the people visible in it ([] if nobody).
- person_id: set ONLY if a person from the supplied brand library clearly matches the role (by name or kind); else null.
- locations: every distinct setting. Invent a fitting description for the brand's business activity if the script does not give one. library_id: set ONLY if a supplied library location is clearly the same place; else null.
- brand_card: true only for the closing scene that shows the brand card / logo / call to action without a person; in such scenes do not invent a person.
- Everything shown to the user (title, label, visual, voice, onscreen, role title/description, location name/description) is UZBEK. Keys are latin."""


def _slug(s: str, default: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", (s or "").lower()).strip("_")
    return s[:40] or default


def _split_words(text: str, n: int) -> list[str]:
    w = (text or "").split()
    if n <= 1 or len(w) <= 1:
        return [text or ""] + [""] * (n - 1)
    size = math.ceil(len(w) / n)
    parts = [" ".join(w[i * size:(i + 1) * size]) for i in range(n)]
    return parts


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize_analysis(data: dict) -> dict:
    """AI javobini tekshiradi va tuzatadi: sahnalar <= 5 s, kalitlar mosligi, slotlar 1..5. (Sinash uchun alohida funksiya.)"""
    if not isinstance(data, dict) or not isinstance(data.get("scenes"), list) or not data["scenes"]:
        raise ai.AIError("Ssenariyni sahnalarga bo'lib bo'lmadi. Ssenariy matnini tekshirib, qayta urinib ko'ring.")
    roles_in, locs_in = {}, {}
    for r in data.get("roles") or []:
        if isinstance(r, dict) and (r.get("title") or r.get("key")):
            k = _slug(r.get("key") or r.get("title"), f"role{len(roles_in) + 1}")
            roles_in[k] = r
    for lo in data.get("locations") or []:
        if isinstance(lo, dict) and (lo.get("name") or lo.get("key")):
            k = _slug(lo.get("key") or lo.get("name"), f"loc{len(locs_in) + 1}")
            locs_in[k] = lo
    scenes, t = [], 0.0
    for sc in data["scenes"]:
        if not isinstance(sc, dict):
            continue
        a, b = _f(sc.get("t_from")), _f(sc.get("t_to"))
        if a is None or b is None or b <= a:
            a, b = t, t + 4.0
        keys = [_slug(x, "") for x in (sc.get("roles") or []) if isinstance(x, str)]
        lk = _slug(sc.get("location") or "", "") if sc.get("location") else ""
        base = {"label": str(sc.get("label") or "").strip()[:80], "visual": str(sc.get("visual") or "").strip(), "voice": str(sc.get("voice") or "").strip(),
                "onscreen": str(sc.get("onscreen") or "").strip(), "roles": [k for k in dict.fromkeys(keys) if k],
                "location": lk, "kind": "brand" if sc.get("brand_card") else "normal"}
        d = b - a
        n = max(1, math.ceil(d / D.MAX_SCENE_SEC - 1e-9))
        voices = _split_words(base["voice"], n)
        for i in range(n):
            part = dict(base)
            part["t_from"], part["t_to"] = round(a + d * i / n, 2), round(a + d * (i + 1) / n, 2)
            part["duration"] = round(d / n, 2)
            part["voice"] = voices[i]
            if i:
                part["onscreen"] = ""
                part["label"] = (base["label"] + f" (davomi {i + 1}/{n})").strip()
            elif n > 1:
                part["label"] = (base["label"] + f" (1/{n})").strip()
            scenes.append(part)
        t = b
    if not scenes:
        raise ai.AIError("Ssenariyda sahna topilmadi")
    for i, s in enumerate(scenes, 1):
        s["idx"] = i
    used_roles = []
    for s in scenes:
        for k in s["roles"]:
            if k not in used_roles:
                used_roles.append(k)
    roles = []
    for k in used_roles:
        r = roles_in.get(k, {})
        roles.append({"key": k, "title": str(r.get("title") or k).strip()[:120], "description": str(r.get("description") or "").strip(),
                      "person_id": r.get("person_id") if isinstance(r.get("person_id"), int) else None, "slots": []})
    used_locs = []
    for s in scenes:
        if s["location"] and s["location"] not in used_locs:
            used_locs.append(s["location"])
    if not used_locs:
        used_locs = list(locs_in)[:1] or ["main"]
        for s in scenes:
            s["location"] = s["location"] or used_locs[0]
    for s in scenes:
        if not s["location"]:
            s["location"] = used_locs[0]
    locs = []
    for k in used_locs:
        lo = locs_in.get(k, {})
        locs.append({"key": k, "name": str(lo.get("name") or "Asosiy joy").strip()[:120], "description": str(lo.get("description") or "").strip(),
                     "library_id": lo.get("library_id") if isinstance(lo.get("library_id"), int) else None})
    return {"title": str(data.get("title") or "").strip()[:200], "scenes": scenes, "roles": roles, "locations": locs}


async def analyze(brand: dict, script: str, fmt: str, ppl: list[dict], locs: list[dict], ctx=None) -> dict:
    lib_p = "\n".join(f"- id {p['id']}: {p['name']} ({D.KINDS.get(p['kind'], p['kind'])}) {p.get('appearance') or ''}".strip() for p in ppl) or "(empty)"
    lib_l = "\n".join(f"- id {lo['id']}: {lo['name']} — {(lo['description'] or '')[:200]}" for lo in locs) or "(empty)"
    user = (f"{D.brand_text(brand)}\n\nVideo format: {fmt}\n\nBrand people library:\n{lib_p}\n\nBrand locations library:\n{lib_l}\n\n"
            f"SCRIPT (Uzbek):\n{script}")
    data = await text_json(ANALYZE_SYSTEM, user, ctx=ctx, note="ssenariy tahlili", max_out=9000, effort="low")
    res = normalize_analysis(data)
    pid_ok = {p["id"] for p in ppl}
    lid_ok = {lo["id"] for lo in locs}
    for r in res["roles"]:
        if r["person_id"] not in pid_ok:
            r["person_id"] = None
    for lo in res["locations"]:
        if lo["library_id"] not in lid_ok:
            lo["library_id"] = None
    return res



# ---------------------------------------------------------------- odam va joylar: qisqa ingliz tavsifi (bir marta)
IDENT_SYSTEM = """You write short FIXED English descriptors that keep people and places identical across all prompts of one video.
Return ONLY JSON: {"people": {"<key>": "..."}, "places": {"<key>": "..."}}.
people: 25-45 words each: apparent age range, gender, build, hair, facial hair, glasses, skin tone and the clothing that fits this video and the business. Use ONLY facts from the notes;
if something is unknown stay neutral and do not invent distinctive facial features (the real face is defined by the person's reference photos, never describe eye/nose/lip shapes).
places: 30-50 words each: layout, materials, colours, key props, light, mood, suited to the business activity."""


async def character_sheets(brand: dict, roles: list[dict], plocs: list[dict], ctx=None) -> tuple[dict, dict]:
    """roles/plocs: faqat identity/place_en yo'qlari uchun chaqiriladi. (rkey->identity, lkey->place) qaytaradi."""
    if not roles and not plocs:
        return {}, {}
    pl = "\n".join(f"- key {r['rkey']}: role «{r['title']}»; person: {(r.get('person') or {}).get('name', '(not chosen)')}; "
                   f"notes (Uzbek): {(r.get('person') or {}).get('appearance') or ''} {r['description']}".strip() for r in roles) or "(none)"
    lc = "\n".join(f"- key {p['lkey']}: «{p['name']}» (Uzbek): {p['description']}" for p in plocs) or "(none)"
    user = f"{D.brand_text(brand)}\n\nPEOPLE:\n{pl}\n\nPLACES:\n{lc}"
    data = await text_json(IDENT_SYSTEM, user, ctx=ctx, note="odam va joy tavsifi", max_out=2500, effort="low")
    people = {k: str(v).strip() for k, v in ((data or {}).get("people") or {}).items() if str(v).strip()}
    places = {k: str(v).strip() for k, v in ((data or {}).get("places") or {}).items() if str(v).strip()}
    return people, places


# ---------------------------------------------------------------- rasm va video promptlari (paket)
PROMPTS_SYSTEM = """You are a senior art director and prompt engineer. You do NOT generate anything: you only WRITE prompts that the user will paste into external tools.
For every scene write:
 - "image": ONE prompt for the chosen IMAGE platform (format and length exactly as that platform's rules say). The image is the FIRST FRAME of a short video (<= 5 s): choose a pose and framing that can be animated naturally.
 - "video": an object with one prompt per chosen VIDEO platform (key = the platform name exactly), each following that platform's own rules. They animate THAT image: the supplied image is the first frame, so describe only motion, camera and ambient movement that fit the scene and the voice line.
 - "audio": ONE audio prompt (English instructions): voice character (gender/age/personality inferred from the speaker's role), tone and emotion, speaking pace (target words per second so the line fits the clip length), language 'Uzbek (uz-UZ)',
   the EXACT spoken line in quotes (unchanged Uzbek), and a short note on background music mood/level and sound effects. If there is no voice line, describe only music/ambience.
Return ONLY JSON: {"scenes": [{"idx": 1, "image": "...", "video": {"<platform>": "..."}, "audio": "..."}]} with one entry per requested scene.
Hard rules:
- Stay strictly inside the script: use only the scene's own description, people, place and voice line. Never invent new events, people, dialogue, brands or props.
- IDENTITY: for every person visible in a scene put that person's FIXED DESCRIPTOR (given below) into the image prompt and into each video prompt, together with the platform-appropriate sentence that the user's attached reference photos define the exact face
  (same face, hairstyle, age and build; never change the identity). Use the person's role title, not a made-up name. Never describe eye, nose or lip shapes yourself.
- PLACE: repeat the place's FIXED DESCRIPTOR so the setting is identical in every scene of that place.
- Keep clothing, lighting and colour mood consistent between scenes (continuity), following the previous scene note when given.
- No readable text, letters, numbers, logos or watermarks anywhere in the image or video. On-screen text and logos are added later in editing. A brand-card / call-to-action scene has no person: build a clean modern composition and keep a clean empty area where the logo will be placed.
- All prompts are in ENGLISH (only the quoted spoken line stays Uzbek). Respect each platform's word counts and format. Clip length = the scene's duration in seconds (never more than 5)."""


def _scene_block(sc: dict, roles_by_key: dict, places_by_key: dict) -> str:
    people = []
    for k in sc.get("roles") or []:
        r = roles_by_key.get(k)
        if r:
            people.append(f"{r['title']} [{r.get('identity') or r['description']}]")
    pl = places_by_key.get(sc.get("lkey")) or {}
    words = len((sc.get("voice") or "").split())
    return (f"### Scene idx={sc['idx']} ({sc.get('label') or ''}); duration {sc['duration']:g} s; "
            f"{'BRAND CARD (no person)' if sc.get('kind') == 'brand' else 'normal'}\n"
            f"Seen (Uzbek): {sc['visual']}\nVoice line (Uzbek, {words} words): {sc.get('voice') or '(none)'}\n"
            f"On-screen text (NOT drawn; added in editing): {sc.get('onscreen') or '(none)'}\n"
            f"People: {'; '.join(people) or 'nobody'}\nPlace: «{pl.get('name', '')}» [{pl.get('place_en') or pl.get('description') or ''}]")


def _platform_text(img: dict | None, vids: list[dict]) -> str:
    out = ["IMAGE PLATFORM: " + (f"{img['name']}\n{img['rules']}" if img else "generic (plain descriptive English prompt, 70-120 words)")]
    for v in vids:
        out.append(f"VIDEO PLATFORM «{v['name']}» (max clip {v['max_sec']:g} s, {'generates audio itself' if v['audio'] else 'no audio'}):\n{v['rules']}")
    if not vids:
        out.append("VIDEO PLATFORMS: none requested: use an empty object for \"video\".")
    return "\n\n".join(out)


def _clean_batch(data, scenes: list[dict], vids: list[dict]) -> dict[int, dict]:
    """AI javobini tekshiradi: {idx: {'image','video','audio'}} faqat to'liq yozilgan sahnalar uchun."""
    items = (data or {}).get("scenes") if isinstance(data, dict) else None
    out = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("idx"))
        except (TypeError, ValueError):
            continue
        img = str(it.get("image") or "").strip()
        vd = it.get("video") if isinstance(it.get("video"), dict) else {}
        video = {}
        for v in vids:
            t = vd.get(v["name"]) or next((x for k, x in vd.items() if str(k).lower() == v["name"].lower()), "")
            if isinstance(t, str) and t.strip():
                video[v["name"]] = t.strip()
        if img and len(video) == len(vids):
            out[idx] = {"image": img, "video": video, "audio": str(it.get("audio") or "").strip()}
    return {i: out[i] for i in out if i in {s["idx"] for s in scenes}}


async def write_prompts(brand: dict, project: dict, scenes: list[dict], roles: list[dict], plocs: list[dict],
                        img: dict | None, vids: list[dict], *, prev_visual="", old: dict | None = None, note="", variant=False, ctx=None) -> dict[int, dict]:
    """Bir necha sahna uchun rasm + video + audio promptlarini BITTA so'rovda yozadi. old/note/variant: bitta sahnani qayta yozish uchun."""
    roles_by_key = {r["rkey"]: r for r in roles}
    places_by_key = {p["lkey"]: p for p in plocs}
    style = D.STYLES.get(project["style"], D.STYLES["real"])[1]
    user = (f"{D.brand_text(brand)}\n\nAspect ratio of the final video/images: {project['fmt']}\nVisual style: {style}\n\n{_platform_text(img, vids)}\n\n"
            + (f"Previous scene (continuity): {prev_visual}\n\n" if prev_visual else "")
            + "SCENES TO WRITE:\n" + "\n\n".join(_scene_block(s, roles_by_key, places_by_key) for s in scenes))
    if old:
        user += f"\n\nCurrent prompts of this scene (rewrite them):\nIMAGE: {old.get('image', '')}\n"
        if note:
            user += f"The user wants this change (Uzbek): {note}\nApply it to the image prompt, keep everything else, and rewrite every video and audio prompt so they match the NEW image.\n"
        elif variant:
            user += "Write a clearly different variant (another pose, framing and camera angle) of the SAME scene content; rewrite video and audio prompts to match.\n"
    n = len(scenes)
    data = await text_json(PROMPTS_SYSTEM, user, ctx=ctx, note=f"promptlar ({scenes[0]['idx']}-{scenes[-1]['idx']}-sahna)",
                           max_out=min(16000, 1200 * n + 400 * n * max(1, len(vids)) + 600), effort="low")
    return _clean_batch(data, scenes, vids)
