"""Top 50: mahalla, maktab, oliy ta'lim, texnikum va o'quv markazlari guruhlarini kalit so'zsiz avtomatik topish.

Foydalanuvchi so'z kiritmaydi: dastur o'zi tayyor so'rovlar bilan qidiradi, faqat reklama yuborish mumkin bo'lgan (kanal emas,
a'zolar yoza oladi, reklama taqiqlanmagan, a'zolar soni yetarli, taqiqlangan mavzu emas), siz hali a'zo bo'lmagan guruhlarni
a'zolar soni bo'yicha tartiblab beradi."""
import asyncio
import json
import random
from datetime import datetime

from . import db
from .config import log
from .core import joiner, manager

CATEGORIES = {
    "Mahalla": ["mahalla", "mahalla guruhi", "mahalla chat", "mahalla yoshlari", "mahalla fuqarolar yig'ini", "махалля",
                "Toshkent mahalla", "Samarqand mahalla", "Farg'ona mahalla", "Andijon mahalla", "Namangan mahalla",
                "Buxoro mahalla", "Xorazm mahalla", "Qashqadaryo mahalla", "Surxondaryo mahalla", "Jizzax mahalla",
                "Sirdaryo mahalla", "Navoiy mahalla", "Qoraqalpog'iston mahalla"],
    "Maktab": ["maktab", "maktab ota-onalar", "maktab o'qituvchilari", "umumiy o'rta ta'lim maktabi", "maktab sinf ota-onalar",
               "ixtisoslashtirilgan maktab", "prezident maktabi", "maktab o'quvchilari", "школа узбекистан", "maktab direktor",
               "1-maktab", "2-maktab", "3-maktab", "5-maktab", "10-maktab"],
    "Oliy ta'lim": ["universitet", "oliy ta'lim", "talabalar", "institut", "universitet talabalari", "abituriyent", "OTM",
                    "davlat universiteti", "magistratura", "talabalar chat", "университет узбекистан", "talabalar hayoti",
                    "universitet guruhi", "bakalavr", "tatu", "tdyu", "samdu", "tdtu"],
    "Texnikum / kollej": ["texnikum", "kollej", "kasb-hunar kollejlari", "politexnikum", "texnikum talabalari", "kollej guruhi",
                          "техникум", "pedagogika kolleji", "tibbiyot texnikumi", "iqtisodiyot kolleji", "texnikum abituriyent"],
    "O'quv markaz": ["o'quv markaz", "o'quv markazi", "ta'lim markazi", "kurslar", "til kurslari", "IT o'quv markaz",
                     "ingliz tili kursi", "matematika kursi", "repetitor", "o'quv markazi ota-onalar", "учебный центр ташкент",
                     "abituriyent kurs", "rus tili kursi", "koreys tili kursi", "dasturlash kursi", "online kurs uz",
                     "kasb-hunar o'quv markazi", "bilim markazi"],
}
progress: dict[int, dict] = {}


def _meta(aid: int) -> dict:
    r = db.one("SELECT * FROM top_meta WHERE account_id=?", (aid,))
    if not r:
        db.ex("INSERT INTO top_meta(account_id,ptr_json,runs) VALUES(?,?,0)", (aid, "{}"))
        r = db.one("SELECT * FROM top_meta WHERE account_id=?", (aid,))
    try:
        ptr = json.loads(r["ptr_json"] or "{}")
    except Exception:
        ptr = {}
    return {"ptr": ptr, "last_run": r["last_run"], "runs": r["runs"] or 0}


def evaluate(info: dict, min_members: int, cfg: dict) -> tuple[str, str]:
    """('ok'|'rejected', sabab)"""
    from .discovery import _hits, uz_share
    if info.get("skip"):
        return "rejected", info["skip"]
    if info.get("joined"):
        return "rejected", "Allaqachon a'zo"
    if info.get("scam") or info.get("restricted"):
        return "rejected", "Telegram cheklagan guruh"
    if not info.get("can_send", True):
        return "rejected", "A'zolar xabar yoza olmaydi"
    if info.get("ads_flag"):
        return "rejected", "Reklama taqiqlangan"
    m = info.get("members") or 0
    if m < min_members:
        return "rejected", f"A'zolar kam ({m} < {min_members})"
    everything = " ".join([info.get("title") or "", info.get("about") or "", info.get("pinned") or "", " ".join(info.get("texts") or [])]).lower()
    for cat, words in cfg["bans"].items():
        h = _hits(everything, words)
        if h:
            return "rejected", f"Taqiqlangan mavzu ({cat}): '{h[0].rstrip('$')}'"
    h = _hits(everything, cfg["block_extra"])
    if h:
        return "rejected", f"Sizning taqiq so'zingiz: '{h[0]}'"
    uz, wc = uz_share((info.get("texts") or []) + [info.get("title") or "", info.get("about") or ""])
    if wc >= 25 and uz < 25:
        return "rejected", f"O'zbek tili ulushi past ({uz}%)"
    return "ok", ""


def refresh_status(aid: int):
    """Allaqachon a'zo bo'lingan / navbatdagi guruhlar holatini yangilaydi."""
    db.ex("UPDATE top_groups SET status='joined' WHERE account_id=? AND status IN ('ok','queued') AND lower(username) IN "
          "(SELECT lower(username) FROM groups WHERE account_id=? AND username IS NOT NULL)", (aid, aid))
    for c in db.q("SELECT * FROM top_groups WHERE account_id=? AND status='queued'", (aid,)):
        t = db.one("SELECT status, detail FROM join_targets WHERE batch_id=? AND lower(key)=?", (c["batch_id"], c["username"]))
        if not t or t["status"] == "pending":
            continue
        if t["status"] in ("joined", "already", "requested"):
            db.ex("UPDATE top_groups SET status='joined' WHERE id=?", (c["id"],))
        else:
            db.ex("UPDATE top_groups SET status='rejected', reason=? WHERE id=?", (t["detail"] or t["status"], c["id"]))


async def search(aid: int, per_cat: int = 3, max_inspect: int = 45) -> int:
    """Har kategoriyadan navbatdagi so'rovlarni ishga tushiradi va yangi mos guruhlarni bazaga qo'shadi."""
    from .auditor import get_cfg as acfg
    from .discovery import get_cfg as dcfg
    from .limits import blacklisted_ids
    svc = manager.get(aid)
    min_m = acfg(aid)["min_members"]
    cfg = dcfg(aid)
    meta = _meta(aid)
    ptr = meta["ptr"]
    queries = []
    for cat, qs in CATEGORIES.items():
        p = ptr.get(cat, 0) % len(qs)
        for i in range(per_cat):
            queries.append((cat, qs[(p + i) % len(qs)]))
        ptr[cat] = (p + per_cat) % len(qs)
    prog = progress[aid] = {"running": True, "done": 0, "total": len(queries), "new": 0, "msg": "Qidirilmoqda"}
    joined = {r["username"].lower() for r in db.q("SELECT username FROM groups WHERE account_id=? AND username IS NOT NULL", (aid,))}
    black = blacklisted_ids(aid)
    inspected = 0
    try:
        for i, (cat, q) in enumerate(queries):
            prog["msg"] = f"Qidirilmoqda: {q}"
            try:
                res = await svc.search_public(q, strict=True, min_members=min_m, check_ads=False, stats={})
            except Exception as e:
                log.info("Top50 qidiruv xatosi %s: %s", q, type(e).__name__)
                if type(e).__name__ == "FloodWaitError":
                    prog["msg"] = "Telegram kutishni so'radi, keyinroq urinib ko'ring"
                    break
                res = []
            for r in res:
                un = (r.get("username") or "").lower()
                if not un or un in joined or r.get("joined") or r.get("kind") != "group":
                    continue
                if db.one("SELECT 1 FROM top_groups WHERE account_id=? AND username=?", (aid, un)):
                    continue
                if inspected >= max_inspect:
                    break
                await asyncio.sleep(random.uniform(1.5, 3))
                inspected += 1
                try:
                    info = await svc.inspect_public(un)
                except Exception as e:
                    if type(e).__name__ == "FloodWaitError":
                        prog["msg"] = "Telegram kutishni so'radi, keyinroq urinib ko'ring"
                        raise
                    continue
                if info.get("tg_id") in black:
                    st, why = "rejected", "Qora ro'yxatda"
                else:
                    st, why = evaluate(info, min_m, cfg)
                from .discovery import uz_share
                uz, _ = uz_share((info.get("texts") or []) + [info.get("title") or "", info.get("about") or ""])
                db.ex("INSERT OR IGNORE INTO top_groups(account_id,username,title,about,members,per_day,uz,category,status,reason,found_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (aid, un, info.get("title") or r.get("title"), (info.get("about") or "")[:300],
                                                        info.get("members") or r.get("members"), info.get("per_day"), uz, cat, st, why, db.now()))
                if st == "ok":
                    prog["new"] += 1
            prog["done"] = i + 1
            await asyncio.sleep(random.uniform(5, 9))
    except Exception as e:
        if not str(prog.get("msg", "")).startswith("Telegram"):
            prog["msg"] = f"Xato: {type(e).__name__}"
    finally:
        db.ex("UPDATE top_meta SET ptr_json=?, last_run=?, runs=runs+1 WHERE account_id=?", (json.dumps(ptr), db.now(), aid))
        if prog.get("msg", "").startswith(("Qidirilmoqda",)):
            prog["msg"] = "Tugadi"
        prog["running"] = False
        refresh_status(aid)
    return prog["new"]


def top(aid: int, category: str = "", limit: int = 50) -> list:
    refresh_status(aid)
    sql = "SELECT * FROM top_groups WHERE account_id=? AND status IN ('ok','queued')"
    args = [aid]
    if category:
        sql += " AND category=?"
        args.append(category)
    sql += " ORDER BY members DESC, per_day DESC LIMIT ?"
    args.append(limit)
    return db.q(sql, args)


def enqueue(aid: int, usernames: list[str], start: bool = True) -> int | None:
    """Tanlangan guruhlarni a'zo bo'lish navbatiga qo'shadi (standart interval bilan, yoki sozlash uchun draft)."""
    from .discovery import get_cfg as dcfg
    names = [u.lower() for u in usernames]
    rows = [r for r in db.q(f"SELECT * FROM top_groups WHERE account_id=? AND status='ok' AND username IN ({','.join('?' * len(names))})",
                            [aid, *names])] if names else []
    if not rows:
        return None
    cfg = dcfg(aid)
    if start:
        bid = db.ex("INSERT INTO join_batches(account_id,filename,status,created_at,total,min_delay,max_delay,daily_limit) VALUES(?,?,?,?,?,?,?,?)",
                    (aid, f"Top50 {datetime.now():%Y-%m-%d %H:%M}", "queued", db.now(), len(rows), cfg["min_delay"], cfg["max_delay"],
                     max(cfg["daily_target"], 1)))
    else:
        cols = [{"label": "Top 50", "items": [{"kind": "username", "key": r["username"], "ref": "@" + r["username"]} for r in rows]}]
        bid = db.ex("INSERT INTO join_batches(account_id,filename,status,created_at,raw) VALUES(?,?,?,?,?)",
                    (aid, "Top50", "draft", db.now(), json.dumps(cols, ensure_ascii=False)))
    if start:
        db.many("INSERT INTO join_targets(batch_id,ref,kind,key) VALUES(?,?,?,?)", [(bid, "@" + r["username"], "username", r["username"]) for r in rows])
    db.many("UPDATE top_groups SET status='queued', batch_id=? WHERE id=?", [(bid, r["id"]) for r in rows])
    if start:
        joiner.start(bid)
    return bid
