"""Kompyuterga bog'lash: ma'lumotlar boshqa kompyuterga ko'chirilsa (Github, nusxa, flesh), akkauntlar, baza va
barcha saqlangan ma'lumotlar tozalanadi. Hammasi noldan boshlanadi.

Ikki qatlam:
1. Fayllar: data\\machine.id — kompyuter izi. Mos kelmasa data papkasi (session, media, baza sozlamasi ...) tozalanadi.
2. Baza: MySQL ichida ham iz saqlanadi (settings.machine_fp). Mos kelmasa barcha jadvallar o'chiriladi.
Qo'shimcha: maxfiy qiymatlar (API HASH, OpenAI kaliti, MySQL paroli) kompyuterga bog'liq shifrlangan (secure.py),
ya'ni nusxasi boshqa kompyuterda baribir o'qilmaydi.
"""
import shutil

from . import secure
from .config import DATA_DIR, log

MARK = DATA_DIR / "machine.id"
KEEP = {"README.txt", "machine.id"}


def fingerprint() -> str:
    return secure.fingerprint()


def wipe():
    for p in DATA_DIR.iterdir():
        if p.name in KEEP:
            continue
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            log.warning("O'chirib bo'lmadi: %s", p)
    for d in ("sessions", "media", "imports", "logs", "backups", "tmp"):
        (DATA_DIR / d).mkdir(exist_ok=True)


def check() -> bool:
    """True qaytarsa, ma'lumotlar tozalandi (boshqa kompyuter aniqlandi)."""
    fp, old = fingerprint(), secure.legacy_fingerprint()
    if not MARK.exists():
        MARK.write_text(fp)          # birinchi ishga tushish: shu kompyuterga bog'lanadi
        return False
    cur = MARK.read_text().strip()
    if cur == fp:
        return False
    if cur == old:                   # eski formula bilan yozilgan iz: shu kompyuter, yangi formulaga o'tkazamiz
        MARK.write_text(fp)
        return False
    log.warning("Boshqa kompyuter aniqlandi: session va ma'lumotlar tozalanmoqda")
    wipe()
    MARK.write_text(fp)
    return True


def check_db() -> bool:
    """Baza ichidagi iz. Mos kelmasa barcha jadvallar o'chiriladi va qayta yaratiladi. True = tozalandi."""
    from . import db
    fp, old = fingerprint(), secure.legacy_fingerprint()
    r = db.one("SELECT value FROM settings WHERE key='machine_fp'")
    if not r:
        db.ex("INSERT OR REPLACE INTO settings(key,value) VALUES('machine_fp',?)", (fp,))
        return False
    if r["value"] in (fp, old):
        if r["value"] != fp:
            db.ex("UPDATE settings SET value=? WHERE key='machine_fp'", (fp,))
        return False
    log.warning("Baza boshqa kompyuterga tegishli: barcha jadvallar tozalanmoqda")
    db.wipe_all()
    db.ex("INSERT OR REPLACE INTO settings(key,value) VALUES('machine_fp',?)", (fp,))
    wipe()
    return True
