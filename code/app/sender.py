"""Postlarni navbat bilan yuboruvchi fon xizmati (har akkaunt uchun alohida navbat)."""
import asyncio
import json
import random
from datetime import datetime, timedelta

from telethon import errors

from . import db, jobsvc
from .config import log
from .limits import blacklist_add, event, gate, slow_factor
from .textutil import render_variant

FMT = "%Y-%m-%d %H:%M:%S"

# Xato nomi -> (izoh, qora ro'yxatga qo'shilsinmi)
FRIENDLY = {
    "ChatWriteForbiddenError": ("Bu guruhda yozish taqiqlangan", True),
    "UserBannedInChannelError": ("Siz bu guruhdan bloklangansiz", True),
    "ChatAdminRequiredError": ("Faqat adminlar yoza oladi", True),
    "ChannelPrivateError": ("Guruh yopiq yoki siz a'zo emassiz", True),
    "UserNotParticipantError": ("Siz bu guruhga a'zo emassiz", True),
    "ChatRestrictedError": ("Guruhda cheklov qo'yilgan", True),
    "MediaCaptionTooLongError": ("Matn juda uzun", False),
    "ChatSendMediaForbiddenError": ("Guruhda media yuborish taqiqlangan", False),
    "ChatSendPhotosForbiddenError": ("Guruhda rasm yuborish taqiqlangan", False),
    "ChatSendVideosForbiddenError": ("Guruhda video yuborish taqiqlangan", False),
    "ChatSendPlainForbiddenError": ("Guruhda matn yuborish taqiqlangan", True),
    "PeerIdInvalidError": ("Guruh topilmadi (o'chirilgan bo'lishi mumkin)", True),
    "ChannelInvalidError": ("Guruh topilmadi", True),
}


class Sender:
    def __init__(self, manager):
        self.mgr = manager
        self.tasks: dict[int, asyncio.Task] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self._sched_task = None

    # ---------- boshqaruv ----------
    def _lock(self, aid) -> asyncio.Lock:
        return self.locks.setdefault(aid, asyncio.Lock())

    def start(self, job_id: int):
        t = self.tasks.get(job_id)
        if t and not t.done():
            return
        self.tasks[job_id] = asyncio.create_task(self._run(job_id))

    def begin_scheduler(self):
        self._sched_task = asyncio.create_task(self._scheduler())

    async def stop(self):
        if self._sched_task:
            self._sched_task.cancel()
        for t in self.tasks.values():
            t.cancel()

    async def _scheduler(self):
        tick = 0
        while True:
            try:
                for r in db.q("SELECT id FROM jobs WHERE status='scheduled' AND scheduled_at<=?", (db.now(),)):
                    db.ex("UPDATE jobs SET status='queued' WHERE id=?", (r["id"],))
                    self.start(r["id"])
                for r in db.q("SELECT id FROM jobs WHERE status='waiting' AND resume_at<=?", (db.now(),)):
                    db.ex("UPDATE jobs SET status='queued', error=NULL WHERE id=?", (r["id"],))
                    self.start(r["id"])
                if tick % 6 == 0:
                    await self._run_recurring()
            except Exception:
                log.exception("Sender scheduler")
            tick += 1
            await asyncio.sleep(5)

    async def _run_recurring(self):
        """Takrorlanuvchi kampaniyalarni vaqti kelganda ishga tushiradi."""
        for c in db.q("SELECT * FROM campaigns WHERE rec_active=1 AND next_run IS NOT NULL AND next_run<=?", (db.now(),)):
            nxt = jobsvc.next_run(c["recurrence"], c["rec_time"], c["rec_days"])
            db.ex("UPDATE campaigns SET next_run=? WHERE id=?", (nxt, c["id"]))
            st = await self.mgr.get(c["account_id"]).status()
            if not st.get("authorized"):
                log.warning("Kampaniya '%s': akkaunt ulanmagan, o'tkazib yuborildi", c["name"])
                continue
            jid = jobsvc.run_campaign(c["id"])
            if jid:
                log.info("Kampaniya '%s' avtomatik ishga tushdi: job #%s", c["name"], jid)
                self.start(jid)

    # ---------- ichki ----------
    def _status(self, job_id):
        r = db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
        return r["status"] if r else None

    async def _sleep(self, job_id, seconds: float) -> bool:
        db.ex("UPDATE jobs SET next_at=? WHERE id=?",
              ((datetime.now() + timedelta(seconds=seconds)).strftime(FMT), job_id))
        loop = asyncio.get_event_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            if self._status(job_id) != "running":
                return False
            await asyncio.sleep(min(1, max(0.05, end - loop.time())))
        db.ex("UPDATE jobs SET next_at=NULL WHERE id=?", (job_id,))
        return self._status(job_id) == "running"

    def _recount(self, job_id):
        db.ex("UPDATE jobs SET "
              "done=(SELECT COUNT(*) FROM job_targets WHERE job_id=? AND status='sent'),"
              "failed=(SELECT COUNT(*) FROM job_targets WHERE job_id=? AND status='failed') WHERE id=?",
              (job_id, job_id, job_id))

    def _wait_until(self, job_id, when: datetime, reason: str):
        db.ex("UPDATE jobs SET status='waiting', resume_at=?, error=?, next_at=NULL WHERE id=? AND status='running'",
              (when.strftime(FMT), reason, job_id))

    async def _run(self, job_id: int):
        job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job:
            return
        aid = job["account_id"]
        async with self._lock(aid):
            job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            if not job or job["status"] != "queued":
                return
            db.ex("UPDATE jobs SET status='running', started_at=COALESCE(started_at,?), error=NULL, resume_at=NULL "
                  "WHERE id=?", (db.now(), job_id))
            try:
                await self._process(job)
            except asyncio.CancelledError:
                db.ex("UPDATE jobs SET status='interrupted' WHERE id=? AND status='running'", (job_id,))
                raise
            except Exception as e:
                log.exception("Job #%s xatosi", job_id)
                db.ex("UPDATE jobs SET status='failed', error=? WHERE id=? AND status='running'",
                      (f"{type(e).__name__}: {e}", job_id))
            finally:
                db.ex("UPDATE jobs SET status='done' WHERE id=? AND status='running'", (job_id,))
                db.ex("UPDATE jobs SET next_at=NULL WHERE id=?", (job_id,))
                if self._status(job_id) in ("done", "cancelled", "failed"):
                    db.ex("UPDATE jobs SET finished_at=? WHERE id=?", (db.now(), job_id))
                self._recount(job_id)

    async def _process(self, job):
        job_id, aid = job["id"], job["account_id"]
        svc = self.mgr.get(aid)
        svc.clear_media_cache()
        variants = {v["id"]: v for v in jobsvc.variants_of(job_id)}
        first_variant = next(iter(variants.values()), None)
        targets = db.q("SELECT * FROM job_targets WHERE job_id=? AND status='pending' ORDER BY id", (job_id,))
        first = True
        for t in targets:
            if self._status(job_id) != "running":
                return
            if not first:
                delay = random.uniform(job["min_delay"], job["max_delay"]) * slow_factor(aid)
                if not await self._sleep(job_id, delay):
                    return
            first = False
            g = gate(aid)
            if g:
                self._wait_until(job_id, g[0], g[1])
                return
            v = variants.get(t["variant_id"]) or first_variant
            if not await self._send_one(job, t, v, svc):
                return
            self._recount(job_id)

    async def _send_one(self, job, t, v, svc) -> bool:
        """False: job to'xtatildi yoki to'xtatish kerak."""
        job_id, aid = job["id"], job["account_id"]
        media = jobsvc.media_of(v)
        grp = db.one("SELECT * FROM groups WHERE account_id=? AND tg_id=?", (aid, t["tg_id"]))
        gdict = {"title": t["title"], "username": grp["username"] if grp else "", "tg_id": t["tg_id"]}
        if media and grp and grp["no_media"]:
            db.ex("UPDATE job_targets SET status='skipped', error=? WHERE id=?",
                  ("Guruhda media yuborish taqiqlangan", t["id"]))
            return True
        utm = {"source": job["utm_source"] or "telegram", "campaign": job["utm_campaign"] or ""} if job["utm_on"] else None
        last = {"pct": -10}

        async def progress(sent, total):
            if not total:
                return
            pct = int(sent * 100 / total)
            if pct - last["pct"] >= 10 or pct == 100:
                last["pct"] = pct
                db.ex("UPDATE job_targets SET error=? WHERE id=?", (f"Yuklanmoqda: {pct}%", t["id"]))

        for _ in range(3):
            text = render_variant(v["text"] or "", gdict, utm)
            db.ex("UPDATE job_targets SET status='sending', error=NULL WHERE id=?", (t["id"],))
            try:
                ids = await svc.send(t["tg_id"], text, v["parse_mode"], media, v["media_type"], progress)
                db.ex("UPDATE job_targets SET status='sent', error=NULL, sent_at=?, msg_ids=? WHERE id=?",
                      (db.now(), json.dumps(ids), t["id"]))
                event(aid, "sent")
                return True
            except (errors.FloodWaitError, errors.SlowModeWaitError) as e:
                wait = int(getattr(e, "seconds", 30)) + 2
                if isinstance(e, errors.FloodWaitError):
                    event(aid, "flood", str(wait))
                db.ex("UPDATE job_targets SET status='pending', error=? WHERE id=?",
                      (f"Telegram {wait}s kutishni so'radi, kutilmoqda...", t["id"]))
                if wait > 900:
                    self._wait_until(job_id, datetime.now() + timedelta(seconds=wait), f"Telegram {wait // 60} daqiqa kutishni so'radi (FloodWait). Avtomatik davom etadi.")
                    return False
                if not await self._sleep(job_id, wait):
                    return False
            except Exception as e:
                name = type(e).__name__
                friendly, to_bl = FRIENDLY.get(name, (f"{name}: {e}"[:250], False))
                log.warning("Yuborishda xato: job=%s guruh=%s: %s", job_id, t["title"], name, exc_info=name not in FRIENDLY)
                event(aid, "error", name)
                if to_bl:
                    blacklist_add(aid, t["tg_id"], t["title"], "Avto: " + friendly)
                    friendly += " (qora ro'yxatga qo'shildi)"
                db.ex("UPDATE job_targets SET status='failed', error=?, sent_at=? WHERE id=?", (friendly, db.now(), t["id"]))
                return True
        db.ex("UPDATE job_targets SET status='failed', error=?, sent_at=? WHERE id=?",
              ("FloodWait: qayta urinishlar tugadi", db.now(), t["id"]))
        return True
