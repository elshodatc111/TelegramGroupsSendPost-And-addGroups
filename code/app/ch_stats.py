"""Kanallarim: saqlangan postlardan ko'rsatkichlar (AI'siz, oddiy hisob-kitob)."""
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import db

FMT = "%Y-%m-%d %H:%M:%S"
WD = ["Dush", "Sesh", "Chor", "Pay", "Jum", "Shan", "Yak"]
MEDIA_LABELS = {"text": "Matn", "photo": "Rasm", "video": "Video", "album": "Albom", "voice": "Ovoz", "audio": "Audio",
                "doc": "Fayl", "poll": "So'rovnoma"}


def _cut(days):
    return (datetime.now() - timedelta(days=days)).strftime(FMT)


def posts(cid, comp_id, days=30):
    return db.q("SELECT * FROM ch_posts WHERE channel_id=? AND comp_id=? AND date>=? ORDER BY date DESC",
                (cid, comp_id, _cut(days)))


def members_of(cid, comp_id):
    if comp_id == 0:
        r = db.one("SELECT members FROM ch_channels WHERE id=?", (cid,))
    else:
        r = db.one("SELECT members FROM ch_competitors WHERE id=?", (comp_id,))
    return (r["members"] if r else None) or 0


def _mature(rows, hours=12):
    lim = (datetime.now() - timedelta(hours=hours)).strftime(FMT)
    return [r for r in rows if r["date"] and r["date"] <= lim]


def source_stats(cid, comp_id, days=30) -> dict:
    rows = posts(cid, comp_id, days)
    mem = members_of(cid, comp_id)
    m = _mature(rows)
    views = [r["views"] for r in m if r["views"]]
    n = len(rows)
    mix = Counter(r["media"] or "text" for r in rows)
    vids = [r["duration"] for r in rows if r["media"] == "video" and r["duration"]]
    by_hour = defaultdict(list)
    for r in m:
        try:
            by_hour[int(r["date"][11:13])].append(r["views"] or 0)
        except (TypeError, ValueError):
            pass
    best_hours = sorted(((h, sum(v) / len(v), len(v)) for h, v in by_hour.items() if len(v) >= 2), key=lambda x: -x[1])[:3]
    avg = (sum(views) / len(views)) if views else 0
    return {
        "posts": n, "per_day": round(n / max(days, 1), 2), "members": mem,
        "avg_views": round(avg), "median_views": round(statistics.median(views)) if views else 0,
        "er": round(avg / mem * 100, 1) if mem else None,
        "avg_reactions": round(sum(r["reactions"] or 0 for r in m) / len(m), 1) if m else 0,
        "avg_forwards": round(sum(r["forwards"] or 0 for r in m) / len(m), 1) if m else 0,
        "mix": {k: {"n": v, "pct": round(v / n * 100)} for k, v in mix.most_common()} if n else {},
        "video_share": round(mix.get("video", 0) / n * 100) if n else 0,
        "avg_video_sec": round(sum(vids) / len(vids)) if vids else 0,
        "link_share": round(sum(1 for r in rows if r["has_link"]) / n * 100) if n else 0,
        "avg_len": round(sum(len(r["text"] or "") for r in rows) / n) if n else 0,
        "best_hours": [{"hour": h, "avg": round(a), "n": c} for h, a, c in best_hours],
    }


def top_posts(cid, comp_id, days=30, n=8, by="err"):
    rows = _mature(posts(cid, comp_id, days))
    mem = members_of(cid, comp_id) or 1
    key = (lambda r: (r["views"] or 0) / mem) if by == "err" else (lambda r: r["views"] or 0)
    out = sorted(rows, key=key, reverse=True)[:n]
    return [dict(r, err=round((r["views"] or 0) / mem * 100, 1)) for r in out]


def heatmap(cid, comp_id, days=60):
    """Hafta kuni x soat bo'yicha o'rtacha ko'rishlar."""
    cells = defaultdict(list)
    for r in _mature(posts(cid, comp_id, days)):
        try:
            d = datetime.strptime(r["date"], FMT)
        except (TypeError, ValueError):
            continue
        cells[(d.weekday(), d.hour)].append(r["views"] or 0)
    grid = [[round(sum(cells[(w, h)]) / len(cells[(w, h)])) if cells.get((w, h)) else 0 for h in range(24)] for w in range(7)]
    mx = max((v for row in grid for v in row), default=0)
    return {"grid": grid, "max": mx, "wd": WD}


def growth(cid, comp_id, days=60):
    cut = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.q("SELECT day, members FROM ch_members_log WHERE channel_id=? AND comp_id=? AND day>=? ORDER BY day", (cid, comp_id, cut))
    return [(r["day"], r["members"]) for r in rows]


def growth_delta(cid, comp_id, days=7):
    g = growth(cid, comp_id, days + 1)
    if len(g) < 2:
        return None
    return g[-1][1] - g[0][1]


def compare(cid, days=30):
    """Jadval: o'z kanal + barcha raqobatchilar yonma-yon."""
    ch = db.one("SELECT * FROM ch_channels WHERE id=?", (cid,))
    out = []
    s = source_stats(cid, 0, days)
    out.append(dict(s, name=ch["title"], own=True, comp_id=0, growth7=growth_delta(cid, 0)))
    for c in db.q("SELECT * FROM ch_competitors WHERE channel_id=? AND active=1 ORDER BY title", (cid,)):
        s = source_stats(cid, c["id"], days)
        out.append(dict(s, name=c["title"], own=False, comp_id=c["id"], growth7=growth_delta(cid, c["id"]), error=c["error"],
                        last_sync=c["last_sync"], username=c["username"]))
    return out


def usage_summary(month: str | None = None):
    """Kanal bo'yicha AI sarfi (token va USD). month: 'YYYY-MM' (None = joriy oy)."""
    month = month or datetime.now().strftime("%Y-%m")
    rows = db.q("SELECT channel_id, MAX(ch_title) title, purpose, COUNT(*) calls, SUM(tokens_in) tin, SUM(tokens_out) tout, "
                "SUM(cached_in) cached, SUM(audio_sec) audio, SUM(cost) cost, MIN(priced) priced "
                "FROM ch_usage WHERE day LIKE ? GROUP BY channel_id, purpose", (month + "%",))
    chans = defaultdict(lambda: {"title": "", "calls": 0, "tin": 0, "tout": 0, "audio": 0.0, "cost": 0.0, "unpriced": False, "by": []})
    for r in rows:
        c = chans[r["channel_id"]]
        c["title"] = r["title"] or c["title"]
        c["calls"] += r["calls"]
        c["tin"] += int(r["tin"] or 0)
        c["tout"] += int(r["tout"] or 0)
        c["audio"] += float(r["audio"] or 0)
        c["cost"] += float(r["cost"] or 0)
        c["unpriced"] = c["unpriced"] or not r["priced"]
        c["by"].append(r)
    live = {r["id"]: r["title"] for r in db.q("SELECT id, title FROM ch_channels")}
    for cid, c in chans.items():
        c["id"] = cid
        c["title"] = live.get(cid) or (f"{c['title']} (o'chirilgan)" if c["title"] else f"Kanal #{cid}")
    return sorted(chans.values(), key=lambda x: -x["cost"])
