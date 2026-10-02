"""Tizim tahlili: loyihaga kerak bo'lgan har bir qismning holati, xatolar manbai va tuzatish yo'li.

Hammasi lokal: hech narsa tashqariga yuborilmaydi (AI xulosasi so'ralmaguncha). Avto-tuzatish faqat xavfsiz amallar (MySQL'ni ishga tushirish).
"""
import asyncio
import json
import re
import shutil
import socket
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from . import ai, ch_data, ch_track, db, notify
from .config import BACKUP_DIR, DATA_DIR, LOG_DIR, log

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
TASKS: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------- fon jarayonlarini kuzatish
def supervise(name: str, factory, restart_after: int = 60):
    """Fon siklini ishga tushiradi; yiqilsa sababini yozadi va qayta ishga tushiradi."""
    async def runner():
        restarts = 0
        while True:
            db.ex("INSERT OR REPLACE INTO sys_beat(name,ts,state,info,restarts) VALUES(?,?,?,?,?)", (name, db.now(), "running", "", restarts))
            try:
                await factory()
                db.ex("INSERT OR REPLACE INTO sys_beat(name,ts,state,info,restarts) VALUES(?,?,?,?,?)", (name, db.now(), "stopped", "Sikl o'zi tugadi", restarts))
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                restarts += 1
                log.exception("Fon jarayoni yiqildi: %s", name)
                info = f"{type(e).__name__}: {e}"[:400]
                db.ex("INSERT OR REPLACE INTO sys_beat(name,ts,state,info,restarts) VALUES(?,?,?,?,?)", (name, db.now(), "crashed", info, restarts))
                notify.event("error", name, "Fon jarayoni yiqildi va qayta ishga tushiriladi: " + info)
                await asyncio.sleep(restart_after)
    t = asyncio.create_task(runner())
    TASKS[name] = t
    return t


ONESHOT = {"manager"}          # bir marta bajarilib tugaydigan vazifalar (xato emas)
LOOP_TITLES = {
    "manager": "Akkauntlarni ulash", "auto_refresh": "Guruhlarni avto-yangilash", "leaver": "Guruhdan chiqish", "dailyreport": "Kunlik hisobot (Telegram Guruhlar)",
    "discovery": "Guruh qidirish", "auditor": "Guruh auditi", "ch_collect": "Kanal ma'lumotlarini yig'ish", "ch_plan": "Kontent reja yuboruvchi",
    "ch_learn": "Bilim xotirasi", "backup": "Baza zaxirasi", "ch_track": "Havola bosilishlarini olish", "ch_insight": "Statistika va viral kuzatuvi",
    "ch_report": "Haftalik hisobot", "ig_collect": "Instagram: ma'lumot yig'ish", "ig_plan": "Instagram: reja (eslatma/joylash)", "cf_watch": "Cloudflare R2: hajm nazorati",
    "meet_sched": "Zoom Online: darslar sikli", "meet_bot": "Zoom Online: o'qituvchilar boti", "img_work": "Image Generator: rasm yaratish navbati",
}


# ---------------------------------------------------------------- yordamchi
def item(group, title, state, detail="", fix="", key=None):
    return {"group": group, "title": title, "state": state, "detail": detail, "fix": fix, "key": key or title}


def _run(cmd, timeout=6):
    try:
        flags = 0x08000000 if sys.platform == "win32" else 0
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def _size(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def _mod(text: str) -> str:
    t = text.lower()
    for words, name in ((("mysql", "pymysql", "aria", "operationalerror", "xampp"), "Baza (MySQL)"), (("openai", "ai.chat", "insufficient_quota", "rate limit"), "OpenAI"),
                        (("instagram", "ig_api", "ig_collect", "ig_plan", "igerror", "graph.instagram"), "Instagram"), (("floodwait", "telethon", "rpcerror", "session", "telegram"), "Telegram"), (("ffmpeg", "whisper", "transcri"), "Video → matn"),
                        (("diffusers", "cuda", "torch", "imagegen", "rasm"), "Rasm yaratish"), (("worker", "cloudflare", "track"), "Kuzatuv havolasi"),
                        (("reportlab", "openpyxl", "hisobot"), "Hisobot"), (("ch_plan", "send_item", "reja"), "Kontent reja"), (("sender", "joiner", "campaign"), "Telegram Guruhlar")):
        if any(w in t for w in words):
            return name
    return "Boshqa"


# ---------------------------------------------------------------- tekshiruvlar
def check_database():
    g = "Baza"
    out = []
    if not db.IS_MYSQL:
        return [item(g, "Baza turi", WARN, "SQLite rejimi (eski).", "run.bat orqali MySQL (XAMPP) bilan ishga tushiring.")]
    try:
        from . import mysqldb
        conf = mysqldb.pool.conf or {}
        v = db.one("SELECT VERSION() v")["v"]
        sz = db.one("SELECT COALESCE(SUM(data_length+index_length),0) s, COUNT(*) n FROM information_schema.tables WHERE table_schema=DATABASE()")
        out.append(item(g, "MySQL / MariaDB", OK, f"Ishlayapti: {v}, baza {_size(float(sz['s']))}, {sz['n']} ta jadval", key="mysql"))
        from .xampp import port_open
        if not port_open(conf.get("host", "127.0.0.1"), int(conf.get("port", 3306))):
            out.append(item(g, "MySQL porti", FAIL, "Port yopiq.", "XAMPP Control Panel'da MySQL ni Start qiling yoki run.bat ni qayta ishga tushiring.", key="mysql_port"))
    except Exception as e:
        out.append(item(g, "MySQL / MariaDB", FAIL, f"{type(e).__name__}: {e}",
                        "XAMPP Control Panel'da MySQL ishlayotganini tekshiring. «Aria/crashed» xatosi bo'lsa yo'riqnomadagi tiklash bosqichlarini bajaring.", key="mysql"))
        return out
    files = sorted(BACKUP_DIR.glob("tgp-*.sql.gz"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        out.append(item(g, "Zaxira nusxa", WARN, "Hali zaxira olinmagan.", "Dastur 10 daqiqa ishlagach avtomatik oladi. Kompyuter tez-tez o'chsa, run.bat ni uzoqroq ochiq qoldiring.", key="backup"))
    else:
        age = (datetime.now() - datetime.fromtimestamp(files[0].stat().st_mtime)).total_seconds() / 3600
        out.append(item(g, "Zaxira nusxa", OK if age < 36 else WARN, f"Oxirgisi {age:.0f} soat oldin, jami {len(files)} ta ({_size(files[0].stat().st_size)})",
                        "" if age < 36 else "Zaxira eskirgan: dastur har kuni olishi kerak. Loglarda «Zaxira olinmadi» qatoriga qarang.", key="backup"))
    du = shutil.disk_usage(DATA_DIR)
    free = du.free / 1e9
    out.append(item(g, "Disk (data papkasi)", OK if free > 5 else WARN if free > 1 else FAIL, f"{free:.1f} GB bo'sh",
                    "" if free > 5 else "Joy kam: eski media/backup fayllarni o'chiring yoki diskni tozalang.", key="disk"))
    return out


def check_telegram():
    g = "Telegram"
    out = []
    has_api = bool(db.get_setting("api_id") and db.get_setting("api_hash"))
    out.append(item(g, "API ID / API HASH", OK if has_api else FAIL, "Kiritilgan" if has_api else "Kiritilmagan",
                    "" if has_api else "my.telegram.org → API development tools dan oling va Sozlamalarga kiriting.", key="api"))
    try:
        s = socket.create_connection(("api.telegram.org", 443), timeout=4)
        s.close()
        out.append(item(g, "Telegram'ga internet", OK, "api.telegram.org ochiq", key="tg_net"))
    except OSError as e:
        out.append(item(g, "Telegram'ga internet", FAIL, f"Ulanib bo'lmadi: {e}", "Internet, VPN yoki antivirus/firewall'ni tekshiring.", key="tg_net"))
    from .core import manager
    for ws, label in (("posting", "Telegram Guruhlar"), ("channels", "Telegram SMM")):
        rows = db.q("SELECT * FROM accounts WHERE workspace=?", (ws,))
        if not rows:
            out.append(item(g, f"Akkaunt — {label}", INFO, "Akkaunt qo'shilmagan", "Bo'limning «Akkaunt» sahifasidan ulang." if ws == "channels" else "Akkauntlar sahifasidan ulang.", key=f"acc_{ws}"))
            continue
        for r in rows:
            svc = manager.services.get(r["id"])
            ok = bool(svc and svc.info)
            out.append(item(g, f"Akkaunt — {label}: {r['name']}", OK if ok else FAIL,
                            f"+{svc.info['phone']}" if ok else "Ulanmagan", "" if ok else "Akkaunt sahifasida «Qayta ulash» ni bosing (kod keladi).", key=f"acc_{r['id']}"))
    flood = db.q("SELECT info, ts FROM sys_events WHERE kind IN ('warn','error') AND info LIKE '%FloodWait%' ORDER BY id DESC LIMIT 1")
    if flood:
        out.append(item(g, "Telegram cheklovi (FloodWait)", WARN, f"{flood[0]['ts'][:16]}: {flood[0]['info'][:160]}", "Bir necha soat kuting; yuborish tezligini kamaytiring.", key="flood"))
    return out


def check_openai():
    g = "OpenAI"
    if not ai.configured():
        return [item(g, "OpenAI kaliti", WARN, "Kiritilmagan", "Telegram SMM → Sozlamalar → OpenAI kalitini kiriting (g'oya, tahlil, rasm prompti uchun).", key="openai")]
    out = [item(g, "OpenAI kaliti", OK, "Kiritilgan", key="openai")]
    bad = db.q("SELECT info, ts FROM sys_events WHERE source IN ('ai','openai') AND kind='error' ORDER BY id DESC LIMIT 1")
    month = datetime.now().strftime("%Y-%m")
    c = db.one("SELECT COUNT(*) n, COALESCE(SUM(cost),0) c FROM ch_usage WHERE day LIKE ?", (month + "%",))
    out.append(item(g, "Shu oy AI sarfi", INFO, f"{c['n']} ta so'rov, ${float(c['c']):.4f}", key="ai_use"))
    if bad:
        out.append(item(g, "Oxirgi OpenAI xatosi", WARN, f"{bad[0]['ts'][:16]}: {bad[0]['info'][:200]}", "Balans, limit va kalit to'g'riligini platform.openai.com da tekshiring.", key="ai_err"))
    return out


def check_media_tools():
    g = "Video / rasm vositalari"
    out = []
    from . import transcribe
    st = transcribe.local_status()
    out.append(item(g, "ffmpeg", OK if st["ffmpeg"] else WARN, "Topildi" if st["ffmpeg"] else "Topilmadi",
                    "" if st["ffmpeg"] else "Video → matn va videodan audio olish uchun kerak: tools\\install_whisper.bat yoki winget install Gyan.FFmpeg", key="ffmpeg"))
    out.append(item(g, "faster-whisper (lokal transkripsiya)", OK if st["fw"] else WARN, "O'rnatilgan" if st["fw"] else "Yo'q",
                    "" if st["fw"] else "tools\\install_whisper.bat ni ishga tushiring (yoki OpenAI transkripsiyasi ishlatiladi).", key="whisper"))
    rc, txt = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader"])
    if rc == 0 and txt.strip():
        out.append(item(g, "Video karta (NVIDIA)", OK, txt.strip().splitlines()[0], key="gpu"))
    else:
        out.append(item(g, "Video karta (NVIDIA)", WARN, "Drayver yoki karta topilmadi (nvidia-smi ishlamadi)",
                        "NVIDIA drayverini o'rnating/yangilang (nvidia.com/drivers). Karta bo'lmasa ham dastur ishlaydi: Whisper CPU'da, rasm gradient fon bilan.", key="gpu"))
    from . import imagegen
    im = imagegen.status()
    pv = sys.version_info
    out.append(item(g, "Python versiyasi", OK if pv[:2] <= (3, 12) else INFO, f"{pv[0]}.{pv[1]}.{pv[2]}",
                    "" if pv[:2] <= (3, 12) else "Juda yangi Python uchun PyTorch ba'zan topilmaydi. tools\\install_imagegen.bat Python 3.12 bilan yangi muhitni o'zi yaratadi.", key="pyver"))
    out.append(item(g, "Pillow (brend qatlami)", OK if im["pillow"] else FAIL, "Bor" if im["pillow"] else "Yo'q", "" if im["pillow"] else "pip install Pillow (run.bat buni o'zi qiladi).", key="pillow"))
    if im["ready"]:
        out.append(item(g, "Lokal rasm yaratish", OK, f"{im['gpu']} ({im['vram_gb']} GB), model {im['model']}", key="imagegen"))
    else:
        why = "PyTorch yo'q" if not im["torch"] else "CUDA topilmadi (PyTorch CPU versiyasi yoki drayver muammosi)" if not im["cuda"] else "diffusers yo'q"
        out.append(item(g, "Lokal rasm yaratish", WARN, why + f" (Python {pv[0]}.{pv[1]})", "tools\\install_imagegen.bat ni ishga tushiring (kerak bo'lsa Python 3.12 ni o'zi o'rnatadi). Shu vaqtgacha «Gradient fon» ishlaydi.", key="imagegen"))
    for mod, label, fix in (("reportlab", "PDF hisobot (reportlab)", "pip install reportlab"), ("openpyxl", "Excel hisobot (openpyxl)", "pip install openpyxl"),
                            ("httpx", "httpx", "pip install httpx"), ("telethon", "Telethon", "pip install telethon")):
        try:
            __import__(mod)
            out.append(item(g, label, OK, "O'rnatilgan", key=mod))
        except Exception:
            out.append(item(g, label, FAIL, "O'rnatilmagan", fix + " (yoki run.bat ni qayta ishga tushiring)", key=mod))
    return out


async def check_worker():
    g = "Kuzatuv havolasi"
    if not ch_track.enabled():
        return [item(g, "Cloudflare Worker", INFO, "Ulanmagan", "Post havolalaridagi bosilishni sanash uchun code\\tools\\CLOUDFLARE.md bo'yicha ulang (bepul).", key="worker")]
    try:
        r = await ch_track.ping()
        return [item(g, "Cloudflare Worker", OK, f"Javob berdi: {json.dumps(r, ensure_ascii=False)[:120]}", key="worker"),
                item(g, "Bepul limit", INFO, "Kuniga 1000 ta yozuv (KV). Oshsa yo'naltirish ishlayveradi, sanash to'xtaydi.", key="kv")]
    except Exception as e:
        return [item(g, "Cloudflare Worker", FAIL, f"{type(e).__name__}: {e}"[:250], "Worker manzili/kalitini Sozlamalarda tekshiring, Cloudflare'da deploy qilinganini ko'ring.", key="worker")]


def check_loops():
    g = "Fon jarayonlari"
    out = []
    beats = {r["name"]: r for r in db.q("SELECT * FROM sys_beat")}
    for name, title in LOOP_TITLES.items():
        t = TASKS.get(name)
        b = beats.get(name)
        if t is None:
            out.append(item(g, title, INFO, "Kuzatilmayapti", key=f"loop_{name}"))
        elif name in ONESHOT and t.done() and not t.cancelled() and not (b and b["state"] == "crashed"):
            out.append(item(g, title, OK, "Bajarildi (bir martalik vazifa, dastur ochilganda ishlaydi)", key=f"loop_{name}"))
        elif t.done() and not t.cancelled():
            exc = None
            try:
                exc = t.exception()
            except Exception:
                pass
            out.append(item(g, title, FAIL, f"To'xtagan: {exc or (b['info'] if b else '')}", "Dasturni qayta ishga tushiring; xato loglarda.", key=f"loop_{name}"))
        elif b and b["state"] == "crashed":
            out.append(item(g, title, WARN, f"{b['restarts']} marta yiqilgan, oxirgisi {b['ts'][:16]}: {b['info'][:160]}", "Avtomatik qayta ishga tushadi. Takrorlansa, loglardagi xatoni yuboring.", key=f"loop_{name}"))
        else:
            out.append(item(g, title, OK, "Ishlayapti" + (f" (qayta ishga tushgan: {b['restarts']})" if b and b["restarts"] else ""), key=f"loop_{name}"))
    return out


def check_content():
    g = "Telegram SMM ishi"
    out = []
    late = db.one("SELECT COUNT(*) c FROM ch_plan WHERE status='scheduled' AND scheduled_at < ?", ((datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),))["c"]
    failed = db.one("SELECT COUNT(*) c FROM ch_plan WHERE status IN ('failed','missed') AND scheduled_at >= ?", ((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),))["c"]
    out.append(item(g, "Kechikkan rejalashtirilgan postlar", OK if not late else FAIL, f"{late} ta", "" if not late else "Kanal akkaunti ulanganini va loglarni tekshiring: post vaqti o'tib ketgan.", key="late"))
    last_fail = db.one("SELECT title, error FROM ch_plan WHERE status='failed' ORDER BY id DESC LIMIT 1")
    out.append(item(g, "Oxirgi 7 kunda yuborilmagan postlar", OK if not failed else WARN, f"{failed} ta" + (f" (oxirgisi: {last_fail['error'][:140]})" if last_fail and last_fail["error"] else ""),
                    "" if not failed else "Kontent reja sahifasida xato sababini ko'ring; kanal akkauntida post yuborish huquqi bormi?", key="failed"))
    for ch in ch_data.channels():
        last = db.one("SELECT MAX(date) d FROM ch_posts WHERE channel_id=? AND comp_id=0", (ch["id"],))["d"]
        out.append(item(g, f"Kanal «{ch['title']}»: ma'lumot", OK if last else WARN, f"oxirgi post: {last[:16] if last else 'yo`q'}, obunachi {ch['members'] or 0}",
                        "" if last else "Kanallar → Sinxronlash ni bosing.", key=f"chan_{ch['id']}"))
    return out


def check_instagram():
    g = "Instagram"
    out = []
    accs = db.q("SELECT * FROM ig_accounts ORDER BY username")
    mode = "auto" if db.get_setting("ig_publish_mode", "reminder") == "auto" else "reminder"
    out.append(item(g, "Joylash rejimi", INFO, "Avto (cloudflared tunnel)" if mode == "auto" else "Eslatma (qo'lda joylash): tunnel kerak emas", key="ig_mode"))
    if not accs:
        out.append(item(g, "Ulangan sahifalar", INFO, "Hali Instagram sahifa ulanmagan", "Instagram → Sahifalar.", key="ig_none"))
        return out
    for a in accs:
        days = None
        if a["token_expires"]:
            try:
                days = (datetime.strptime(a["token_expires"], "%Y-%m-%d %H:%M:%S") - datetime.now()).days
            except ValueError:
                pass
        name = f"@{a['username']}"
        if a["status"] == "token":
            out.append(item(g, f"{name}: token", FAIL, (a["last_error"] or "token yaroqsiz")[:200], "Instagram → Sahifalar: yangi token kiriting.", key=f"ig_tok_{a['id']}"))
        elif days is not None and days < 0:
            out.append(item(g, f"{name}: token", FAIL, "Muddati o'tgan", "Instagram → Sahifalar: qayta ulang.", key=f"ig_tok_{a['id']}"))
        elif days is not None and days <= 10:
            out.append(item(g, f"{name}: token", WARN, f"{days} kun qoldi", "Dastur o'zi yangilaydi; yangilanmasa «Tokenni yangilash».", key=f"ig_tok_{a['id']}"))
        else:
            out.append(item(g, f"{name}: token", OK, f"{days} kun qoldi" if days is not None else "Faol", key=f"ig_tok_{a['id']}"))
        sy = a["synced_at"]
        stale = True
        if sy:
            try:
                stale = datetime.now() - datetime.strptime(sy, "%Y-%m-%d %H:%M:%S") > timedelta(hours=12)
            except ValueError:
                pass
        if a["status"] == "error":
            out.append(item(g, f"{name}: ma'lumot yig'ish", WARN, (a["last_error"] or "")[:200], "Instagram → Diagnostika.", key=f"ig_sync_{a['id']}"))
        else:
            out.append(item(g, f"{name}: ma'lumot yig'ish", WARN if stale else OK, f"oxirgi: {sy[:16] if sy else 'hali yo`q'}", "Instagram → «Yangilash»." if stale else "", key=f"ig_sync_{a['id']}"))
    late = db.one("SELECT COUNT(*) c FROM ig_plan WHERE status IN ('scheduled','reminded') AND scheduled_at < ?", ((datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),))["c"]
    failed = db.one("SELECT COUNT(*) c FROM ig_plan WHERE status IN ('failed','missed') AND scheduled_at >= ?", ((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),))["c"]
    out.append(item(g, "Instagram postlari: kutayotgan/kechikkan", OK if not late else WARN, f"{late} ta vaqti o'tgan, hali belgilanmagan", "" if not late else "Instagram → Kontent reja: «Joylandi» deb belgilang yoki suring.", key="ig_late"))
    out.append(item(g, "Oxirgi 7 kunda joylanmagan postlar", OK if not failed else WARN, f"{failed} ta", "" if not failed else "Kontent reja sahifasida xato sababi ko'rinadi.", key="ig_failed"))
    return out


def check_meet():
    from . import meet_sched
    return meet_sched.health_items()


def check_imagegen():
    from . import img_work
    return img_work.health_items(item)


def check_cloud():
    from . import cf
    return cf.health_items(item)


def log_summary(hours=24, limit=8):
    """Log fayldagi ERROR/WARNING larni guruhlab beradi: qaysi bo'limda, necha marta."""
    lines = []
    for f in (LOG_DIR / "app.log", LOG_DIR / "app.log.1"):
        try:
            lines += f.read_text(encoding="utf-8", errors="ignore").splitlines()[-6000:]
        except OSError:
            pass
    cut = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    rx = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (ERROR|WARNING|CRITICAL) (.*)$")
    groups: dict[tuple, dict] = {}
    cur = None
    for ln in lines:
        m = rx.match(ln)
        if m:
            cur = None
            if m.group(1) < cut:
                continue
            msg = re.sub(r"\d+", "#", m.group(3))[:140]
            key = (m.group(2), msg)
            g = groups.setdefault(key, {"level": m.group(2), "msg": m.group(3)[:200], "n": 0, "last": m.group(1), "exc": ""})
            g["n"] += 1
            g["last"] = m.group(1)
            cur = g
        elif cur is not None and re.match(r"^[A-Za-z_.]+(Error|Exception|Exit|Timeout)\b.*", ln):
            cur["exc"] = ln[:220]
    rows = sorted(groups.values(), key=lambda x: (x["level"] != "ERROR" and x["level"] != "CRITICAL", -x["n"]))[:limit]
    for r in rows:
        r["module"] = _mod(r["msg"] + " " + r["exc"])
    totals = Counter(g["level"] for g in groups.values() for _ in range(g["n"]))
    return {"rows": rows, "errors": totals.get("ERROR", 0) + totals.get("CRITICAL", 0), "warnings": totals.get("WARNING", 0)}


def events(limit=30):
    return db.q("SELECT * FROM sys_events WHERE kind IN ('warn','error') ORDER BY id DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------- umumiy
async def run_all() -> dict:
    items: list[dict] = []
    for fn in (check_database, check_telegram, check_openai, check_media_tools, check_loops, check_content, check_instagram, check_meet, check_imagegen, check_cloud):
        try:
            items += fn()
        except Exception as e:
            log.exception("Tekshiruv yiqildi: %s", fn.__name__)
            items.append(item("Tizim", fn.__name__, FAIL, f"Tekshiruvning o'zida xato: {type(e).__name__}: {e}", "Loglarni ko'ring.", key=fn.__name__))
    try:
        items += await check_worker()
    except Exception as e:
        items.append(item("Kuzatuv havolasi", "Cloudflare Worker", FAIL, str(e)[:200], key="worker"))
    lg = log_summary()
    if lg["errors"]:
        top = lg["rows"][0] if lg["rows"] else None
        items.append(item("Loglar", "Oxirgi 24 soat xatolari", WARN if lg["errors"] < 10 else FAIL,
                          f"{lg['errors']} ta xato, {lg['warnings']} ta ogohlantirish" + (f". Asosiysi [{top['module']}]: {top['msg'][:150]}" if top else ""),
                          "Sahifa pastidagi «Loglar tahlili» jadvaliga qarang; bu xatolar eski (tuzatilgan) bo'lsa, ahamiyatsiz.", key="logs"))
    else:
        items.append(item("Loglar", "Oxirgi 24 soat xatolari", OK, f"Xato yo'q ({lg['warnings']} ogohlantirish)", key="logs"))
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault(it["group"], []).append(it)
    cnt = Counter(i["state"] for i in items)
    return {"groups": groups, "counts": cnt, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "logs": lg,
            "problems": [i for i in items if i["state"] in (FAIL, WARN)], "events": events()}


def fix_mysql() -> str:
    """Xavfsiz avto-tuzatish: MySQL'ni ishga tushirishga urinadi."""
    from . import mysqldb
    from .xampp import ensure_running
    conf = mysqldb.load_conf()
    port = ensure_running(conf["host"], int(conf["port"]), conf.get("xampp_dir") or None, wait=40)
    return f"MySQL port {port} da ishlayapti"


AI_SYSTEM = """You are a support engineer for a local Windows app (FastAPI + Telethon + MySQL/XAMPP + OpenAI + optional local GPU image generation and Whisper).
You receive a health-check report and recent log errors. Give the owner (non-developer, Uzbek speaker) a short conclusion in Uzbek (Latin):
which parts are broken, the most likely root cause of each (from the evidence only), and exact next steps in order. Do not invent facts.
Return ONLY JSON: {"summary": "2-4 sentences", "problems": [{"title": "...", "cause": "...", "steps": ["step 1", "step 2"]}], "all_good": true|false}"""


async def ai_conclusion(report: dict) -> dict:
    slim = {"problems": [{k: p[k] for k in ("group", "title", "state", "detail")} for p in report["problems"]],
            "log_errors": [{k: r[k] for k in ("level", "module", "msg", "exc", "n", "last")} for r in report["logs"]["rows"]],
            "events": [{"ts": e["ts"], "source": e["source"], "info": e["info"][:200]} for e in report["events"][:12]]}
    data, _ = await ai.chat(0, "analysis", AI_SYSTEM, json.dumps(slim, ensure_ascii=False)[:9000], max_out=1800, note="tizim tahlili")
    return data if isinstance(data, dict) else {"summary": str(data), "problems": [], "all_good": False}
