"""Postlarni navbat bilan, oraliq interval qo'yib yuboruvchi fon xizmati."""
import asyncio
import random
from datetime import datetime, timedelta

from telethon import errors

from . import db
from .config import log


class Sender:
    def __init__(self, tg):
        self.tg = tg
        self.tasks: dict[int, asyncio.Task] = {}
        self.lock = asyncio.Lock()       # bir vaqtda faqat bitta job ishlaydi
        self._sched_task = None

    # ---------- boshqaruv ----------
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
        while True:
            try:
                due = db.q("SELECT id FROM jobs WHERE status='scheduled' AND scheduled_at<=?",
                           (db.now(),))
                for r in due:
                    db.ex("UPDATE jobs SET status='queued' WHERE id=?", (r["id"],))
                    self.start(r["id"])
            except Exception:
                pass
            await asyncio.sleep(5)

    # ---------- ichki ----------
    def _status(self, job_id):
        r = db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
        return r["status"] if r else None

    async def _sleep(self, job_id, seconds: float) -> bool:
        """Kutish. Job to'xtatilgan bo'lsa False qaytaradi."""
        db.ex("UPDATE jobs SET next_at=? WHERE id=?",
              ((datetime.now() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S"), job_id))
        end = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < end:
            if self._status(job_id) != "running":
                return False
            await asyncio.sleep(min(1, max(0.05, end - asyncio.get_event_loop().time())))
        db.ex("UPDATE jobs SET next_at=NULL WHERE id=?", (job_id,))
        return self._status(job_id) == "running"

    def _recount(self, job_id):
        db.ex("UPDATE jobs SET "
              "done=(SELECT COUNT(*) FROM job_targets WHERE job_id=? AND status='sent'),"
              "failed=(SELECT COUNT(*) FROM job_targets WHERE job_id=? AND status='failed') "
              "WHERE id=?", (job_id, job_id, job_id))

    async def _run(self, job_id: int):
        async with self.lock:
            job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            if not job or job["status"] != "queued":
                return
            db.ex("UPDATE jobs SET status='running', started_at=COALESCE(started_at,?), error=NULL "
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
                db.ex("UPDATE jobs SET finished_at=?, next_at=NULL WHERE id=?", (db.now(), job_id))
                self._recount(job_id)

    async def _process(self, job):
        job_id = job["id"]
        targets = db.q("SELECT * FROM job_targets WHERE job_id=? AND status='pending' ORDER BY id",
                       (job_id,))
        self.tg.clear_media_cache()
        first = True
        for t in targets:
            if self._status(job_id) != "running":
                return
            if not first:
                delay = random.uniform(job["min_delay"], job["max_delay"])
                if not await self._sleep(job_id, delay):
                    return
            first = False
            if not await self._send_one(job, t):
                return
            self._recount(job_id)

    async def _send_one(self, job, t) -> bool:
        """False: job to'xtatildi."""
        job_id = job["id"]
        last = {"pct": -10}

        async def progress(sent, total):
            if not total:
                return
            pct = int(sent * 100 / total)
            if pct - last["pct"] >= 10 or pct == 100:
                last["pct"] = pct
                db.ex("UPDATE job_targets SET error=? WHERE id=?", (f"Yuklanmoqda: {pct}%", t["id"]))

        for _ in range(3):
            db.ex("UPDATE job_targets SET status='sending', error=NULL WHERE id=?", (t["id"],))
            try:
                await self.tg.send(t["tg_id"], job["text"] or "", job["parse_mode"],
                                   job["media_path"], job["media_type"], progress)
                db.ex("UPDATE job_targets SET status='sent', error=NULL, sent_at=? WHERE id=?",
                      (db.now(), t["id"]))
                return True
            except (errors.FloodWaitError, errors.SlowModeWaitError) as e:
                wait = int(getattr(e, "seconds", 30)) + 2
                db.ex("UPDATE job_targets SET status='pending', error=? WHERE id=?",
                      (f"Telegram {wait}s kutishni so'radi, kutilmoqda...", t["id"]))
                if not await self._sleep(job_id, wait):
                    return False
            except Exception as e:
                log.exception("Yuborishda xato: job=%s guruh=%s", job_id, t["title"])
                db.ex("UPDATE job_targets SET status='failed', error=?, sent_at=? WHERE id=?",
                      (f"{type(e).__name__}: {e}"[:300], db.now(), t["id"]))
                return True
        db.ex("UPDATE job_targets SET status='failed', error=?, sent_at=? WHERE id=?",
              ("FloodWait: qayta urinishlar tugadi", db.now(), t["id"]))
        return True
