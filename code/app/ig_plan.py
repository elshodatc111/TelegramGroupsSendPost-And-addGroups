"""Instagram: rejalashtirilgan postlar — eslatma yoki avto joylash, vaqt taklifi."""
import asyncio
from datetime import datetime, timedelta

from . import cf, db, ig_api, ig_data, ig_stats, ig_tunnel, notify
from .config import log

FMT = "%Y-%m-%d %H:%M:%S"
_busy: set[int] = set()
DAYS_UZ = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]


def effective_mode(it) -> str:
    return it["mode"] if it["mode"] in ("auto", "reminder") else ig_data.publish_mode()


# ---------------------------------------------------------------- vaqt taklifi
def busy_slots(aid, start, end, exclude_id=None):
    rows = db.q("SELECT id,title,scheduled_at,status FROM ig_plan WHERE account_id=? AND status IN ('scheduled','reminded','publishing') "
                "AND scheduled_at>=? AND scheduled_at<=? ORDER BY scheduled_at", (aid, start.strftime(FMT), end.strftime(FMT)))
    out = []
    for r in rows:
        if exclude_id and r["id"] == exclude_id:
            continue
        try:
            out.append({"id": r["id"], "title": r["title"], "status": r["status"], "t": datetime.strptime(r["scheduled_at"], FMT)})
        except (TypeError, ValueError):
            pass
    return out


def suggest_times(aid, exclude_id=None, days=7, n=3, min_gap_h=4) -> dict:
    """Eng yaxshi 3 vaqt: oldingi postlar qamrovi (hafta kuni+soat) va rejadagi postlar hisobga olinadi."""
    scores = ig_stats.hour_scores(aid)
    now = datetime.now()
    busy = busy_slots(aid, now, now + timedelta(days=days + 1), exclude_id)
    cap = ig_data.max_per_day()
    data = bool(scores)
    default_hours = {12: 3, 13: 2, 19: 5, 20: 5, 21: 4, 18: 3}
    cands = []
    for d in range(0, days + 1):
        day = (now + timedelta(days=d)).replace(minute=0, second=0, microsecond=0)
        same = [b for b in busy if b["t"].date() == day.date()]
        if len(same) >= cap:
            continue
        for h in range(7, 23):
            t = day.replace(hour=h)
            if t <= now + timedelta(minutes=30):
                continue
            if any(abs((t - b["t"]).total_seconds()) < min_gap_h * 3600 for b in busy):
                continue
            base = scores.get((t.weekday(), h), 0) if data else default_hours.get(h, 0.3)
            if base <= 0:
                nb = [scores.get((t.weekday(), hh), 0) for hh in (h - 1, h + 1)]
                base = max(nb) * 0.5
            cands.append((base * (0.97 ** d), t))
    cands.sort(key=lambda x: -x[0])
    picked = []
    for sc, t in cands:
        if any(abs((t - p[1]).total_seconds()) < min_gap_h * 3600 for p in picked):
            continue
        picked.append((sc, t))
        if len(picked) >= n:
            break
    return {"times": [{"at": t.strftime("%Y-%m-%dT%H:%M"), "label": f"{DAYS_UZ[t.weekday()]}, {t.strftime('%d.%m %H:%M')}",
                       "load": sum(1 for b in busy if b["t"].date() == t.date())} for _, t in sorted(picked, key=lambda x: x[1])],
            "data": data, "cap": cap}


# ---------------------------------------------------------------- joylash
async def publish_item(item_id: int) -> bool:
    it = db.one("SELECT * FROM ig_plan WHERE id=?", (item_id,))
    if not it or it["status"] in ("published", "cancelled") or item_id in _busy:
        return False
    acc = ig_data.get(it["account_id"])
    if not acc or not acc["token"]:
        db.ex("UPDATE ig_plan SET status='failed', error=? WHERE id=?", ("Akkaunt yoki token yo'q", item_id))
        return False
    names = ig_data.media_list(it)
    if not names:
        db.ex("UPDATE ig_plan SET status='failed', error=? WHERE id=?", ("Media fayl yo'q: Instagram'ga matn bilan yolg'iz post chiqmaydi", item_id))
        return False
    _busy.add(item_id)
    opened = False
    db.ex("UPDATE ig_plan SET status='publishing', error=NULL WHERE id=?", (item_id,))
    try:
        items = ig_data.media_items(names)
        if any(x["missing"] for x in items):
            raise RuntimeError("Bulutdagi fayl o'chirilgan: postga boshqa fayl tanlang")
        base = None
        if any(not x["cloud"] for x in items):
            base = await ig_tunnel.open_tunnel()
            opened = True
        urls = [x["u"] if x["cloud"] else ig_tunnel.public_url(base, x["name"], ig_tunnel.grant(x["name"])) for x in items]
        res = await ig_api.publish(ig_data.token(acc), acc["ig_user_id"], it["mtype"] or "IMAGE", urls, it["caption"] or "")
        db.ex("UPDATE ig_plan SET status='published', ig_media_id=?, permalink=?, sent_at=?, error=NULL WHERE id=?",
              (res["id"], res.get("permalink"), db.now(), item_id))
        ig_data.log_event(acc["id"], "post", f"reja #{item_id} joylandi: {res.get('permalink')}")
        notify.toast("Instagram", f"@{acc['username']}: post joylandi")
        try:
            await cf.after_post(item_id)
        except Exception:
            log.warning("cf.after_post", exc_info=True)
        return True
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:480]
        log.warning("Instagram joylash xato (#%s): %s", item_id, msg, exc_info=True)
        db.ex("UPDATE ig_plan SET status='failed', error=? WHERE id=?", (msg, item_id))
        ig_data.log_event(acc["id"], "error", f"reja #{item_id}: {msg}")
        notify.event("error", "Instagram", f"@{acc['username']} postni joylab bo'lmadi: {msg}")
        notify.toast("Instagram", f"@{acc['username']}: joylab bo'lmadi, qo'lda joylang")
        return False
    finally:
        _busy.discard(item_id)
        if opened:
            await ig_tunnel.close_tunnel()


def mark_done(item_id: int, permalink: str = ""):
    db.ex("UPDATE ig_plan SET status='published', sent_at=?, permalink=?, error=NULL, mode='reminder' WHERE id=?",
          (db.now(), permalink or None, item_id))


async def tick():
    now = datetime.now()
    grace = max(1, int(db.get_setting("ig_plan_grace_h", 6) or 6))
    rem = ig_data.remind_min()
    for it in db.q("SELECT * FROM ig_plan WHERE status IN ('scheduled','reminded') ORDER BY scheduled_at LIMIT 50"):
        try:
            when = datetime.strptime(it["scheduled_at"], FMT)
        except (TypeError, ValueError):
            continue
        acc = ig_data.get(it["account_id"])
        uname = acc["username"] if acc else "?"
        mode = effective_mode(it)
        if mode == "reminder":
            if now >= when - timedelta(minutes=rem) and not it["reminded"]:
                db.ex("UPDATE ig_plan SET reminded=1, status='reminded' WHERE id=?", (it["id"],))
                notify.toast("Instagram: post vaqti", f"@{uname}: {it['title'][:60]} — {when.strftime('%H:%M')} da joylang")
                ig_data.log_event(it["account_id"], "remind", f"reja #{it['id']} eslatma")
            if now - when > timedelta(hours=grace):
                db.ex("UPDATE ig_plan SET status='missed', error=? WHERE id=?", ("Belgilanmadi: vaqti o'tib ketdi", it["id"]))
        else:
            if now >= when:
                if now - when > timedelta(hours=grace):
                    db.ex("UPDATE ig_plan SET status='missed', error=? WHERE id=?",
                          ("Vaqtida joylanmadi (dastur o'chiq edi)", it["id"]))
                else:
                    await publish_item(it["id"])


async def loop():
    await asyncio.sleep(75)
    while True:
        try:
            if ig_data.accounts():
                await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ig_plan.loop")
        await asyncio.sleep(30)
