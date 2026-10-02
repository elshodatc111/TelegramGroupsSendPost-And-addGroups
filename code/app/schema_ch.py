"""Kanallarim bo'limi jadvallari (ch_*). Group Post jadvallaridan butunlay alohida.

DDL MySQL sintaksisida yozilgan; SQLite (eski rejim) uchun avtomatik moslashtiriladi.
"""
import json
import re

T = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"

TABLES = [
    f"""CREATE TABLE IF NOT EXISTS ch_types(
        `key` VARCHAR(40) PRIMARY KEY, title VARCHAR(120), description TEXT, rubrics_json LONGTEXT, tone VARCHAR(255),
        freq VARCHAR(120), hints TEXT, builtin INT DEFAULT 0) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_channels(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, tg_id BIGINT NOT NULL, title VARCHAR(600),
        username VARCHAR(120), about TEXT, members INT, status VARCHAR(16) DEFAULT 'active', type_key VARCHAR(40),
        is_creator INT DEFAULT 0, rights_json TEXT, created_at VARCHAR(32), archived_at VARCHAR(32), synced_at VARCHAR(32),
        purpose TEXT, audience TEXT, lang VARCHAR(16) DEFAULT 'uz', tone TEXT, rubrics_json LONGTEXT, post_freq VARCHAR(120),
        cta TEXT, bans TEXT, biz_json LONGTEXT, lead_url VARCHAR(600), lead_text TEXT, video_format TEXT, samples LONGTEXT,
        brand_name VARCHAR(255), phones TEXT, address TEXT, landmark TEXT, contacts TEXT, color1 VARCHAR(9), color2 VARCHAR(9),
        color3 VARCHAR(9), logo VARCHAR(255), img_style TEXT, track_on INT DEFAULT 1,
        UNIQUE KEY u_chch(account_id, tg_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_competitors(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT NOT NULL, tg_id BIGINT NOT NULL, title VARCHAR(600),
        username VARCHAR(120), about TEXT, members INT, is_private INT DEFAULT 0, active INT DEFAULT 1,
        added_at VARCHAR(32), last_sync VARCHAR(32), notes TEXT, error VARCHAR(255),
        UNIQUE KEY u_comp(channel_id, tg_id), INDEX idx_comp_ch(channel_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_posts(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT NOT NULL, comp_id INT NOT NULL DEFAULT 0, msg_id BIGINT NOT NULL,
        date VARCHAR(32), text LONGTEXT, media VARCHAR(16), duration INT DEFAULT 0, views INT DEFAULT 0, forwards INT DEFAULT 0,
        reactions INT DEFAULT 0, replies INT DEFAULT 0, grouped_id BIGINT, has_link INT DEFAULT 0, fetched_at VARCHAR(32),
        upd_at VARCHAR(32), transcript LONGTEXT, tr_lang VARCHAR(8), tr_at VARCHAR(32),
        UNIQUE KEY u_post(channel_id, comp_id, msg_id), INDEX idx_post_date(channel_id, comp_id, date)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_members_log(
        channel_id INT NOT NULL, comp_id INT NOT NULL DEFAULT 0, day VARCHAR(10) NOT NULL, members INT,
        PRIMARY KEY(channel_id, comp_id, day)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_plan(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT NOT NULL, title VARCHAR(255), text LONGTEXT,
        media_json LONGTEXT, media_type VARCHAR(16), parse_mode VARCHAR(10) DEFAULT 'none', scheduled_at VARCHAR(32),
        status VARCHAR(16) DEFAULT 'draft', msg_id BIGINT, sent_at VARCHAR(32), error TEXT, idea_id INT,
        created_at VARCHAR(32), track_code VARCHAR(16), ab_id INT, ab_variant VARCHAR(2), image_prompt TEXT,
        INDEX idx_plan(channel_id, status, scheduled_at)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_ideas(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT NOT NULL, created_at VARCHAR(32), kind VARCHAR(16) DEFAULT 'manual',
        lang VARCHAR(8), want_video INT DEFAULT 0, user_note TEXT, title VARCHAR(400), body_json LONGTEXT,
        status VARCHAR(16) DEFAULT 'new', feedback TEXT, model VARCHAR(80), tokens_in INT DEFAULT 0, tokens_out INT DEFAULT 0,
        cost DOUBLE DEFAULT 0, plan_id INT, INDEX idx_ideas(channel_id, created_at)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_memory(
        channel_id INT PRIMARY KEY, memo LONGTEXT, updated_at VARCHAR(32), version INT DEFAULT 0, seen_ts VARCHAR(32),
        video_summary LONGTEXT, video_at VARCHAR(32)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_usage(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT NOT NULL, ts VARCHAR(32), day VARCHAR(10), purpose VARCHAR(40),
        model VARCHAR(80), tokens_in INT DEFAULT 0, tokens_out INT DEFAULT 0, cached_in INT DEFAULT 0,
        audio_sec DOUBLE DEFAULT 0, cost DOUBLE DEFAULT 0, priced INT DEFAULT 1, ok INT DEFAULT 1, note VARCHAR(255),
        ch_title VARCHAR(255),
        INDEX idx_usage(channel_id, day)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_prices(
        model VARCHAR(80) PRIMARY KEY, in_per_m DOUBLE DEFAULT 0, out_per_m DOUBLE DEFAULT 0, cached_per_m DOUBLE DEFAULT 0,
        audio_per_min DOUBLE DEFAULT 0) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_log(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT, ts VARCHAR(32), kind VARCHAR(20), info TEXT,
        INDEX idx_chlog(channel_id, ts)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_links(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT NOT NULL, code VARCHAR(24) NOT NULL, plan_id INT, idea_id INT,
        ab_id INT, variant VARCHAR(2), label VARCHAR(255), created_at VARCHAR(32),
        UNIQUE KEY u_link(channel_id, code), INDEX idx_link_plan(plan_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_clicks(
        channel_id INT NOT NULL, code VARCHAR(24) NOT NULL, day VARCHAR(10) NOT NULL, n INT DEFAULT 0,
        PRIMARY KEY(channel_id, code, day)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_daily(
        channel_id INT NOT NULL, day VARCHAR(10) NOT NULL, members INT, joined INT, left_n INT, views_total INT DEFAULT 0,
        posts INT DEFAULT 0, views_avg INT DEFAULT 0, notif_pct DOUBLE, PRIMARY KEY(channel_id, day)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_official(
        channel_id INT NOT NULL, kind VARCHAR(40) NOT NULL, data LONGTEXT, updated_at VARCHAR(32),
        PRIMARY KEY(channel_id, kind)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_audience(
        channel_id INT NOT NULL, day VARCHAR(10) NOT NULL, total INT, sampled INT, premium INT, bots INT, deleted INT,
        a_online INT, a_recent INT, a_week INT, a_month INT, a_long INT, PRIMARY KEY(channel_id, day)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_alerts(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT NOT NULL, comp_id INT NOT NULL DEFAULT 0, msg_id BIGINT NOT NULL,
        ts VARCHAR(32), kind VARCHAR(16) DEFAULT 'viral', title VARCHAR(400), info TEXT, ratio DOUBLE, views INT,
        seen INT DEFAULT 0, notified INT DEFAULT 0, analysis LONGTEXT,
        UNIQUE KEY u_alert(channel_id, comp_id, msg_id, kind), INDEX idx_alert(channel_id, seen)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_ab(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT NOT NULL, title VARCHAR(400), idea_id INT, hypothesis TEXT,
        status VARCHAR(16) DEFAULT 'draft', winner VARCHAR(2), result_json LONGTEXT, created_at VARCHAR(32),
        INDEX idx_ab(channel_id, created_at)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_comments(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT NOT NULL, post_msg_id BIGINT, cmsg_id BIGINT NOT NULL,
        date VARCHAR(32), text TEXT, UNIQUE KEY u_cm(channel_id, cmsg_id), INDEX idx_cm(channel_id, date)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_month(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT NOT NULL, month VARCHAR(7), created_at VARCHAR(32),
        body_json LONGTEXT, model VARCHAR(80), cost DOUBLE DEFAULT 0, INDEX idx_month(channel_id, month)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_reports(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT NOT NULL, kind VARCHAR(16), period VARCHAR(40), path VARCHAR(600),
        created_at VARCHAR(32), INDEX idx_rep(channel_id, created_at)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ch_images(
        id INT AUTO_INCREMENT PRIMARY KEY, channel_id INT NOT NULL, plan_id INT, idea_id INT, prompt TEXT, name VARCHAR(255),
        size VARCHAR(16), engine VARCHAR(40), created_at VARCHAR(32), INDEX idx_img(channel_id, created_at)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS sys_events(
        id INT AUTO_INCREMENT PRIMARY KEY, ts VARCHAR(32), kind VARCHAR(16), source VARCHAR(60), info TEXT,
        INDEX idx_sysev(ts)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS sys_beat(
        name VARCHAR(60) PRIMARY KEY, ts VARCHAR(32), state VARCHAR(16), info TEXT, restarts INT DEFAULT 0) {T}""",
]


from . import schema_ig  # noqa: E402  (Instagram jadvallari shu ro'yxatga qo'shiladi)
TABLES += schema_ig.TABLES


def _to_sqlite(stmt: str):
    """MySQL DDL -> SQLite DDL. (CREATE TABLE, [CREATE INDEX ...]) qaytaradi."""
    s = stmt.replace(T, "")
    s = re.sub(r"`", "", s)
    idx = []
    tbl = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", s).group(1)

    def grab(m):
        idx.append(f"CREATE INDEX IF NOT EXISTS {m.group(1)} ON {tbl}({m.group(2)})")
        return ""
    s = re.sub(r",\s*INDEX\s+(\w+)\(([^)]*)\)", grab, s)
    s = re.sub(r"UNIQUE KEY\s+\w+\(([^)]*)\)", r"UNIQUE(\1)", s)
    s = re.sub(r"INT AUTO_INCREMENT PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT", s)
    s = re.sub(r"\bBIGINT\b", "INTEGER", s)
    s = re.sub(r"\bINT\b", "INTEGER", s)
    s = re.sub(r"VARCHAR\(\d+\)", "TEXT", s)
    s = re.sub(r"\bLONGTEXT\b", "TEXT", s)
    s = re.sub(r"\bDOUBLE\b", "REAL", s)
    s = re.sub(r"DEFAULT \('(.*?)'\)", r"DEFAULT '\1'", s)
    return s, idx


def ddl(backend: str) -> list[str]:
    if backend == "mysql":
        return list(TABLES)
    out = []
    for t in TABLES:
        create, idx = _to_sqlite(t)
        out.append(create)
        out += idx
    return out


EXTRA = [("ch_channels", c, d) for c, d in (
    ("brand_name", "VARCHAR(255)"), ("phones", "TEXT"), ("address", "TEXT"), ("landmark", "TEXT"), ("contacts", "TEXT"),
    ("color1", "VARCHAR(9)"), ("color2", "VARCHAR(9)"), ("color3", "VARCHAR(9)"), ("logo", "VARCHAR(255)"),
    ("img_style", "TEXT"), ("track_on", "INT DEFAULT 1"))] + [("ch_plan", c, d) for c, d in (
    ("track_code", "VARCHAR(16)"), ("ab_id", "INT"), ("ab_variant", "VARCHAR(2)"), ("image_prompt", "TEXT"))]

TABLE_NAMES = ["ch_types", "ch_channels", "ch_competitors", "ch_posts", "ch_members_log", "ch_plan", "ch_ideas",
               "ch_memory", "ch_usage", "ch_prices", "ch_log", "ch_links", "ch_clicks", "ch_daily", "ch_official", "ch_audience",
               "ch_alerts", "ch_ab", "ch_comments", "ch_month", "ch_reports", "ch_images", "sys_events", "sys_beat"] + schema_ig.TABLE_NAMES

# ---------------------------------------------------------------- kanal turlari (presetlar)
TYPES = [
    ("it", "IT / dasturlash", "Dasturlash, IT kurslari, texnologiya yangiliklari va karyera maslahatlari.",
     ["Foydali maslahat (kod/vosita)", "Yangilik va tahlil", "Karyera va ish o'rinlari", "Mini-dars / yo'riqnoma", "Talaba natijasi (keys)", "Savol-javob / so'rovnoma"],
     "Do'stona, aniq, texnik so'zlar tushunarli izoh bilan", "Kuniga 1-2 post", "Kod namunasi, qisqa video (30-60s), skrinshot."),
    ("edu_center", "O'quv markaz", "O'quv markaz yoki kurslar: o'quvchi jalb qilish, natijalar, yangi guruhlar.",
     ["Yangi guruh e'loni", "O'quvchi natijasi / fikri", "O'qituvchi tanishtiruvi", "Foydali maslahat", "Aksiya va chegirma", "Dars jarayonidan lavha"],
     "Ishonchli, samimiy, ota-onalar va yoshlarga mos", "Kuniga 1-2 post", "Dars jarayonidan reels, o'quvchi intervyusi, natijalar."),
    ("korean", "Koreys tili", "Koreys tili o'rgatish, TOPIK/EPS-TOPIK, Koreyada ish va o'qish.",
     ["Kunlik so'z / ibora", "Grammatika mini-dars", "TOPIK/EPS-TOPIK test", "Koreyada ish/viza yangiligi", "Koreya madaniyati", "O'quvchi yutug'i"],
     "Yengil, motivatsion, sodda tushuntirish", "Kuniga 1-3 post", "So'z-rasm reels, talaffuz videosi, kichik test."),
    ("local", "Mahalla / hudud yangiliklari", "Hudud (shahar, tuman, mahalla) yangiliklari, e'lonlar va xizmatlar.",
     ["Hudud yangiligi", "Foydali ma'lumot (xizmat, manzil)", "E'lon va takliflar", "Tadbirlar", "Aholi savollari"],
     "Xolis, aniq, mahalliy til va atamalar", "Kuniga 2-4 post", "Foto va qisqa reportaj videolari."),
    ("business", "Biznes", "Biznes, tadbirkorlik, marketing va sotuv bo'yicha kanal.",
     ["Sotuv maslahati", "Keys / muvaffaqiyat hikoyasi", "Bozor tahlili", "Xatolar va saboqlar", "Taklif / aksiya", "Savol-javob"],
     "Ishonchli, natijaga yo'naltirilgan, raqamlar bilan", "Kuniga 1-2 post", "Ekspert videosi, infografika, karusel."),
    ("school_uni", "Universitet / maktab", "Ta'lim muassasasi: qabul, yangiliklar, talabalar hayoti.",
     ["Qabul va imtiyozlar", "Talabalar hayoti", "Yutuqlar va tadbirlar", "Foydali maslahatlar", "E'lonlar"],
     "Rasmiy lekin iliq, yoshlarga yaqin", "Kuniga 1-2 post", "Tadbir lavhalari, talaba intervyulari."),
    ("ads", "E'lonlar / ish o'rinlari", "Vakansiya, e'lon, xizmat va mahsulot takliflari.",
     ["Yangi vakansiya", "Xizmat taklifi", "Mahsulot e'loni", "Foydali maslahat (ishga kirish)", "Aksiya"],
     "Qisqa, aniq, foyda birinchi o'rinda", "Kuniga 3-6 post", "Foto karta, qisqa video."),
    ("ecommerce", "Onlayn do'kon / savdo", "Mahsulot sotish: katalog, aksiya, mijoz fikrlari.",
     ["Mahsulot taqdimoti", "Aksiya / chegirma", "Mijoz fikri", "Qanday foydalanish", "Yangi kelgan tovar", "Savol-javob"],
     "Jonli, ishontiruvchi, aniq narx va foyda", "Kuniga 1-3 post", "Unboxing, demo video, before/after."),
    ("expert", "Shaxsiy brend / ekspert", "Ekspert yoki blogerning shaxsiy kanali.",
     ["Shaxsiy hikoya", "Ekspert maslahati", "Xatolar va saboqlar", "Kulisdagi lavhalar", "Mijoz natijasi", "Savol-javob"],
     "Samimiy, birinchi shaxsda, ishonch uyg'otuvchi", "Kuniga 1 post", "Yuz ko'rinadigan video, ovozli xabar."),
    ("entertainment", "Ko'ngilochar", "Qiziqarli kontent, hazil, test va o'yinlar.",
     ["Hazil / meme", "Qiziqarli fakt", "Test / so'rovnoma", "Trend video", "Kun savoli"],
     "Yengil, jonli, ijodkor", "Kuniga 2-5 post", "Qisqa trend videolar."),
]


def seed():
    """Kanal turlari presetlarini qo'shadi (mavjudini o'zgartirmaydi)."""
    from . import db
    for key, title, desc, rubrics, tone, freq, hints in TYPES:
        db.ex("INSERT OR IGNORE INTO ch_types(key,title,description,rubrics_json,tone,freq,hints,builtin) VALUES(?,?,?,?,?,?,?,1)",
              (key, title, desc, json.dumps(rubrics, ensure_ascii=False), tone, freq, hints))
    # taxminiy narxlar (USD / 1M token; audio: USD / daqiqa). Sozlamalar sahifasida yangilash mumkin.
    for model, i, o, c, a in PRICES:
        db.ex("INSERT OR IGNORE INTO ch_prices(model,in_per_m,out_per_m,cached_per_m,audio_per_min) VALUES(?,?,?,?,?)",
              (model, i, o, c, a))


# model: (kirish, chiqish, keshlangan kirish, audio daqiqa) — OpenAI'ning e'lon qilingan narxlariga yaqin boshlang'ich qiymatlar
PRICES = [
    ("gpt-5", 1.25, 10.0, 0.125, 0), ("gpt-5-mini", 0.25, 2.0, 0.025, 0), ("gpt-5-nano", 0.05, 0.4, 0.005, 0),
    ("gpt-4.1", 2.0, 8.0, 0.5, 0), ("gpt-4.1-mini", 0.4, 1.6, 0.1, 0), ("gpt-4.1-nano", 0.1, 0.4, 0.025, 0),
    ("gpt-4o", 2.5, 10.0, 1.25, 0), ("gpt-4o-mini", 0.15, 0.6, 0.075, 0),
    ("whisper-1", 0, 0, 0, 0.006), ("gpt-4o-transcribe", 0, 0, 0, 0.006), ("gpt-4o-mini-transcribe", 0, 0, 0, 0.003),
]
