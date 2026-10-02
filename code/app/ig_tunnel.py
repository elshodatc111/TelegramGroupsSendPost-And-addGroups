"""Avto joylash uchun vaqtinchalik ochiq havola (Cloudflare Quick Tunnel, bepul, akkauntsiz).

Faqat postdan oldin ochiladi va keyin yopiladi. Tunnel orqali faqat /ig-pub/<token>/<fayl> ochiq:
boshqa hamma manzil main.py dagi qo'riqchi tomonidan to'sib qo'yiladi.
"""
import asyncio
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

import httpx

from . import db
from .config import CODE_DIR, MEDIA_DIR, PORT, log

EXE = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
DL_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
TOOLS = CODE_DIR / "tools"

_grants: dict[str, tuple[str, float]] = {}      # token -> (nisbiy fayl yo'li, amal qilish vaqti)
_proc: asyncio.subprocess.Process | None = None
_url: str | None = None
_lock = asyncio.Lock()
_users = 0


def find_exe() -> str | None:
    p = (db.get_setting("ig_cloudflared") or "").strip().strip('"')
    if p and Path(p).exists():
        return p
    local = TOOLS / EXE
    if local.exists():
        return str(local)
    return shutil.which("cloudflared")


def grant(rel_path: str, ttl: int = 1800) -> str:
    now = time.time()
    for k in [k for k, v in _grants.items() if v[1] < now]:
        _grants.pop(k, None)
    t = secrets.token_urlsafe(24)
    _grants[t] = (rel_path, now + ttl)
    return t


def resolve(token: str, name: str) -> Path | None:
    g = _grants.get(token)
    if not g or g[1] < time.time():
        return None
    rel = g[0]
    if Path(rel).name != name:
        return None
    p = (MEDIA_DIR / rel).resolve()
    if MEDIA_DIR.resolve() not in p.parents or not p.is_file():
        return None
    return p


def revoke_all():
    _grants.clear()


async def open_tunnel() -> str:
    """Tunnelni ochadi (allaqachon ochiq bo'lsa, qayta ishlatadi) va https manzilini qaytaradi."""
    global _proc, _url, _users
    async with _lock:
        if _proc and _proc.returncode is None and _url:
            _users += 1
            return _url
        exe = find_exe()
        if not exe:
            raise RuntimeError("cloudflared topilmadi. Instagram → Sozlamalar → «cloudflared'ni yuklab olish» ni bosing.")
        _proc = await asyncio.create_subprocess_exec(
            exe, "tunnel", "--url", f"http://127.0.0.1:{PORT}", "--no-autoupdate",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        url = None
        t0 = time.time()
        try:
            while time.time() - t0 < 45:
                line = await asyncio.wait_for(_proc.stderr.readline(), timeout=45)
                if not line:
                    break
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line.decode(errors="ignore"))
                if m:
                    url = m.group(0)
                    break
        except asyncio.TimeoutError:
            pass
        if not url:
            await _kill()
            raise RuntimeError("Tunnel ochilmadi (45 soniya). Internetni va cloudflared'ni tekshiring.")
        _url = url
        _users = 1
        asyncio.create_task(_drain(_proc))
        await asyncio.sleep(3)          # Cloudflare marshrutni tarqatguncha
        return url


async def _drain(proc):
    try:
        while proc.stderr and await proc.stderr.readline():
            pass
    except Exception:
        pass


async def _kill():
    global _proc, _url
    p, _proc, _url = _proc, None, None
    if p and p.returncode is None:
        try:
            p.terminate()
            await asyncio.wait_for(p.wait(), 8)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


async def close_tunnel():
    global _users
    async with _lock:
        _users = max(0, _users - 1)
        if _users == 0:
            await _kill()
            revoke_all()
            log.info("Instagram tunnel yopildi")


def public_url(base: str, rel_path: str, token: str) -> str:
    return f"{base}/ig-pub/{token}/{Path(rel_path).name}"


async def selftest(base: str) -> tuple[bool, str]:
    """Tashqaridan o'zimizning faylga kira olamizmi? (tunnel haqiqatan ishlayotganini tekshirish)"""
    d = MEDIA_DIR / "ig"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "_selftest.txt"
    f.write_text("ok", encoding="utf-8")
    tok = grant("ig/_selftest.txt", 120)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c:
            r = await c.get(public_url(base, "ig/_selftest.txt", tok))
            r2 = await c.get(f"{base}/")          # bu to'silgan bo'lishi kerak
        if r.status_code != 200 or r.text.strip() != "ok":
            return False, f"Tunnel orqali fayl ochilmadi (HTTP {r.status_code})"
        if r2.status_code not in (403, 404):
            return False, f"XAVF: tunnel orqali bosh sahifa ochiq (HTTP {r2.status_code})! Avto rejimni ishlatmang."
        return True, "Tunnel ishlayapti, faqat bitta fayl ochiq, qolgan sahifalar yopiq"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        f.unlink(missing_ok=True)


async def download_exe() -> str:
    if sys.platform != "win32":
        raise RuntimeError("Avto-yuklash faqat Windows uchun. cloudflared'ni qo'lda o'rnating.")
    TOOLS.mkdir(parents=True, exist_ok=True)
    dest = TOOLS / EXE
    tmp = dest.with_suffix(".part")
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20), follow_redirects=True) as c:
        async with c.stream("GET", DL_URL) as r:
            if r.status_code != 200:
                raise RuntimeError(f"Yuklab bo'lmadi: HTTP {r.status_code}")
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    f.write(chunk)
    if tmp.stat().st_size < 5_000_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Yuklangan fayl noto'g'ri (juda kichik)")
    tmp.replace(dest)
    return str(dest)
