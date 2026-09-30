r"""Loyiha sozlamalari va papka yo'llari.

Tuzilma:
    C:\TelegramGroupPost\
        data\   <- ma'lumotlar (baza, session, media, importlar, loglar)
        code\   <- dastur kodi (shu papka)
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TGP_DATA_DIR", CODE_DIR.parent / "data")).resolve()

DB_PATH = DATA_DIR / "app.db"            # eski SQLite bazasi (MySQL'ga ko'chirish manbai / zaxira)
DBCONF_PATH = DATA_DIR / "dbconf.json"    # MySQL ulanish sozlamasi (parol shifrlangan)
BACKUP_DIR = DATA_DIR / "backups"
TMP_DIR = DATA_DIR / "tmp"               # vaqtinchalik fayllar (video transkripsiya va h.k.)
# Baza turi: "mysql" (standart, XAMPP) yoki "sqlite" (eski rejim)
DB_BACKEND = os.environ.get("TGP_DB", "mysql").lower()
SESSION_DIR = DATA_DIR / "sessions"
MEDIA_DIR = DATA_DIR / "media"
IMPORT_DIR = DATA_DIR / "imports"
LOG_DIR = DATA_DIR / "logs"
SESSION_PATH = SESSION_DIR / "account"

for _d in (DATA_DIR, SESSION_DIR, MEDIA_DIR, IMPORT_DIR, LOG_DIR, BACKUP_DIR, TMP_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Faqat local kompyuterdan ochiladi (xavfsizlik uchun)
HOST = "127.0.0.1"
PORT = int(os.environ.get("TGP_PORT", "8000"))

CAPTION_LIMIT = 1024      # rasm/video ostidagi matn limiti (Telegram)
MIN_DELAY_FLOOR = 3       # post yuborishda eng kichik interval (soniya)
JOIN_MIN_FLOOR = 15       # a'zo bo'lishda eng kichik interval (soniya)
MAX_UPLOAD_MB = 2000      # Telegram limiti ~2 GB


def _setup_logging():
    logger = logging.getLogger("tgp")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fh = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    return logger


log = _setup_logging()
