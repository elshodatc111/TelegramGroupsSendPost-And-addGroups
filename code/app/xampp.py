r"""XAMPP MySQL/MariaDB serverini topish va (ishlamayotgan bo'lsa) avtomatik ishga tushirish."""
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import log


class DbUnavailable(RuntimeError):
    """MySQL serveriga ulanib bo'lmadi va uni ishga tushirib ham bo'lmadi."""


def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_xampp(hint: str | None = None) -> Path | None:
    cands = []
    if hint:
        cands.append(hint)
    if os.environ.get("XAMPP_DIR"):
        cands.append(os.environ["XAMPP_DIR"])
    drives = [f"{d}:\\" for d in "CDEFGH"]
    for d in drives:
        cands += [d + "xampp", d + "Program Files\\xampp", d + "Program Files (x86)\\xampp"]
    cands += [str(Path.home() / "xampp"), "/opt/lampp"]
    for c in cands:
        p = Path(c)
        if (p / "mysql" / "bin" / ("mysqld.exe" if sys.platform == "win32" else "mysqld")).exists() or \
                (p / "bin" / "mysqld").exists():
            return p
    return None


def ini_port(xampp: Path) -> int | None:
    ini = xampp / "mysql" / "bin" / "my.ini"
    try:
        sec = None
        for line in ini.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("["):
                sec = line.lower()
            elif sec == "[mysqld]" and re.match(r"port\s*=", line):
                return int(re.split(r"=", line, 1)[1].split("#")[0].strip())
    except Exception:
        pass
    return None


def ensure_running(host: str = "127.0.0.1", port: int = 3306, xampp_hint: str | None = None, wait: int = 75) -> int:
    """MySQL ishlayotganini ta'minlaydi. Haqiqiy port raqamini qaytaradi.
    Ishlamasa, XAMPP'dagi mysqld'ni yashirin rejimda ishga tushiradi."""
    x = find_xampp(xampp_hint)
    real_port = (ini_port(x) if x else None) or port
    if port_open(host, real_port):
        return real_port
    if port != real_port and port_open(host, port):
        return port
    if not x or sys.platform != "win32":
        raise DbUnavailable(
            f"MySQL serveri ishlamayapti ({host}:{real_port}) va XAMPP topilmadi. "
            "XAMPP Control Panel'da MySQL'ni Start qiling yoki data\\dbconf.json da xampp_dir ni ko'rsating.")
    mysqld = x / "mysql" / "bin" / "mysqld.exe"
    ini = x / "mysql" / "bin" / "my.ini"
    cmd = [str(mysqld)] + ([f"--defaults-file={ini}"] if ini.exists() else []) + ["--standalone", "--console"]
    log.info("XAMPP MySQL ishga tushirilmoqda: %s", mysqld)
    flags = 0x08000000 | 0x00000008          # CREATE_NO_WINDOW | DETACHED_PROCESS
    try:
        subprocess.Popen(cmd, cwd=str(mysqld.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    except OSError as e:
        raise DbUnavailable(f"MySQL ishga tushmadi: {e}")
    t0 = time.time()
    while time.time() - t0 < wait:
        if port_open(host, real_port):
            time.sleep(1.0)                  # serverga to'liq tayyor bo'lishi uchun
            return real_port
        time.sleep(0.7)
    raise DbUnavailable("XAMPP MySQL serveri 75 soniyada ishga tushmadi. XAMPP Control Panel'da MySQL logini tekshiring.")
