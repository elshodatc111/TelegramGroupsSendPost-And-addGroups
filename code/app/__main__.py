"""Ishga tushirish: python -m app

Server tayyor bo'lgach brauzerda avtomatik ochiladi.
Agar dastur allaqachon ishlab turgan bo'lsa, faqat brauzerni ochadi.
"""
import socket
import threading
import time
import webbrowser

import sys

import uvicorn

from .config import DB_BACKEND, HOST, PORT

URL = f"http://{HOST}:{PORT}"


def port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


def open_when_ready():
    for _ in range(120):          # ~60 soniyagacha kutadi
        if port_open():
            webbrowser.open(URL)
            return
        time.sleep(0.5)


def prestart_db() -> bool:
    """XAMPP MySQL ishlamayotgan bo'lsa avtomatik ishga tushiradi. Bo'lmasa tushunarli xabar chiqaradi."""
    if DB_BACKEND != "mysql":
        return True
    from . import mysqldb
    from .xampp import DbUnavailable
    print("  MySQL (XAMPP) tekshirilmoqda...")
    try:
        mysqldb.connect_server()
        return True
    except DbUnavailable as e:
        print(f"\n  XATO: {e}\n\n  XAMPP o'rnatilganiga ishonch hosil qiling (masalan C:\\xampp) yoki XAMPP Control Panel'da MySQL ni Start qiling.")
        return False


if __name__ == "__main__":
    if port_open():
        print(f"Dastur allaqachon ishlayapti: {URL}")
        webbrowser.open(URL)
    else:
        if not prestart_db():
            sys.exit(1)
        threading.Thread(target=open_when_ready, daemon=True).start()
        print(f"\n  Telegram Group Post: {URL}\n  To'xtatish uchun bu oynani yoping yoki Ctrl+C bosing.\n")
        uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")
