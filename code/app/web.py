"""Veb qatlam uchun umumiy yordamchilar: shablonlar, ikonlar, redirect."""
import html
import re
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import db
from .config import CODE_DIR

templates = Jinja2Templates(directory=str(CODE_DIR / "app" / "templates"))

STATUS_LABELS = {
    "draft": "Qoralama", "scheduled": "Rejalashtirilgan", "queued": "Navbatda",
    "running": "Yuborilmoqda", "done": "Tugadi", "cancelled": "To'xtatildi",
    "interrupted": "Uzilgan", "failed": "Xato", "waiting": "Kutmoqda",
    "pending": "Kutilmoqda", "sending": "Yuborilmoqda", "sent": "Yuborildi", "skipped": "O'tkazib yuborildi",
}
JOIN_LABELS = {
    "pending": "Kutilmoqda", "joined": "Ulandi", "already": "Allaqachon a'zo",
    "requested": "So'rov yuborildi", "notfound": "Topilmadi", "invalid": "Guruh emas",
    "private": "Yopiq / yaroqsiz", "banned": "Bloklangan", "limit": "Limit", "failed": "Xato",
}
BATCH_LABELS = dict(STATUS_LABELS, running="Ishlamoqda")

_ICONS = {
    "send": '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
    "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "user-plus": '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/>',
    "file-text": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "settings": '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
    "home": '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
    "sun": '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>',
    "moon": '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "refresh": '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "play": '<polygon points="5 3 19 12 5 21 5 3"/>',
    "menu": '<line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>',
    "video": '<polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/>',
    "eye": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
    "alert": '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>',
    "table": '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="9" y1="3" x2="9" y2="21"/>',
    "bar-chart": '<line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/>',
    "inbox": '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "tag": '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/>',
    "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "repeat": '<polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>',
    "layers": '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    "chevron-down": '<polyline points="6 9 12 15 18 9"/>',
    "log-out": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
    "message": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "file": '<path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/>',
    "bulb": '<path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.2 1 2V17h6v-.3c0-.8.4-1.5 1-2A7 7 0 0 0 12 2z"/>',
    "trending": '<polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/>',
    "cpu": '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>',
    "film": '<rect x="2" y="2" width="20" height="20" rx="2.18"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
}
ICONS = {k: Markup(v) for k, v in _ICONS.items()}


def ic(name: str, cls: str = "") -> Markup:
    return Markup(f'<svg class="ic {cls}" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-{name}"/></svg>')


_ALLOWED_TAGS = ("b", "strong", "i", "em", "u", "s", "code", "pre")


def render_text(text: str, mode: str = "none") -> Markup:
    """Preview uchun xavfsiz matn (HTML rejimida faqat ruxsat etilgan teglar)."""
    esc = html.escape(text or "")
    if mode == "html":
        for t in _ALLOWED_TAGS:
            esc = re.sub(rf"&lt;(/?){t}&gt;", rf"<\1{t}>", esc)
        esc = re.sub(r'&lt;a href=&quot;(https?://[^"<>\s]+?)&quot;&gt;',
                     r'<a href="\1" target="_blank" rel="noopener">', esc)
        esc = esc.replace("&lt;/a&gt;", "</a>")
    return Markup(esc.replace("\n", "<br>"))


def spark(values, w=90, h=26, cls="") -> Markup:
    """Kichik chiziqli grafik (SVG)."""
    vals = [v for v in (values or []) if v is not None]
    if len(vals) < 2:
        return Markup('<span class="mut tiny">—</span>')
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pts = " ".join(f"{i * (w - 2) / (len(vals) - 1) + 1:.1f},{h - 2 - (v - lo) / rng * (h - 4):.1f}" for i, v in enumerate(vals))
    up = vals[-1] >= vals[0]
    return Markup(f'<svg class="spark {cls}" width="{w}" height="{h}" viewBox="0 0 {w} {h}"><polyline fill="none" '
                  f'stroke="{"var(--blue)" if up else "var(--pri)"}" stroke-width="2" stroke-linejoin="round" points="{pts}"/></svg>')


def num(v) -> str:
    try:
        return f"{int(v):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def usd(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"${v:,.4f}" if v < 1 else f"${v:,.2f}"


templates.env.filters["render_text"] = render_text
templates.env.filters["num"] = num
templates.env.filters["usd"] = usd
templates.env.globals["spark"] = spark
templates.env.globals.update(STATUS=STATUS_LABELS, JOIN=JOIN_LABELS, BATCH=BATCH_LABELS, ICONS=ICONS, ic=ic)


def need_account(request: Request):
    """Akkaunt tanlanmagan bo'lsa, akkaunt qo'shish sahifasiga yo'naltiradi."""
    if not acc_id(request):
        return go("/accounts", err="Avval Telegram akkaunt qo'shing")
    return None


def acc_id(request: Request):
    return getattr(request.state, "account_id", None)


def ws_of(request: Request) -> str:
    return getattr(request.state, "ws", "posting")


def cur_channel(request: Request):
    """Kanallarim: tanlangan kanal (yoki None — «Barcha kanallar» / kanal yo'q)."""
    return getattr(request.state, "channel", None)


def need_channel(request: Request):
    """Kanal tanlanmagan bo'lsa, kanal tanlash sahifasini ko'rsatadi."""
    if cur_channel(request):
        return None
    return page(request, "ch_pick.html")


def page(request: Request, name: str, status_code: int = 200, **ctx):
    from .core import manager
    ctx.setdefault("msg", request.query_params.get("msg"))
    if not ctx["msg"] and (n := db.get_setting("machine_notice")):
        ctx["msg"] = n
        db.del_setting("machine_notice")
    ctx.setdefault("err", request.query_params.get("err"))
    user = getattr(request.state, "user", None)
    ws = ws_of(request)
    accounts, cur = [], None
    if ws == "posting":
        try:
            accounts = manager.list(user["id"], "posting") if user else []
        except Exception:
            accounts = []
        aid = acc_id(request)
        cur = next((a for a in accounts if a["id"] == aid), None)
    ctx.update(accounts=accounts, cur=cur, unread=cur["unread"] if cur else 0,
               auth_on=bool(db.get_setting("password_hash")), user=user, ws=ws,
               ch_list=getattr(request.state, "channels", []), cur_ch=cur_channel(request),
               ch_all=getattr(request.state, "ch_all", False), ch_acc=getattr(request.state, "ch_account", None),
               alerts_unseen=_unseen() if ws == "channels" else 0)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _unseen() -> int:
    try:
        from . import ch_insight
        return ch_insight.unseen()
    except Exception:
        return 0


def go(url: str, msg: str | None = None, err: str | None = None):
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif err:
        url += f"{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)
