"""Akkaunt xavfsizligi: ish vaqti oynasi, kunlik limit, salomatlik, qora ro'yxat."""
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


def gate(account_id: int):
    return work_gate(account_id) or daily_gate(account_id)


# ---------------- qora ro'yxat ----------------
def blacklist_add(account_id: int, tg_id: int, title: str, reason: str):
    db.ex("INSERT INTO blacklist(account_id,tg_id,title,reason,created_at) VALUES(?,?,?,?,?) "
          "ON CONFLICT(account_id,tg_id) DO UPDATE SET reason=excluded.reason", (account_id, tg_id, title, reason, db.now()))


def blacklisted_ids(account_id: int) -> set[int]:
    return {r["tg_id"] for r in db.q("SELECT tg_id FROM blacklist WHERE account_id=?", (account_id,))}
