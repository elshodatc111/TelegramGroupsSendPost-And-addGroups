"""Ishga tushirish: python -m app

Server tayyor bo'lgach brauzerda avtomatik ochiladi.
Agar dastur allaqachon ishlab turgan bo'lsa, faqat brauzerni ochadi.
"""
import socket
import threading
import time
import webbrowser

import uvicorn

from .config import HOST, PORT

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


if __name__ == "__main__":
    if port_open():
        print(f"Dastur allaqachon ishlayapti: {URL}")
        webbrowser.open(URL)
    else:
        threading.Thread(target=open_when_ready, daemon=True).start()
        print(f"\n  Telegram Group Post: {URL}\n  To'xtatish uchun bu oynani yoping yoki Ctrl+C bosing.\n")
        uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")
