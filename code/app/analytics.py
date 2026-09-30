"""Tahlil: eng yaxshi vaqt, guruh ballari, guruhdan chiqish nomzodlari."""
from datetime import datetime, timedelta

from . import db

WD = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]


def _since(days):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def best_time(aid: int, days: int = 60) -> dict:
    """Yuborilgan postlar ko'rishlarini hafta kuni va soat bo'yicha guruhlaydi."""
    rows = db.q("SELECT jt.sent_at, jt.views FROM job_targets jt JOIN jobs j ON j.id=jt.job_id "
                "WHERE j.account_id=? AND jt.status='sent' AND jt.views IS NOT NULL AND jt.deleted=0 AND jt.sent_at>=?",
                (aid, _since(days)))
    cell: dict[tuple, list] = {}
    for r in rows:
        try:
            dt = datetime.strptime(r["sent_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        cell.setdefault((dt.weekday(), dt.hour), []).append(r["views"])
    grid = [[{"n": 0, "avg": 0} for _ in range(24)] for _ in range(7)]
    for (w, h), v in cell.items():
        grid[w][h] = {"n": len(v), "avg": round(sum(v) / len(v), 1)}
    mx = max((c["avg"] for row in grid for c in row), default=0)
    ranked = sorted(((w, h, c) for w, row in enumerate(grid) for h, c in enumerate(row) if c["n"]),
                    key=lambda x: (-(x[2]["n"] >= 2), -x[2]["avg"]))[:5]
    top = []
    now = datetime.now()
    for w, h, c in ranked:
        d = now.replace(hour=h, minute=0, second=0, microsecond=0) + timedelta(days=(w - now.weekday()) % 7)
        if d <= now:
            d += timedelta(days=7)
        top.append({"weekday": WD[w], "hour": h, "avg": c["avg"], "n": c["n"], "when": d.strftime("%Y-%m-%dT%H:%M")})
    hours = []
    for h in range(24):
        vals = [c for row in grid for c in [row[h]] if c["n"]]
        tot = sum(c["n"] for c in vals)
        hours.append({"h": h, "n": tot, "avg": round(sum(c["avg"] * c["n"] for c in vals) / tot, 1) if tot else 0})
    return {"grid": grid, "max": mx, "top": top, "hours": hours, "samples": len(rows), "wd": WD}


def group_scores(aid: int, days: int = 60) -> list[dict]:
    """Har bir guruh uchun ko'rishlar, a'zolar va 0-100 ball."""
    since = _since(days)
    rows = db.q("SELECT jt.tg_id, MAX(jt.title) title, SUM(jt.status='sent') sent, SUM(jt.status='failed') failed, "
                "COALESCE(SUM(jt.deleted=1),0) deleted, AVG(CASE WHEN jt.status='sent' AND jt.views IS NOT NULL AND jt.deleted=0 THEN jt.views END) avg_views, "
                "MAX(jt.views) max_views, COALESCE(SUM(jt.reactions),0) reactions, COALESCE(SUM(jt.replies),0) replies, "
                "SUM(CASE WHEN jt.views IS NOT NULL AND jt.deleted=0 AND jt.status='sent' THEN 1 ELSE 0 END) measured "
                "FROM job_targets jt JOIN jobs j ON j.id=jt.job_id WHERE j.account_id=? AND jt.sent_at>=? GROUP BY jt.tg_id",
                (aid, since))
    stat = {r["tg_id"]: dict(r) for r in rows}
    out = []
    for g in db.q("SELECT * FROM groups WHERE account_id=? ORDER BY title COLLATE NOCASE", (aid,)):
        s = stat.get(g["tg_id"], {})
        sent, failed, deleted = s.get("sent") or 0, s.get("failed") or 0, s.get("deleted") or 0
        avg = s.get("avg_views")
        members = g["members"]
        log = db.q("SELECT day, members FROM group_members_log WHERE account_id=? AND tg_id=? ORDER BY day", (aid, g["tg_id"]))
        d7 = d30 = None
        if log and members:
            def delta(n):
                cut = (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")
                before = [x["members"] for x in log if x["day"] <= cut and x["members"] is not None]
                after = [x["members"] for x in log if x["day"] > cut and x["members"] is not None]
                old = before[-1] if before else (after[0] if after else None)
                return members - old if old is not None else None
            d7, d30 = delta(7), delta(30)
        rate = (avg / members * 100) if (avg is not None and members) else None
        score = None
        if s.get("measured"):
            view_score = min(100.0, (rate if rate is not None else min(avg, 50)) * 2)
            eng = ((s.get("reactions") or 0) + (s.get("replies") or 0)) / max(1, sent)
            score = 0.7 * view_score + 0.3 * min(100.0, eng * 20) - min(60, 25 * deleted + 10 * failed)
            score = max(0, min(100, round(score)))
        elif deleted or failed:
            score = max(0, 40 - 25 * deleted - 10 * failed)
        level = "none" if score is None else "good" if score >= 60 else "warn" if score >= 30 else "bad"
        out.append({"tg_id": g["tg_id"], "title": g["title"], "username": g["username"], "kind": g["kind"], "members": members,
                    "d7": d7, "d30": d30, "sent": sent, "failed": failed, "deleted": deleted,
                    "avg_views": round(avg, 1) if avg is not None else None, "max_views": s.get("max_views"),
                    "rate": round(rate, 1) if rate is not None else None, "score": score, "level": level,
                    "can_post": g["can_post"], "spark": [x["members"] for x in log[-30:] if x["members"] is not None]})
    return out


def group_detail(aid: int, tg_id: int) -> dict | None:
    g = db.one("SELECT * FROM groups WHERE account_id=? AND tg_id=?", (aid, tg_id))
    if not g:
        return None
    posts = db.q("SELECT jt.sent_at, jt.views, jt.reactions, jt.replies, jt.deleted, jt.status, j.name FROM job_targets jt "
                 "JOIN jobs j ON j.id=jt.job_id WHERE j.account_id=? AND jt.tg_id=? AND jt.status IN ('sent','failed') "
                 "ORDER BY jt.sent_at DESC LIMIT 60", (aid, tg_id))
    log = db.q("SELECT day, members FROM group_members_log WHERE account_id=? AND tg_id=? ORDER BY day", (aid, tg_id))
    return {"g": dict(g), "posts": [dict(p) for p in posts], "members": [dict(m) for m in log]}


# ---------------- guruhdan chiqish ----------------
def leave_settings(uid: int) -> dict:
    return {"on": db.uget(uid, "leave_on", "1") == "1", "bad": db.uget(uid, "leave_bad", "1") == "1",
            "low": db.uget(uid, "leave_low", "1") == "1", "min_posts": int(db.uget(uid, "leave_min_posts", 5)),
            "min_views": int(db.uget(uid, "leave_min_views", 20))}


def leave_candidates(aid: int, uid: int) -> list[dict]:
    cfg = leave_settings(uid)
    out = {}
    since = _since(45)
    if cfg["bad"]:
        for g in db.q("SELECT tg_id,title FROM groups WHERE account_id=? AND can_post=0", (aid,)):
            out[g["tg_id"]] = {"tg_id": g["tg_id"], "title": g["title"], "reason": "Yozish taqiqlangan", "kind": "bad"}
        for g in db.q("SELECT b.tg_id, b.title, b.reason FROM blacklist b JOIN groups g ON g.account_id=b.account_id AND g.tg_id=b.tg_id "
                      "WHERE b.account_id=? AND b.reason LIKE 'Avto%'", (aid,)):
            out.setdefault(g["tg_id"], {"tg_id": g["tg_id"], "title": g["title"], "reason": g["reason"], "kind": "bad"})
        for r in db.q("SELECT jt.tg_id, MAX(jt.title) title, SUM(jt.deleted=1) dele, SUM(jt.status='failed') fail "
                      "FROM job_targets jt JOIN jobs j ON j.id=jt.job_id WHERE j.account_id=? AND jt.sent_at>=? "
                      "GROUP BY jt.tg_id HAVING dele>=2 OR fail>=3", (aid, since)):
            if db.one("SELECT 1 FROM groups WHERE account_id=? AND tg_id=?", (aid, r["tg_id"])):
                why = f"Postlar {r['dele']} marta o'chirilgan" if (r["dele"] or 0) >= 2 else f"{r['fail']} marta yuborib bo'lmadi"
                out.setdefault(r["tg_id"], {"tg_id": r["tg_id"], "title": r["title"], "reason": why, "kind": "bad"})
    if cfg["low"]:
        for g in group_scores(aid, 60):
            if g["sent"] >= cfg["min_posts"] and g["avg_views"] is not None and g["avg_views"] < cfg["min_views"]:
                out.setdefault(g["tg_id"], {"tg_id": g["tg_id"], "title": g["title"], "kind": "low",
                                            "reason": f"O'rtacha ko'rish {g['avg_views']} ({g['sent']} ta postda)"})
    return list(out.values())
