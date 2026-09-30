"""Kompyuterga bog'lash: ma'lumotlar papkasi boshqa kompyuterga ko'chirilsa (Github, nusxa, flesh),
akkauntlar va bazadagi barcha ma'lumotlar tozalanadi. Hammasi noldan boshlanadi."""
import getpass
import hashlib
import platform
import shutil
import uuid

from .config import DATA_DIR, log

MARK = DATA_DIR / "machine.id"
KEEP = {"README.txt", "machine.id"}


def fingerprint() -> str:
    raw = f"{platform.node()}|{uuid.getnode()}|{getpass.getuser()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def wipe():
    for p in DATA_DIR.iterdir():
        if p.name in KEEP:
            continue
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            log.warning("O'chirib bo'lmadi: %s", p)
    for d in ("sessions", "media", "imports", "logs"):
        (DATA_DIR / d).mkdir(exist_ok=True)


def check() -> bool:
    """True qaytarsa, ma'lumotlar tozalandi (boshqa kompyuter aniqlandi)."""
    fp = fingerprint()
    if not MARK.exists():
        MARK.write_text(fp)          # birinchi ishga tushish yoki eski o'rnatish: shu kompyuterga bog'lanadi
        return False
    if MARK.read_text().strip() == fp:
        return False
    log.warning("Boshqa kompyuter aniqlandi: session va ma'lumotlar tozalanmoqda")
    wipe()
    MARK.write_text(fp)
    return True
