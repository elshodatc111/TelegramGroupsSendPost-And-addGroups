"""Akkaunt xavfsizligi: ish vaqti oynasi, kunlik limit, salomatlik, qora ro'yxat."""
import json
from datetime import datetime, timedelta

from . import db

FMT = "%Y-%m-%d %H:%M:%S"


def event(account_id: int, kind: str, info: str | None = None):
    db.ex("INSERT INTO account_events(account_id,kind,ts,info) VALUES(?,?,?,?)", (account_id, kind, db.now(), info))


def _count(account_id, kind, hours):
    since = (datetime.now() - timedelta(hours=hours)).strftime(FMT)
    return db.one("SELECT COUNT(*) c FROM account_events WHERE account_id=? AND kind=? AND ts>=?",
                  (account_id, kind, since))["c"]


def health(account_id: int) -> dict:
    flood, err = _count(account_id, "flood", 24), _count(account_id, "error", 24)
    sent, joined = _count(account_id, "sent", 24), _count(account_id, "joined", 24)
    score = max(0, min(100, 100 - 20 * flood - 4 * min(err, 15)))
    if score >= 80:
        level, label = "good", "Yaxshi"
    elif score >= 50:
        level, label = "warn", "Ehtiyot bo'ling"
    else:
        level, label = "bad", "Xavfli"
    slow = 1.0 if flood == 0 else (1.5 if flood < 3 else 2.0)
    return {"score": score, "level": level, "label": label, "flood": flood, "errors": err,
            "sent": sent, "joined": joined, "slow": slow}


def slow_factor(account_id: int) -> float:
    return health(account_id)["slow"]


def _t(s):
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def work_gate(account_id: int):
    """Ish vaqti oynasidan tashqarida bo'lsa (davom etish vaqti, sabab) qaytaradi."""
    a = db.one("SELECT work_start, work_end FROM accounts WHERE id=?", (account_id,))
    if not a:
        return None
    s, e = _t(a["work_start"] or ""), _t(a["work_end"] or "")
    if s is None or e is None or s == e:
        return None
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    inside = (s <= cur < e) if s < e else (cur >= s or cur < e)
    if inside:
        return None
    start = now.replace(hour=s // 60, minute=s % 60, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    return start, f"Ish vaqti oynasidan tashqari ({a['work_start']}-{a['work_end']}). Oyna ochilganda davom etadi."


def daily_gate(account_id: int):
    """Kunlik yuborish limiti to'lgan bo'lsa (davom etish vaqti, sabab) qaytaradi."""
    a = db.one("SELECT daily_limit FROM accounts WHERE id=?", (account_id,))
    limit = (a["daily_limit"] if a else 0) or 0
    if limit <= 0:
        return None
    since = (datetime.now() - timedelta(hours=24)).strftime(FMT)
    rows = db.q("SELECT ts FROM account_events WHERE account_id=? AND kind='sent' AND ts>=? ORDER BY ts",
                (account_id, since))
    if len(rows) < limit:
        return None
    free = datetime.strptime(rows[len(rows) - limit]["ts"], FMT) + timedelta(hours=24, minutes=1)
    return free, f"Akkauntning kunlik yuborish limiti ({limit}) to'ldi. Avtomatik davom etadi."


# ---------------- isitish (warm-up) rejasi ----------------
DEFAULT_PLAN = [  # (kun, postlar, a'zo bo'lishlar)
    (1, 3, 2), (2, 5, 3), (3, 8, 5), (4, 12, 7), (5, 16, 9), (6, 20, 12), (7, 25, 15),
    (8, 30, 18), (9, 36, 21), (10, 42, 24), (11, 50, 27), (12, 58, 30), (13, 66, 34), (14, 75, 38)]


def default_plan() -> list[dict]:
    return [{"day": d, "posts": p, "joins": j} for d, p, j in DEFAULT_PLAN]


def get_warmup(account_id: int):
    r = db.one("SELECT * FROM warmup WHERE account_id=?", (account_id,))
    if not r:
        return None
    d = dict(r)
    d["plan"] = json.loads(r["plan_json"] or "[]") or default_plan()
    return d


def warm_today(account_id: int):
    """Faol isitish rejasi bo'yicha bugungi limitlar; reja tugagan yoki o'chiq bo'lsa None."""
    w = get_warmup(account_id)
    if not w or not w["active"] or not w["start_date"]:
        return None
    day = (datetime.now().date() - datetime.strptime(w["start_date"], "%Y-%m-%d").date()).days + 1
    if day < 1:
        return None
    if day > len(w["plan"]):
        db.ex("UPDATE warmup SET active=0 WHERE account_id=?", (account_id,))
        return None
    p = w["plan"][day - 1]
    return {"day": day, "of": len(w["plan"]), "posts": int(p["posts"]), "joins": int(p["joins"])}


def _midnight():
    return (datetime.now() + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)


def _today_start():
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).strftime(FMT)


def warm_post_gate(account_id: int):
    w = warm_today(account_id)
    if not w:
        return None
    n = db.one("SELECT COUNT(*) c FROM account_events WHERE account_id=? AND kind='sent' AND ts>=?",
               (account_id, _today_start()))["c"]
    if n >= w["posts"]:
        return _midnight(), f"Isitish rejasi: {w['day']}-kun post limiti ({w['posts']}) to'ldi. Ertaga davom etadi."
    return None


def warm_join_gate(account_id: int):
    w = warm_today(account_id)
    if not w:
        return None
    n = db.one("SELECT COUNT(*) c FROM join_targets jt JOIN join_batches jb ON jb.id=jt.batch_id "
               "WHERE jb.account_id=? AND jt.status IN ('joined','requested') AND jt.tried_at>=?",
               (account_id, _today_start()))["c"]
    if n >= w["joins"]:
        return _midnight(), f"Isitish rejasi: {w['day']}-kun a'zo bo'lish limiti ({w['joins']}) to'ldi. Ertaga davom etadi."
    return None


def gate(account_id: int):
    return work_gate(account_id) or warm_post_gate(account_id) or daily_gate(account_id)


# ---------------- qora ro'yxat ----------------
def blacklist_add(account_id: int, tg_id: int, title: str, reason: str):
    db.ex("INSERT INTO blacklist(account_id,tg_id,title,reason,created_at) VALUES(?,?,?,?,?) "
          "ON CONFLICT(account_id,tg_id) DO UPDATE SET reason=excluded.reason", (account_id, tg_id, title, reason, db.now()))


def blacklisted_ids(account_id: int) -> set[int]:
    return {r["tg_id"] for r in db.q("SELECT tg_id FROM blacklist WHERE account_id=?", (account_id,))}
