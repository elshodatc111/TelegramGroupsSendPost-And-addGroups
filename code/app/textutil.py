"""Matn yordamchilari: spintax, o'zgaruvchilar, UTM."""
import re
import random
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SPIN = re.compile(r"\{([^{}]*\|[^{}]*)\}")
_URL = re.compile(r"https?://[^\s<>\"']+")
_SKIP_HOSTS = ("t.me", "telegram.me", "telegram.org")


def spin(text: str) -> str:
    """{a|b|c} ichidan tasodifiy bittasini tanlaydi (ichma-ich ham ishlaydi)."""
    if not text:
        return ""
    for _ in range(20):
        new = _SPIN.sub(lambda m: random.choice(m.group(1).split("|")), text)
        if new == text:
            break
        text = new
    return text


def spin_variants_count(text: str) -> int:
    n = 1
    for m in _SPIN.finditer(text or ""):
        n *= len(m.group(1).split("|"))
    return n


def add_utm(text: str, source: str, campaign: str, content: str) -> str:
    def repl(m):
        url = m.group(0)
        trail = ""
        while url and url[-1] in ".,;:!?)":
            trail = url[-1] + trail
            url = url[:-1]
        try:
            p = urlsplit(url)
        except ValueError:
            return m.group(0)
        if p.hostname and p.hostname.lower().removeprefix("www.") in _SKIP_HOSTS:
            return m.group(0)
        qs = dict(parse_qsl(p.query, keep_blank_values=True))
        qs.setdefault("utm_source", source or "telegram")
        qs.setdefault("utm_medium", "telegram")
        if campaign:
            qs.setdefault("utm_campaign", campaign)
        if content:
            qs.setdefault("utm_content", content)
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(qs), p.fragment)) + trail
    return _URL.sub(repl, text)


def render_variant(text: str, group: dict | None, utm: dict | None = None) -> str:
    """Yuborishdan oldin: spintax -> o'zgaruvchilar -> UTM."""
    t = spin(text or "")
    title = (group or {}).get("title") or ""
    uname = (group or {}).get("username") or ""
    t = t.replace("{guruh}", title).replace("{username}", ("@" + uname) if uname else title)
    if utm:
        content = uname or str((group or {}).get("tg_id", "")).lstrip("-")
        t = add_utm(t, utm.get("source"), utm.get("campaign"), content)
    return t


def slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (s or "").strip().lower()).strip("_")
    return s or "kampaniya"
