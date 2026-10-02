"""Instagram: ma'lumot yig'ish (profil, kunlik ko'rsatkichlar, postlar, auditoriya) va token yangilash."""
import asyncio
import json
from datetime import datetime, timedelta

from . import db, ig_api, ig_data, notify
from .config import log

DAY_METRICS = ["views", "likes", "comments", "shares", "saves", "total_interactions", "profile_views"]


def _upsert_day(aid, day, **kw):
    db.ex("INSERT OR IGNORE INTO ig_daily(account_id,day) VALUES(?,?)", (aid, day))
    sets = ", ".join(f"{k}=?" for k in kw)
    if sets:
        db.ex(f"UPDATE ig_daily SET {sets} WHERE account_id=? AND day=?", tuple(kw.values()) + (aid, day))


def _fail(acc, e):
    msg = f"{type(e).__name__}: {e}"[:400]
    dead = isinstance(e, ig_api.IGError) and e.token_dead
    db.ex("UPDATE ig_accounts SET last_error=?, status=? WHERE id=?", (msg, "token" if dead else "error", acc["id"]))
    ig_data.log_event(acc["id"], "error", msg)
    if dead:
        notify.event("error", "Instagram", f"@{acc['username']}: token yaroqsiz. Akkauntni qayta ulang. ({e})")
        notify.toast("Instagram", f"@{acc['username']}: token yaroqsiz, qayta ulang")
    else:
        notify.event("warn", "Instagram", f"@{acc['username']}: {msg}")


async def refresh_token_if_needed(acc, force=False) -> bool:
    days = ig_data.token_days(acc)
    if not force and (days is None or days > 10):
        return False
    try:
        age_ok = True
        if acc["token_at"]:
            age_ok = (datetime.now() - datetime.strptime(acc["token_at"], "%Y-%m-%d %H:%M:%S")) > timedelta(hours=24)
        if not age_ok and not force:
            return False
        js = await ig_api.refresh(ig_data.token(acc))
        db.ex("UPDATE ig_accounts SET token=?, token_expires=?, token_at=?, status='active', last_error=NULL WHERE id=?",
              (ig_data.secure.enc(js["access_token"]), ig_api.expires_at(js), db.now(), acc["id"]))
        ig_data.log_event(acc["id"], "token", "Token yangilandi")
        return True
    except Exception as e:
        _fail(acc, e)
        return False


async def sync_account(aid: int, demographics: bool = False) -> dict:
    acc = ig_data.get(aid)
    if not acc or not acc["token"]:
        return {"ok": False, "error": "Akkaunt yoki token yo'q"}
    tok = ig_data.token(acc)
    res = {"ok": True, "media": 0, "insights": 0}
    try:
        await refresh_token_if_needed(acc)
        acc = ig_data.get(aid)
        tok = ig_data.token(acc)
        prof = await ig_api.me(tok)
        today = datetime.now().strftime("%Y-%m-%d")
        db.ex("UPDATE ig_accounts SET username=?,name=?,account_type=?,bio=?,website=?,followers=?,follows=?,media_count=?,picture=?,"
              "synced_at=?,status='active',last_error=NULL WHERE id=?",
              (prof.get("username"), prof.get("name"), prof.get("account_type"), prof.get("biography"), prof.get("website"),
               int(prof.get("followers_count") or 0), int(prof.get("follows_count") or 0), int(prof.get("media_count") or 0),
               prof.get("profile_picture_url"), db.now(), aid))
        _upsert_day(aid, today, followers=int(prof.get("followers_count") or 0), follows=int(prof.get("follows_count") or 0),
                    media_count=int(prof.get("media_count") or 0))
        uid = acc["ig_user_id"]
        # kunlik qatorlar (oxirgi ~30 kun): qamrov va obunachi o'zgarishi
        try:
            for d, v in await ig_api.series(tok, uid, "reach"):
                _upsert_day(aid, d, reach=v)
        except ig_api.IGError as e:
            if e.token_dead:
                raise
            ig_data.log_event(aid, "warn", f"reach qatori: {e}")
        try:
            for d, v in await ig_api.series(tok, uid, "follower_count"):
                _upsert_day(aid, d, new_followers=v)
        except ig_api.IGError as e:
            if e.token_dead:
                raise
        # kecha uchun umumiy ko'rsatkichlar
        y0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        vals = await ig_api.total_value(tok, uid, DAY_METRICS, y0, y0 + timedelta(days=1))
        if vals:
            _upsert_day(aid, y0.strftime("%Y-%m-%d"), views=vals.get("views"), likes=vals.get("likes"), comments=vals.get("comments"),
                        shares=vals.get("shares"), saves=vals.get("saves"), interactions=vals.get("total_interactions"),
                        profile_views=vals.get("profile_views"))
        # postlar
        items = await ig_api.media_list(tok, uid, 50)
        now = db.now()
        for m in items:
            ts = (m.get("timestamp") or "")[:19].replace("T", " ")
            db.ex("INSERT OR IGNORE INTO ig_media(account_id,ig_id) VALUES(?,?)", (aid, m["id"]))
            db.ex("UPDATE ig_media SET permalink=?,caption=?,mtype=?,product=?,ts=?,likes=?,comments=?,thumb=?,fetched_at=? "
                  "WHERE account_id=? AND ig_id=?",
                  (m.get("permalink"), m.get("caption") or "", m.get("media_type"), m.get("media_product_type"), ts,
                   int(m.get("like_count") or 0), int(m.get("comments_count") or 0), m.get("thumbnail_url") or m.get("media_url"),
                   now, aid, m["id"]))
            res["media"] += 1
        # postlar insights: yangi (30 kun) yoki 12 soatdan eski
        cut30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        cut12 = (datetime.now() - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
        todo = db.q("SELECT * FROM ig_media WHERE account_id=? AND ts>=? AND (insights_at IS NULL OR insights_at<?) "
                    "ORDER BY ts DESC LIMIT 30", (aid, cut30, cut12))
        old = db.q("SELECT * FROM ig_media WHERE account_id=? AND insights_at IS NULL AND ts<? ORDER BY ts DESC LIMIT 10", (aid, cut30))
        for m in list(todo) + list(old):
            try:
                ins = await ig_api.media_insights(tok, m["ig_id"], m["product"] or "")
            except ig_api.IGError as e:
                if e.token_dead:
                    raise
                db.ex("UPDATE ig_media SET insights_at=? WHERE id=?", (now, m["id"]))
                continue
            db.ex("UPDATE ig_media SET reach=?,views=?,saved=?,shares=?,interactions=?,watch_ms=?,insights_at=? WHERE id=?",
                  (ins.get("reach", 0), ins.get("views", 0), ins.get("saved", 0), ins.get("shares", 0), ins.get("total_interactions", 0),
                   ins.get("ig_reels_avg_watch_time", 0), now, m["id"]))
            res["insights"] += 1
        # rejadagi postni haqiqiy postga bog'lash (matn bo'yicha)
        _link_plan(aid)
        # auditoriya (100+ obunachi, haftada bir marta)
        if demographics or (int(prof.get("followers_count") or 0) >= 100 and _demo_due(acc)):
            await sync_demographics(aid, tok, uid)
    except Exception as e:
        _fail(acc, e)
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return res


def _demo_due(acc) -> bool:
    if not acc["demo_at"]:
        return True
    try:
        return datetime.now() - datetime.strptime(acc["demo_at"], "%Y-%m-%d %H:%M:%S") > timedelta(days=7)
    except ValueError:
        return True


async def sync_demographics(aid, tok, uid):
    ok = 0
    for br in ("age", "gender", "city", "country"):
        try:
            data = await ig_api.demographics(tok, uid, br)
        except ig_api.IGError as e:
            if e.token_dead:
                raise
            ig_data.log_event(aid, "warn", f"auditoriya ({br}): {e}")
            continue
        db.ex("INSERT OR REPLACE INTO ig_demo(account_id,kind,data,updated_at) VALUES(?,?,?,?)",
              (aid, br, json.dumps(data, ensure_ascii=False), db.now()))
        ok += 1
    db.ex("UPDATE ig_accounts SET demo_at=? WHERE id=?", (db.now(), aid))
    return ok


def demo(aid) -> dict:
    out = {}
    for r in db.q("SELECT * FROM ig_demo WHERE account_id=?", (aid,)):
        try:
            out[r["kind"]] = json.loads(r["data"] or "[]")
        except Exception:
            out[r["kind"]] = []
    return out


def _link_plan(aid):
    """Rejadagi 'published' post havolasi bo'yicha ig_media bilan bog'laydi."""
    for p in db.q("SELECT id, ig_media_id FROM ig_plan WHERE account_id=? AND ig_media_id IS NOT NULL", (aid,)):
        db.ex("UPDATE ig_media SET plan_id=? WHERE account_id=? AND ig_id=? AND plan_id IS NULL", (p["id"], aid, p["ig_media_id"]))


async def loop():
    await asyncio.sleep(90)
    while True:
        try:
            for acc in ig_data.accounts():
                if acc["token"] and acc["status"] != "token":
                    await sync_account(acc["id"])
                    await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ig_collect.loop")
        try:
            hours = max(1, min(48, int(db.get_setting("ig_sync_hours", 3) or 3)))
        except ValueError:
            hours = 3
        await asyncio.sleep(hours * 3600)
