"""Loyiha sozlamalari va papka yo'llari.

Tuzilma:
    C:\TelegramGroupPost\
        data\   <- ma'lumotlar (baza, session, media, loglar)
        code\   <- dastur kodi (shu papka)
"""
import os
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TGP_DATA_DIR", CODE_DIR.parent / "data")).resolve()

DB_PATH = DATA_DIR / "app.db"
SESSION_DIR = DATA_DIR / "sessions"
MEDIA_DIR = DATA_DIR / "media"
LOG_DIR = DATA_DIR / "logs"
SESSION_PATH = SESSION_DIR / "account"

for _d in (DATA_DIR, SESSION_DIR, MEDIA_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Faqat local kompyuterdan ochiladi (xavfsizlik uchun)
HOST = "127.0.0.1"
PORT = int(os.environ.get("TGP_PORT", "8000"))

CAPTION_LIMIT = 1024   # rasm/video ostidagi matn limiti (Telegram)
MIN_DELAY_FLOOR = 3    # ruxsat etilgan eng kichik interval (soniya)
