"""Instagram statistikasi: hisob-kitoblar (AI'siz)."""
from collections import defaultdict
from datetime import datetime, timedelta

from . import db

PRODUCT_L = {"REELS": "Reels", "FEED": "Post", "STORY": "Stories", "AD": "Reklama"}
FMT_L = {"REELS": "Reels", "CAROUSEL_ALBUM": "Karusel", "IMAGE": "Rasm", "VIDEO": "Video"}


def fmt_of(m) -> str:
    if m["product"] == "REELS":
        return "REELS"
    return m["mtype"] or "IMAGE"


def daily(aid, days=30):
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = {r["day"]: dict(r) for r in db.q("SELECT * FROM ig_daily WHERE account_id=? AND day>=? ORDER BY day", (aid, since))}
    out, prev = [], None
    for i in range(days, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        r = rows.get(d, {"day": d})
        for k in ("followers", "follows", "reach", "views", "interactions", "profile_views", "likes", "comments", "shares", "saves", "new_followers"):
            r.setdefault(k, None)
        net = (r["followers"] - prev) if (r.get("followers") is not None and prev is not None) else None
        r["net"] = net
        if r.get("followers") is not None:
            prev = r["followers"]
        out.append(r)
    return out


def media(aid, days=None, limit=200):
    if days:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return db.q("SELECT * FROM ig_media WHERE account_id=? AND ts>=? ORDER BY ts DESC LIMIT ?", (aid, since, limit))
    return db.q("SELECT * FROM ig_media WHERE account_id=? ORDER BY ts DESC LIMIT ?", (aid, limit))


def er(m) -> float:
    """Jalb qilish: (like+izoh+saqlash+ulashish) / qamrov, foizda."""
    reach = m["reach"] or 0
    inter = m["interactions"] or ((m["likes"] or 0) + (m["comments"] or 0) + (m["saved"] or 0) + (m["shares"] or 0))
    return round(inter * 100.0 / reach, 2) if reach else 0.0


def by_format(aid, days=60):
    g = defaultdict(list)
    for m in media(aid, days):
        g[fmt_of(m)].append(m)
    out = []
    for k, ms in g.items():
        withr = [m for m in ms if m["reach"]]
        out.append({"fmt": k, "label": FMT_L.get(k, k), "n": len(ms),
                    "reach": int(sum(m["reach"] for m in withr) / len(withr)) if withr else 0,
                    "er": round(sum(er(m) for m in withr) / len(withr), 2) if withr else 0,
                    "saves": round(sum(m["saved"] or 0 for m in ms) / len(ms), 1),
                    "shares": round(sum(m["shares"] or 0 for m in ms) / len(ms), 1)})
    return sorted(out, key=lambda x: -x["reach"])


def hour_scores(aid, days=90):
    """{(weekday, hour): o'rtacha qamrov} — postlar chiqqan vaqt bo'yicha."""
    g = defaultdict(list)
    for m in media(aid, days):
        try:
            t = datetime.strptime(m["ts"][:19], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if m["reach"]:
            g[(t.weekday(), t.hour)].append(m["reach"])
    return {k: sum(v) / len(v) for k, v in g.items()}


def top(aid, days=60, n=5, key="reach"):
    ms = [m for m in media(aid, days) if m["reach"]]
    return sorted(ms, key=lambda m: -(m[key] or 0))[:n]


def summary(aid, days=30):
    d = daily(aid, days)
    ms = [m for m in media(aid, days)]
    withr = [m for m in ms if m["reach"]]
    fol = [x["followers"] for x in d if x.get("followers") is not None]
    return {"followers": fol[-1] if fol else None, "growth": (fol[-1] - fol[0]) if len(fol) > 1 else None,
            "posts": len(ms), "reach_total": sum(x.get("reach") or 0 for x in d),
            "avg_reach": int(sum(m["reach"] for m in withr) / len(withr)) if withr else 0,
            "er": round(sum(er(m) for m in withr) / len(withr), 2) if withr else 0,
            "per_week": round(len(ms) * 7.0 / days, 1)}


def monthly(aid, n=12):
    rows = db.q("SELECT * FROM ig_daily WHERE account_id=? ORDER BY day", (aid,))
    g = defaultdict(list)
    for r in rows:
        g[r["day"][:7]].append(r)
    out = []
    for m in sorted(g)[-n:]:
        rs = g[m]
        fol = [r["followers"] for r in rs if r["followers"] is not None]
        out.append({"month": m, "followers": fol[-1] if fol else None, "net": (fol[-1] - fol[0]) if len(fol) > 1 else None,
                    "reach": sum(r["reach"] or 0 for r in rs), "views": sum(r["views"] or 0 for r in rs),
                    "inter": sum(r["interactions"] or 0 for r in rs)})
    return out


def goal_progress(acc) -> dict | None:
    tgt = int(acc["goal_target"] or 0)
    if not tgt:
        return None
    cur = int(acc["followers"] or 0)
    first = db.one("SELECT followers FROM ig_daily WHERE account_id=? AND followers IS NOT NULL ORDER BY day LIMIT 1", (acc["id"],))
    start = int(first["followers"]) if first else cur
    span = max(1, tgt - start)
    pct = max(0, min(100, round((cur - start) * 100.0 / span))) if tgt > start else (100 if cur >= tgt else 0)
    left_days = None
    if acc["goal_date"]:
        try:
            left_days = (datetime.strptime(acc["goal_date"], "%Y-%m-%d") - datetime.now()).days
        except ValueError:
            pass
    need_per_day = round((tgt - cur) / left_days, 1) if left_days and left_days > 0 and tgt > cur else None
    return {"target": tgt, "current": cur, "start": start, "pct": pct, "left_days": left_days, "need_per_day": need_per_day}
