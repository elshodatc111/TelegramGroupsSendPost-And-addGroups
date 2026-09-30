"""Windows bildirishnomalari (toast) va tizim voqealari jurnali (sys_events)."""
import base64
import subprocess
import sys
from xml.sax.saxutils import escape

from . import db
from .config import log

_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def event(kind: str, source: str, info: str = ""):
    """kind: info | warn | error. Tizim tahlili bo'limida ko'rinadi."""
    try:
        db.ex("INSERT INTO sys_events(ts,kind,source,info) VALUES(?,?,?,?)", (db.now(), kind, source[:60], str(info)[:1500]))
        db.ex("DELETE FROM sys_events WHERE id < (SELECT m FROM (SELECT MAX(id)-2000 m FROM sys_events) t)")
    except Exception:
        log.debug("sys_events yozilmadi", exc_info=True)


def toast(title: str, msg: str) -> bool:
    """Windows bildirishnomasi. Boshqa tizimda yoki o'chirilgan bo'lsa False."""
    if sys.platform != "win32" or db.get_setting("ch_notify", "1") == "0":
        return False
    t, m = escape(title[:120]).replace("'", "''"), escape(msg[:240]).replace("'", "''")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$x.LoadXml('<toast><visual><binding template=\"ToastText02\"><text id=\"1\">{t}</text><text id=\"2\">{m}</text></binding></visual></toast>')\n"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($x)\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{_AUMID}').Show($n)\n")
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", enc],
                         creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        log.warning("Windows bildirishnomasi yuborilmadi", exc_info=True)
        return False
