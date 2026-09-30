"""Maxfiy ma'lumotlarni shifrlash va kompyuter izi.

- Windows'da Windows DPAPI (joriy foydalanuvchi + shu kompyuter) ishlatiladi: shifrlangan qiymatni boshqa kompyuterda
  (yoki boshqa Windows foydalanuvchisi ostida) ochib bo'lmaydi.
- Boshqa tizimlarda (yoki DPAPI ishlamasa) kompyuter izidan olingan kalit bilan Fernet ishlatiladi.

Shifrlanadigan qiymatlar: API HASH, OpenAI kaliti, MySQL paroli (data/dbconf.json), cookie maxfiy kaliti.
"""
import base64
import getpass
import hashlib
import os
import platform
import sys
import uuid

PREFIX_DPAPI = "dpapi1:"
PREFIX_FK = "fk1:"
_ENTROPY = b"telegram-group-post/v1"


# ---------------- kompyuter izi ----------------
def machine_guid() -> str:
    """Kompyuterning barqaror identifikatori."""
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0])
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            if os.path.exists(p):
                return open(p).read().strip()
    except Exception:
        pass
    return str(uuid.getnode())


def fingerprint() -> str:
    """Yangi iz (v2): MachineGuid + kompyuter nomi + foydalanuvchi."""
    raw = f"v2|{machine_guid()}|{platform.node()}|{getpass.getuser()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def legacy_fingerprint() -> str:
    """Eski formula (machine.id fayllari shu bilan yozilgan). Faqat moslik uchun: eski iz mos kelsa, yangisiga o'tkaziladi."""
    raw = f"{platform.node()}|{uuid.getnode()}|{getpass.getuser()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------- DPAPI (Windows) ----------------
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    din, _k1 = blob(data)
    ent, _k2 = blob(_ENTROPY)
    out = BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(din), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI xatosi")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


# ---------------- Fernet (kompyuter izidan kalit) ----------------
def _fernet():
    from cryptography.fernet import Fernet
    k = hashlib.sha256(f"tgp-secure|{machine_guid()}|{getpass.getuser()}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(k))


def is_encrypted(v) -> bool:
    return isinstance(v, str) and (v.startswith(PREFIX_DPAPI) or v.startswith(PREFIX_FK))


def enc(plain: str) -> str:
    """Matnni shifrlaydi. Allaqachon shifrlangan bo'lsa, o'zini qaytaradi."""
    if plain is None or plain == "" or is_encrypted(plain):
        return plain
    data = plain.encode("utf-8")
    if sys.platform == "win32":
        try:
            token = PREFIX_DPAPI + base64.b64encode(_dpapi(data, True)).decode()
            if dec(token) == plain:               # tekshiruv: qaytib ochilishi shart
                return token
        except Exception:
            pass
    return PREFIX_FK + _fernet().encrypt(data).decode()


def dec(value):
    """Shifrlangan qiymatni ochadi. Shifrlanmagan (eski) qiymat o'zgarishsiz qaytadi.
    Boshqa kompyuterda ochib bo'lmasa, None qaytadi."""
    if not is_encrypted(value):
        return value
    try:
        if value.startswith(PREFIX_DPAPI):
            return _dpapi(base64.b64decode(value[len(PREFIX_DPAPI):]), False).decode("utf-8")
        return _fernet().decrypt(value[len(PREFIX_FK):].encode()).decode("utf-8")
    except Exception:
        return None
