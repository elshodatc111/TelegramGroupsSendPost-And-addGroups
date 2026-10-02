"""Image Generator (6-bo'lim): sxema, sozlamalar, brend / odam / joy / loyiha / sahna ma'lumotlari.

Bu modul boshqa bo'limlar jadvallariga tegmaydi: faqat img_* jadvallari. OpenAI kaliti (openai_key) va AI sarfi (ch_usage)
umumiy; qolgan sozlamalar `settings` jadvalida `img_` old qo'shimchasi bilan saqlanadi.
Fayllar: data/imagegen/{brands,people,locations,projects}/...
"""
import json
import re
import shutil
import uuid
from pathlib import Path

from . import ai, db, schema_ch
from .config import DATA_DIR, log

IMG_DIR = DATA_DIR / "imagegen"
T = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
TABLES = [
    f"""CREATE TABLE IF NOT EXISTS img_brands(
        id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(190), logo VARCHAR(300), activity TEXT, about TEXT, audience TEXT,
        tone TEXT, colors VARCHAR(200), avoid TEXT, slogan VARCHAR(300), created_at VARCHAR(32)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS img_people(
        id INT AUTO_INCREMENT PRIMARY KEY, brand_id INT NOT NULL, name VARCHAR(190), kind VARCHAR(20), appearance TEXT,
        consent INT DEFAULT 0, note TEXT, created_at VARCHAR(32), INDEX idx_ipe(brand_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS img_photos(
        id INT AUTO_INCREMENT PRIMARY KEY, person_id INT NOT NULL, path VARCHAR(300), label VARCHAR(200), created_at VARCHAR(32),
        INDEX idx_iph(person_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS img_locations(
        id INT AUTO_INCREMENT PRIMARY KEY, brand_id INT NOT NULL, name VARCHAR(190), description TEXT, prompt_en TEXT,
        master VARCHAR(300), master_at VARCHAR(32), master_src VARCHAR(10), created_at VARCHAR(32), INDEX idx_ilo(brand_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS img_projects(
        id INT AUTO_INCREMENT PRIMARY KEY, brand_id INT NOT NULL, title VARCHAR(255), script LONGTEXT, topic TEXT, fmt VARCHAR(10),
        quality VARCHAR(10), style VARCHAR(20), image_platform VARCHAR(80), video_platforms TEXT, status VARCHAR(16) DEFAULT 'new', progress INT DEFAULT 0, msg VARCHAR(300),
        error TEXT, cost DOUBLE DEFAULT 0, ver INT DEFAULT 0, created_at VARCHAR(32), started_at VARCHAR(32), finished_at VARCHAR(32),
        INDEX idx_ipr(brand_id, status)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS img_roles(
        id INT AUTO_INCREMENT PRIMARY KEY, project_id INT NOT NULL, rkey VARCHAR(60), title VARCHAR(190), description TEXT,
        person_id INT, slots LONGTEXT, identity TEXT, INDEX idx_iro(project_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS img_plocs(
        id INT AUTO_INCREMENT PRIMARY KEY, project_id INT NOT NULL, lkey VARCHAR(60), name VARCHAR(190), description TEXT,
        location_id INT, place_en TEXT, INDEX idx_ipl(project_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS img_scenes(
        id INT AUTO_INCREMENT PRIMARY KEY, project_id INT NOT NULL, idx INT, t_from DOUBLE, t_to DOUBLE, duration DOUBLE,
        label VARCHAR(190), kind VARCHAR(12), visual TEXT, voice TEXT, onscreen TEXT, roles LONGTEXT, lkey VARCHAR(60),
        status VARCHAR(16) DEFAULT 'pending', active_ver INT DEFAULT 0, error TEXT, warn TEXT, updated_at VARCHAR(32),
        INDEX idx_isc(project_id, idx)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS img_versions(
        id INT AUTO_INCREMENT PRIMARY KEY, scene_id INT NOT NULL, n INT, image_prompt LONGTEXT, file VARCHAR(300), note TEXT,
        qa_ok INT, qa_json LONGTEXT, cost DOUBLE DEFAULT 0, model VARCHAR(80), created_at VARCHAR(32), INDEX idx_ive(scene_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS img_prompts(
        id INT AUTO_INCREMENT PRIMARY KEY, scene_id INT NOT NULL, version_id INT NOT NULL, kind VARCHAR(10), platform VARCHAR(80),
        text LONGTEXT, created_at VARCHAR(32), INDEX idx_ipp(scene_id, version_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS img_platforms(
        id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(80), kind VARCHAR(10) DEFAULT 'video', active INT DEFAULT 1, max_sec DOUBLE DEFAULT 5, rules LONGTEXT,
        audio INT DEFAULT 0, sort INT DEFAULT 0, builtin INT DEFAULT 0, UNIQUE KEY u_ipf(name)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS img_jobs(
        id INT AUTO_INCREMENT PRIMARY KEY, project_id INT, kind VARCHAR(16), scene_id INT, payload LONGTEXT,
        status VARCHAR(12) DEFAULT 'queued', msg VARCHAR(300), error TEXT, created_at VARCHAR(32), started_at VARCHAR(32),
        finished_at VARCHAR(32), INDEX idx_ij(status, id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS img_costs(
        id INT AUTO_INCREMENT PRIMARY KEY, ts VARCHAR(32), project_id INT, scene_id INT, kind VARCHAR(20), model VARCHAR(80),
        cost DOUBLE DEFAULT 0, note VARCHAR(255), INDEX idx_ico(project_id)) {T}""",
]
TABLE_NAMES = ["img_brands", "img_people", "img_photos", "img_locations", "img_projects", "img_roles", "img_plocs", "img_scenes",
               "img_versions", "img_prompts", "img_platforms", "img_jobs", "img_costs"]
_ready = False

MAX_SCENE_SEC = 5.0          # har bir sahna (video klip) uzog'i bilan 5 soniya
FORMATS = {                  # format: yorliq (faqat promptga yoziladi: hech narsa kesilmaydi/yaratilmaydi)
    "9:16": "9:16 vertikal (Reels, Shorts, TikTok)",
    "16:9": "16:9 gorizontal (YouTube, taqdimot)",
    "1:1": "1:1 kvadrat (post)",
    "4:5": "4:5 portret (Instagram post)",
    "2:3": "2:3 portret",
    "3:2": "3:2 gorizontal",
}
STYLES = {
    "real": ("Fotorealistik", "photorealistic, natural light, realistic skin and fabric textures, shot on a full-frame camera, 35mm lens"),
    "cine": ("Kinematografik", "cinematic still, soft film grain, dramatic but natural lighting, shallow depth of field, color graded"),
    "3d": ("3D multfilm", "stylized 3D animation look, clean shapes, soft global illumination, vibrant but tasteful colors"),
    "flat": ("Yassi illyustratsiya", "modern flat vector illustration, clean shapes, limited harmonious palette, subtle gradients"),
}
STATUS_UZ = {"new": "Yangi", "analyzing": "Tahlil qilinmoqda", "needs_assets": "Odamlar kutilmoqda", "running": "Promptlar yozilmoqda",
             "done": "Tayyor", "partial": "Tayyor (xatoliklar bor)", "error": "Xato", "stopped": "To'xtatildi"}
SCENE_UZ = {"pending": "Kutmoqda", "writing": "Promptlar yozilmoqda", "done": "Tayyor", "error": "Xato"}

DEFAULTS = {"model_text": "", "model_script": "", "batch": "5"}
CAM_SHOTS = [            # kamera bilan ro'yxatga olish: (fayl belgisi, o'zbekcha ko'rsatma)
    ("front", "To'g'ri kameraga qarang, neytral yuz"),
    ("left34", "Boshingizni chapga 3/4 burchakka buring"),
    ("right34", "Boshingizni o'ngga 3/4 burchakka buring"),
    ("smile", "Tabassum qiling (tishlar ko'rinsin)"),
    ("up", "Biroz yuqoriga qarang (iyak va bo'yin ko'rinsin)"),
]

CAM_LABELS = {"front": "To'g'ri", "left34": "Chap 3/4", "right34": "O'ng 3/4", "smile": "Tabassum", "up": "Yuqoriga qarash"}

# (nomi, turi, klip uzunligi, audio, qoidalar). Rasm platformalari: prompt formati har birida har xil; qoidalarni Sozlamalarda o'zgartirish mumkin.
PLATFORMS = [
    ("Sora", "video", 4, 1, "Image-to-video. Write ONE flowing paragraph of natural cinematic language (60-110 words): what happens over time "
     "(subject and action), camera movement (slow push-in, gentle pan, handheld, locked-off), lighting and mood. The supplied image is the "
     "first frame, so do not re-describe static details at length: focus on what MOVES. Keep physics realistic and motion subtle for faces. "
     "State the clip length. No on-screen text or logos. End with one short 'Audio:' line (ambient sound, and the spoken line in quotes if the scene has voice)."),
    ("Veo", "video", 8, 1, "Veo (image-to-video) prompt as a single structured paragraph in this order: Subject, Action, Camera motion, Style and lighting, "
     "then an 'Audio:' line with ambient sound and the dialogue in quotes (spoken language: Uzbek). 50-90 words. The supplied image is the "
     "first frame. Mention the exact duration. No text overlays, no subtitles."),
    ("Kling", "video", 5, 0, "Concise prompt, 40-70 words: subject, motion, camera move (for example 'slow dolly in'), atmosphere. Image-to-video, the "
     "image is the first frame. After the prompt add a separate last line starting with 'Negative prompt:' (blurry, distorted face, extra fingers, "
     "morphing, text, watermark, jitter). Duration 5 s."),
    ("Runway", "video", 5, 0, "Runway image-to-video: describe ONLY the motion and the camera, never the content of the image. 20-50 words, positive phrasing only, "
     "no negative prompts. Example shape: 'The subject <action>. The camera <move>. <ambient motion>. Smooth, natural, stable.'"),
    ("Leonardo", "video", 5, 0, "Leonardo Motion (image-to-video): 30-60 words, plain concise sentences. Describe the subject's movement, the camera movement "
     "and subtle ambient motion; ask for smooth natural motion and a stable face. No text overlays."),
    ("gpt-image (ChatGPT)", "image", 0, 0, "Natural-language prompt, 70-130 words, plain descriptive sentences: subject and exact pose/expression, clothing, setting and key props, "
     "camera angle and shot size, lighting and colour mood. The user attaches the reference photos of each person to the chat: write 'Use the attached reference photos of <person>; keep the exact face, hairstyle, age and build'. "
     "End with the aspect ratio in words (for example 'Vertical 9:16 composition')."),
    ("Midjourney", "image", 0, 0, "Midjourney prompt: one line of comma-separated descriptive phrases (50-90 words): subject, action, clothing, setting, camera/lens, lighting, mood, style. "
     "Do NOT write full sentences about references; instead put the token '{CREF}' where the user will paste the character reference URL (--cref {CREF}) when people are present. "
     "End with parameters: '--ar 9:16' (use the project's aspect ratio) and '--style raw'. No text in the picture."),
    ("Flux", "image", 0, 0, "Flux prompt: 60-100 words of clear natural language, most important elements first (subject, action, setting), then lighting, lens, mood. "
     "Mention that the person must match the supplied reference image. No negative prompt, no keyword soup."),
    ("Imagen (Gemini)", "image", 0, 0, "Imagen/Gemini image prompt: 2-4 descriptive sentences (60-110 words) with photographic terms (lens, light, composition). "
     "When people are present say 'the person from the attached reference photos, same face and features'. State the aspect ratio in words."),
    ("Leonardo (rasm)", "image", 0, 0, "Leonardo image prompt: 50-90 words of descriptive phrases: subject, action, setting, lighting, camera, style. "
     "For people say 'character reference: same face as the supplied reference images'. End with 'Aspect ratio: 9:16' (use the project's ratio)."),
]


# ---------------------------------------------------------------- sxema
def _ensure_columns():
    """Avvalgi versiyada yaratilgan jadvallarga yangi ustunlarni qo'shadi (mavjud ma'lumot saqlanadi)."""
    need = [("img_platforms", "kind", "VARCHAR(10) DEFAULT 'video'"), ("img_projects", "image_platform", "VARCHAR(80)"),
            ("img_projects", "video_platforms", "TEXT"), ("img_roles", "identity", "TEXT"), ("img_plocs", "place_en", "TEXT")]
    for t, c, ddl in need:
        try:
            db.one(f"SELECT {c} FROM {t} LIMIT 1")
        except Exception:
            try:
                db.ex(f"ALTER TABLE {t} ADD COLUMN {c} {ddl}")
            except Exception:
                log.exception("Ustun qo'shilmadi: %s.%s", t, c)


def ensure_schema():
    global _ready
    if db.IS_MYSQL:
        from . import mysqldb
        for st in TABLES:
            mysqldb.raw(st)
    else:
        for st in TABLES:
            create, idx = schema_ch._to_sqlite(st)
            db.ex(create)
            for i in idx:
                db.ex(i)
    _ensure_columns()
    for i, p in enumerate(PLATFORMS):
        db.ex("INSERT OR IGNORE INTO img_platforms(name,kind,active,max_sec,rules,audio,sort,builtin) VALUES(?,?,?,?,?,?,?,1)",
              (p[0], p[1], 1, p[2], p[4], p[3], i))
    db.ex("UPDATE img_platforms SET kind='video' WHERE kind IS NULL OR kind=''")
    # dastur qayta ishga tushganda yarim qolgan ishlar navbatga qaytariladi (qadamlar qayta bajarilganda tugallanganlari o'tkazib yuboriladi)
    db.ex("UPDATE img_jobs SET status='queued', msg='Qayta ishga tushirildi' WHERE status='running'")
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    _ready = True


def ready():
    if not _ready:
        ensure_schema()


def now() -> str:
    return db.now()


def jl(s, default=None):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


def jd(o) -> str:
    return json.dumps(o, ensure_ascii=False)


def row(r):
    return dict(r) if r is not None else None


def rows(sql, args=()):
    return [dict(r) for r in db.q(sql, args)]


# ---------------------------------------------------------------- sozlamalar
def cfg(key):
    v = db.get_setting("img_" + key)
    return DEFAULTS.get(key, "") if v in (None, "") else v


def set_cfg(key, value):
    db.set_setting("img_" + key, str(value))


def flag(key) -> bool:
    return str(cfg(key)) not in ("0", "", "false", "False")


def num(key, default=0.0) -> float:
    try:
        return float(cfg(key))
    except (TypeError, ValueError):
        return default


def model_text() -> str:
    return cfg("model_text") or ai.model_for("analysis")


def model_script() -> str:
    return cfg("model_script") or ai.model_for("idea")


def add_cost(project_id, scene_id, kind, model, cost, note=""):
    cost = float(cost or 0)
    db.ex("INSERT INTO img_costs(ts,project_id,scene_id,kind,model,cost,note) VALUES(?,?,?,?,?,?,?)",
          (now(), project_id, scene_id, kind, model[:80], cost, note[:255]))
    if project_id:
        db.ex("UPDATE img_projects SET cost=cost+? WHERE id=?", (cost, project_id))


def total_cost(days: int | None = None) -> float:
    if days:
        from datetime import datetime, timedelta
        cut = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
        r = db.one("SELECT COALESCE(SUM(cost),0) c FROM img_costs WHERE ts >= ?", (cut,))
    else:
        r = db.one("SELECT COALESCE(SUM(cost),0) c FROM img_costs")
    return float(r["c"] or 0)


# ---------------------------------------------------------------- fayllar
def safe_name(s: str, default="file") -> str:
    s = re.sub(r"[^\w\-. ]+", "", s or "", flags=re.U).strip().replace(" ", "_")
    return s[:60] or default


def new_path(*parts: str, ext=".jpg") -> tuple[Path, str]:
    """(to'liq yo'l, IMG_DIR ga nisbatan yo'l) — katalog yaratiladi, fayl nomi noyob."""
    d = IMG_DIR.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex[:10]}{ext}"
    return d / name, "/".join(list(parts) + [name])


def resolve(rel: str) -> Path | None:
    """Nisbiy yo'lni xavfsiz tarzda to'liq yo'lga aylantiradi (IMG_DIR dan chiqib ketmasligi shart)."""
    if not rel:
        return None
    p = (IMG_DIR / rel).resolve()
    try:
        p.relative_to(IMG_DIR.resolve())
    except ValueError:
        return None
    return p if p.is_file() else None


def rm_file(rel: str):
    p = resolve(rel)
    if p:
        try:
            p.unlink()
        except OSError:
            log.warning("Fayl o'chirilmadi: %s", rel)


def rm_tree(*parts: str):
    d = IMG_DIR.joinpath(*parts)
    if d.exists() and d.resolve() != IMG_DIR.resolve():
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- brendlar
BRAND_FIELDS = ("name", "activity", "about", "audience", "tone", "colors", "avoid", "slogan")


def brands():
    out = rows("SELECT * FROM img_brands ORDER BY name")
    for b in out:
        b["n_people"] = db.one("SELECT COUNT(*) c FROM img_people WHERE brand_id=?", (b["id"],))["c"]
        b["n_locs"] = db.one("SELECT COUNT(*) c FROM img_locations WHERE brand_id=?", (b["id"],))["c"]
        b["n_proj"] = db.one("SELECT COUNT(*) c FROM img_projects WHERE brand_id=?", (b["id"],))["c"]
    return out


def brand(bid):
    return row(db.one("SELECT * FROM img_brands WHERE id=?", (bid,)))


def save_brand(form: dict, bid=None) -> tuple[int | None, str | None]:
    v = {k: (form.get(k) or "").strip() for k in BRAND_FIELDS}
    if not v["name"]:
        return None, "Brend nomini kiriting"
    if not v["activity"]:
        return None, "Brendning faoliyat turini kiriting: AI rasmlarni shunga moslab tayyorlaydi"
    if bid:
        db.ex("UPDATE img_brands SET name=?,activity=?,about=?,audience=?,tone=?,colors=?,avoid=?,slogan=? WHERE id=?",
              (v["name"], v["activity"], v["about"], v["audience"], v["tone"], v["colors"], v["avoid"], v["slogan"], bid))
        return bid, None
    nid = db.ex("INSERT INTO img_brands(name,activity,about,audience,tone,colors,avoid,slogan,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (v["name"], v["activity"], v["about"], v["audience"], v["tone"], v["colors"], v["avoid"], v["slogan"], now()))
    return nid, None


def delete_brand(bid) -> str | None:
    if db.one("SELECT 1 FROM img_projects WHERE brand_id=? AND status IN ('analyzing','running')", (bid,)):
        return "Brend loyihalari hozir ishlayapti: avval to'xtating"
    for p in rows("SELECT id FROM img_projects WHERE brand_id=?", (bid,)):
        delete_project(p["id"])
    for pe in rows("SELECT id FROM img_people WHERE brand_id=?", (bid,)):
        delete_person(pe["id"])
    for lo in rows("SELECT id FROM img_locations WHERE brand_id=?", (bid,)):
        delete_location(lo["id"])
    b = brand(bid)
    if b and b["logo"]:
        rm_file(b["logo"])
    rm_tree("brands", str(bid))
    db.ex("DELETE FROM img_brands WHERE id=?", (bid,))
    return None


def brand_text(b: dict) -> str:
    """AI uchun brend tavsifi (inglizcha sarlavhalar, mazmun foydalanuvchi tilida)."""
    parts = [f"Brand: {b['name']}", f"Business activity: {b['activity']}"]
    for k, t in (("about", "About"), ("audience", "Audience"), ("tone", "Tone"), ("colors", "Brand colors"), ("slogan", "Slogan"), ("avoid", "Never show")):
        if (b.get(k) or "").strip():
            parts.append(f"{t}: {b[k].strip()}")
    return "\n".join(parts)


# ---------------------------------------------------------------- odamlar
KINDS = {"staff": "Xodim", "teacher": "O'qituvchi", "client": "Mijoz", "owner": "Biznes egasi", "other": "Boshqa"}


def people(bid=None):
    out = rows("SELECT * FROM img_people" + (" WHERE brand_id=?" if bid else "") + " ORDER BY name", (bid,) if bid else ())
    for p in out:
        p["photos"] = photos(p["id"])
    return out


def person(pid):
    p = row(db.one("SELECT * FROM img_people WHERE id=?", (pid,)))
    if p:
        p["photos"] = photos(pid)
    return p


def photos(pid):
    return rows("SELECT * FROM img_photos WHERE person_id=? ORDER BY id", (pid,))


def photo(phid):
    return row(db.one("SELECT * FROM img_photos WHERE id=?", (phid,)))


def save_person(bid, form: dict, pid=None) -> tuple[int | None, str | None]:
    name = (form.get("name") or "").strip()
    if not name:
        return None, "Odam ismini kiriting"
    kind = form.get("kind") if form.get("kind") in KINDS else "staff"
    app_ = (form.get("appearance") or "").strip()
    consent = 1 if form.get("consent") else 0
    note = (form.get("note") or "").strip()
    if pid:
        db.ex("UPDATE img_people SET name=?,kind=?,appearance=?,consent=?,note=? WHERE id=?", (name, kind, app_, consent, note, pid))
        return pid, None
    nid = db.ex("INSERT INTO img_people(brand_id,name,kind,appearance,consent,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (bid, name, kind, app_, consent, note, now()))
    return nid, None


def add_photo(pid, rel: str, label="") -> int:
    return db.ex("INSERT INTO img_photos(person_id,path,label,created_at) VALUES(?,?,?,?)", (pid, rel, label[:200], now()))


def delete_photo(phid) -> str | None:
    ph = photo(phid)
    if not ph:
        return None
    rm_file(ph["path"])
    db.ex("DELETE FROM img_photos WHERE id=?", (phid,))
    return None


def delete_person(pid):
    for ph in photos(pid):
        delete_photo(ph["id"])
    db.ex("UPDATE img_roles SET person_id=NULL WHERE person_id=?", (pid,))
    rm_tree("people", str(pid))
    db.ex("DELETE FROM img_people WHERE id=?", (pid,))


# ---------------------------------------------------------------- joylar (kutubxona: brendga bog'liq)
def locations(bid=None):
    return rows("SELECT * FROM img_locations" + (" WHERE brand_id=?" if bid else "") + " ORDER BY brand_id, name", (bid,) if bid else ())


def location(lid):
    return row(db.one("SELECT * FROM img_locations WHERE id=?", (lid,)))


def save_location(bid, name: str, description: str, lid=None) -> tuple[int | None, str | None]:
    name, description = (name or "").strip(), (description or "").strip()
    if not name:
        return None, "Joy nomini kiriting"
    if not description:
        return None, "Joy tavsifini kiriting (qanday ko'rinishda bo'lishi kerak)"
    if lid:
        old = location(lid)
        keep = old and (old["description"] or "").strip() == description
        db.ex("UPDATE img_locations SET name=?, description=?" + ("" if keep else ", prompt_en=NULL") + " WHERE id=?", (name, description, lid))
        return lid, None
    nid = db.ex("INSERT INTO img_locations(brand_id,name,description,created_at) VALUES(?,?,?,?)", (bid, name, description, now()))
    return nid, None


def delete_location(lid):
    lo = location(lid)
    if not lo:
        return
    db.ex("UPDATE img_plocs SET location_id=NULL WHERE location_id=?", (lid,))
    db.ex("DELETE FROM img_locations WHERE id=?", (lid,))


# ---------------------------------------------------------------- platformalar
def platforms(active_only=False, kind=None):
    sql, args = "SELECT * FROM img_platforms WHERE 1=1", []
    if active_only:
        sql += " AND active=1"
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return rows(sql + " ORDER BY kind DESC, sort, id", tuple(args))


def save_platform(name: str, kind: str, max_sec, rules: str, audio: bool, pid=None) -> str | None:
    name, rules = (name or "").strip(), (rules or "").strip()
    kind = kind if kind in ("image", "video") else "video"
    if not name:
        return "Platforma nomini kiriting"
    if not rules:
        return "Prompt yozish qoidalarini kiriting (AI shunga qarab yozadi)"
    try:
        ms = max(0.0, min(float(max_sec), 60.0))
    except (TypeError, ValueError):
        ms = 5.0
    if kind == "video":
        ms = max(1.0, ms)
    dup = db.one("SELECT id FROM img_platforms WHERE name=?", (name,))
    if dup and dup["id"] != pid:
        return "Bunday nomli platforma allaqachon bor"
    if pid:
        db.ex("UPDATE img_platforms SET name=?,kind=?,max_sec=?,rules=?,audio=? WHERE id=?", (name, kind, ms, rules, 1 if audio else 0, pid))
    else:
        nxt = (db.one("SELECT COALESCE(MAX(sort),0)+1 n FROM img_platforms")["n"])
        db.ex("INSERT INTO img_platforms(name,kind,active,max_sec,rules,audio,sort,builtin) VALUES(?,?,1,?,?,?,?,0)", (name, kind, ms, rules, 1 if audio else 0, nxt))
    return None


# ---------------------------------------------------------------- loyihalar
def projects(limit=100):
    out = rows("SELECT p.*, b.name AS brand_name FROM img_projects p LEFT JOIN img_brands b ON b.id=p.brand_id ORDER BY p.id DESC LIMIT ?", (limit,))
    for p in out:
        p["n_scenes"] = db.one("SELECT COUNT(*) c FROM img_scenes WHERE project_id=?", (p["id"],))["c"]
        p["n_done"] = db.one("SELECT COUNT(*) c FROM img_scenes WHERE project_id=? AND status='done'", (p["id"],))["c"]
    return out


def project(pid):
    return row(db.one("SELECT p.*, b.name AS brand_name FROM img_projects p LEFT JOIN img_brands b ON b.id=p.brand_id WHERE p.id=?", (pid,)))


def set_project(pid, **kw):
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    db.ex(f"UPDATE img_projects SET {cols}, ver=ver+1 WHERE id=?", (*kw.values(), pid))


def create_project(bid, title, script, fmt, style, topic="", image_platform="", video_platforms=None) -> int:
    fmt = fmt if fmt in FORMATS else "9:16"
    style = style if style in STYLES else "real"
    return db.ex("INSERT INTO img_projects(brand_id,title,script,topic,fmt,quality,style,image_platform,video_platforms,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,'new',?)",
                 (bid, (title or "").strip()[:250] or "Nomsiz loyiha", script, topic, fmt, "", style, image_platform or "", jd(list(video_platforms or [])), now()))


def project_platforms(p: dict) -> tuple[dict | None, list[dict]]:
    """Loyiha tanlagan rasm platformasi va video platformalar (hozirgi Sozlamalar bo'yicha; o'chirilgani tushib qoladi)."""
    img = next((x for x in platforms(kind="image") if x["name"] == p.get("image_platform")), None)
    names = jl(p.get("video_platforms"), [])
    vids = [x for x in platforms(kind="video") if x["name"] in names]
    return img, vids


def delete_project(pid):
    for s in rows("SELECT id FROM img_scenes WHERE project_id=?", (pid,)):
        db.ex("DELETE FROM img_prompts WHERE scene_id=?", (s["id"],))
        db.ex("DELETE FROM img_versions WHERE scene_id=?", (s["id"],))
    for t in ("img_scenes", "img_roles", "img_plocs", "img_jobs", "img_costs"):
        db.ex(f"DELETE FROM {t} WHERE project_id=?", (pid,))
    db.ex("DELETE FROM img_projects WHERE id=?", (pid,))
    rm_tree("projects", str(pid))


def roles(pid):
    out = rows("SELECT * FROM img_roles WHERE project_id=? ORDER BY id", (pid,))
    for r in out:
        r["person"] = person(r["person_id"]) if r["person_id"] else None
    return out


def plocs(pid):
    out = rows("SELECT * FROM img_plocs WHERE project_id=? ORDER BY id", (pid,))
    for pl in out:
        pl["loc"] = location(pl["location_id"]) if pl["location_id"] else None
    return out


def scenes(pid, full=True):
    out = rows("SELECT * FROM img_scenes WHERE project_id=? ORDER BY idx", (pid,))
    for s in out:
        s["roles"] = jl(s["roles"], [])
        if full:
            fill_scene(s)
    return out


def scene(sid, full=True):
    s = row(db.one("SELECT * FROM img_scenes WHERE id=?", (sid,)))
    if s:
        s["roles"] = jl(s["roles"], [])
        if full:
            fill_scene(s)
    return s


def fill_scene(s: dict):
    s["versions"] = rows("SELECT * FROM img_versions WHERE scene_id=? ORDER BY n DESC", (s["id"],))
    act = next((v for v in s["versions"] if v["id"] == s["active_ver"]), None)
    s["active"] = act
    s["video"], s["audio"] = {}, ""
    if act:
        for p in rows("SELECT * FROM img_prompts WHERE scene_id=? AND version_id=? ORDER BY id", (s["id"], act["id"])):
            if p["kind"] == "video":
                s["video"][p["platform"]] = p["text"]
            elif p["kind"] == "audio":
                s["audio"] = p["text"]
    s["status_uz"] = SCENE_UZ.get(s["status"], s["status"])


def set_scene(sid, **kw):
    kw["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in kw)
    db.ex(f"UPDATE img_scenes SET {cols} WHERE id=?", (*kw.values(), sid))


def add_version(sid, prompt, note="", cost=0.0, model="") -> int:
    """Sahnaning yangi prompt versiyasi (rasm prompti; unga mos video/audio promptlar img_prompts da version_id bilan)."""
    n = db.one("SELECT COALESCE(MAX(n),0)+1 n FROM img_versions WHERE scene_id=?", (sid,))["n"]
    return db.ex("INSERT INTO img_versions(scene_id,n,image_prompt,file,note,qa_ok,qa_json,cost,model,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (sid, n, prompt, "", note, None, None, cost, model, now()))


def set_prompts(sid, vid, video: dict, audio: str):
    db.ex("DELETE FROM img_prompts WHERE scene_id=? AND version_id=?", (sid, vid))
    for plat, text in video.items():
        db.ex("INSERT INTO img_prompts(scene_id,version_id,kind,platform,text,created_at) VALUES(?,?,'video',?,?,?)", (sid, vid, plat, text, now()))
    if audio:
        db.ex("INSERT INTO img_prompts(scene_id,version_id,kind,platform,text,created_at) VALUES(?,?,'audio','',?,?)", (sid, vid, audio, now()))


# ---------------------------------------------------------------- navbat
def enqueue(kind, project_id=None, scene_id=None, payload=None) -> int:
    return db.ex("INSERT INTO img_jobs(project_id,kind,scene_id,payload,status,created_at) VALUES(?,?,?,?,'queued',?)",
                 (project_id, kind, scene_id, jd(payload or {}), now()))


def job_active(kind=None, project_id=None, scene_id=None) -> bool:
    sql, args = "SELECT 1 FROM img_jobs WHERE status IN ('queued','running')", []
    for col, v in (("kind", kind), ("project_id", project_id), ("scene_id", scene_id)):
        if v is not None:
            sql += f" AND {col}=?"
            args.append(v)
    return bool(db.one(sql + " LIMIT 1", tuple(args)))


def busy_count() -> int:
    return db.one("SELECT COUNT(*) c FROM img_jobs WHERE status IN ('queued','running')")["c"]
