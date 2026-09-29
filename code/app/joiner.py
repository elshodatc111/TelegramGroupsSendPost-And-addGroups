"""Excel'dagi guruhlarga navbat bilan, xavfsiz interval bilan a'zo bo'lish."""
import asyncio
import random
from datetime import datetime, timedelta

from telethon import errors

from . import db
from .config import log
from .limits import event, slow_factor, work_gate
from .telegram_service import NotAGroup

FMT = "%Y-%m-%d %H:%M:%S"

# Telethon xato nomi -> (holat, izoh)
KNOWN = {
    "UsernameNotOccupiedError": ("notfound", "Bunday username topilmadi"),
    "UsernameInvalidError": ("notfound", "Username noto'g'ri formatda"),
    "ChannelPrivateError": ("private", "Yopiq kanal/guruh yoki kirishga ruxsat yo'q"),
    "ChannelInvalidError": ("notfound", "Kanal/guruh topilmadi"),
    "InviteHashExpiredError": ("private", "Taklif havolasi muddati tugagan"),
    "InviteHashInvalidError": ("private", "Taklif havolasi yaroqsiz"),
    "InviteRequestSentError": ("requested", "A'zolik so'rovi yuborildi, admin tasdiqlashi kerak"),
    "UserAlreadyParticipantError": ("already", "Allaqachon a'zo"),
    "UserBannedInChannelError": ("banned", "Siz bu guruhdan bloklangansiz"),
    "UserKickedError": ("banned", "Siz bu guruhdan chiqarib yuborilgansiz"),
    "UsersTooMuchError": ("failed", "Guruhda a'zolar soni limitga yetgan"),
}
STOP = {"ChannelsTooMuchError", "UserChannelsTooMuchError"}
SUCCESS = ("joined", "already", "requested")


def _fmt(dt):
    return dt.strftime(FMT)


class Joiner:
    def __init__(self, manager):
        self.mgr = manager
        self.tasks: dict[int, asyncio.Task] = {}
        self.locks: dict[int, asyncio.Lock] = {}     # har akkaunt uchun bitta paket
        self._sched = None

    # ---------- boshqaruv ----------
    def start(self, batch_id: int):
        t = self.tasks.get(batch_id)
        if t and not t.done():
            return
        self.tasks[batch_id] = asyncio.create_task(self._run(batch_id))

    def begin_scheduler(self):
        self._sched = asyncio.create_task(self._scheduler())

    async def stop(self):
        if self._sched:
            self._sched.cancel()
        for t in self.tasks.values():
            t.cancel()

    async def _scheduler(self):
        """Limit/FloodWait sababli kutayotgan paketlarni vaqti kelganda davom ettiradi."""
        while True:
            try:
                for r in db.q("SELECT id FROM join_batches WHERE status='waiting' AND resume_at<=?", (db.now(),)):
                    db.ex("UPDATE join_batches SET status='queued' WHERE id=?", (r["id"],))
                    self.start(r["id"])
            except Exception:
                log.exception("Joiner scheduler")
            await asyncio.sleep(10)

    def _lock(self, aid):
        return self.locks.setdefault(aid, asyncio.Lock())

    def _status(self, bid):
        r = db.one("SELECT status FROM join_batches WHERE id=?", (bid,))
        return r["status"] if r else None

    async def _sleep(self, bid, seconds: float) -> bool:
        """Kutish (bekor qilinsa False)."""
        db.ex("UPDATE join_batches SET next_at=? WHERE id=?", (_fmt(datetime.now() + timedelta(seconds=seconds)), bid))
        loop = asyncio.get_event_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            if self._status(bid) != "running":
                return False
            await asyncio.sleep(min(1, max(0.05, end - loop.time())))
        db.ex("UPDATE join_batches SET next_at=NULL WHERE id=?", (bid,))
        return self._status(bid) == "running"

    def _wait_until(self, bid, when: datetime, reason: str):
        db.ex("UPDATE join_batches SET status='waiting', resume_at=?, error=?, next_at=NULL "
              "WHERE id=? AND status='running'", (_fmt(when), reason, bid))

    # ---------- ishlash ----------
    async def _run(self, bid: int):
        b0 = db.one("SELECT * FROM join_batches WHERE id=?", (bid,))
        if not b0:
            return
        aid = b0["account_id"]
        async with self._lock(aid):
            b = db.one("SELECT * FROM join_batches WHERE id=?", (bid,))
            if not b or b["status"] != "queued":
                return
            db.ex("UPDATE join_batches SET status='running', started_at=COALESCE(started_at,?), "
                  "error=NULL, resume_at=NULL WHERE id=?", (db.now(), bid))
            try:
                await self._process(b)
            except asyncio.CancelledError:
                db.ex("UPDATE join_batches SET status='interrupted' WHERE id=? AND status='running'", (bid,))
                raise
            except Exception as e:
                log.exception("Join paketi #%s xatosi", bid)
                db.ex("UPDATE join_batches SET status='failed', error=? WHERE id=? AND status='running'",
                      (f"{type(e).__name__}: {e}", bid))
            finally:
                db.ex("UPDATE join_batches SET status='done' WHERE id=? AND status='running'", (bid,))
                db.ex("UPDATE join_batches SET next_at=NULL WHERE id=?", (bid,))
                if self._status(bid) in ("done", "cancelled", "failed"):
                    db.ex("UPDATE join_batches SET finished_at=? WHERE id=?", (db.now(), bid))
            if self._status(bid) == "done":
                try:      # yangi qo'shilgan guruhlar post yuborish ro'yxatida chiqishi uchun
                    await self.mgr.get(aid).fetch_groups()
                except Exception:
                    log.warning("Guruhlarni yangilab bo'lmadi", exc_info=True)

    def _daily_wait(self, aid: int, limit: int):
        """Kunlik limit to'lgan bo'lsa, qachon davom etish mumkinligini qaytaradi."""
        since = _fmt(datetime.now() - timedelta(hours=24))
        rows = db.q("SELECT jt.tried_at FROM join_targets jt JOIN join_batches jb ON jb.id=jt.batch_id "
                    "WHERE jb.account_id=? AND jt.status IN ('joined','requested') "
                    "AND jt.tried_at>=? ORDER BY jt.tried_at", (aid, since))
        if len(rows) < limit:
            return None
        free_at = datetime.strptime(rows[len(rows) - limit]["tried_at"], FMT) + timedelta(hours=24, minutes=1)
        return free_at

    async def _process(self, b):
        bid, aid = b["id"], b["account_id"]
        first, last_heavy = True, True
        while True:
            if self._status(bid) != "running":
                return
            t = db.one("SELECT * FROM join_targets WHERE batch_id=? AND status='pending' ORDER BY id LIMIT 1", (bid,))
            if not t:
                return
            wg = work_gate(aid)
            if wg:
                self._wait_until(bid, wg[0], wg[1])
                return
            free_at = self._daily_wait(aid, b["daily_limit"])
            if free_at:
                self._wait_until(bid, free_at, f"Kunlik limit ({b['daily_limit']}) to'ldi. Avtomatik davom etadi.")
                return
            if not first:
                # a'zo bo'lish bo'lmagan (topilmadi/allaqachon a'zo) holatlardan keyin qisqa kutish
                delay = (random.uniform(b["min_delay"], b["max_delay"]) * slow_factor(aid)) if last_heavy else random.uniform(4, 9)
                if not await self._sleep(bid, delay):
                    return
            first = False
            outcome = await self._join_one(b, t)
            if outcome == "stop":
                return
            if outcome == "wait":
                first = True      # FloodWait kutilgan, qayta urinishdan oldin yana kutmaymiz
                continue
            last_heavy = outcome in ("joined", "requested")

    async def _join_one(self, b, t) -> str:
        bid, aid = b["id"], b["account_id"]

        def save(status, detail=None, title=None):
            db.ex("UPDATE join_targets SET status=?, detail=?, title=COALESCE(?,title), tried_at=? WHERE id=?",
                  (status, detail, title, db.now(), t["id"]))

        try:
            status, title = await self.mgr.get(aid).join(t["kind"], t["key"])
            if status == "joined":
                event(aid, "joined")
            save(status, "Muvaffaqiyatli a'zo bo'lindi" if status == "joined" else "Allaqachon a'zo edingiz", title)
            return status
        except errors.FloodWaitError as e:
            secs = int(e.seconds)
            event(aid, "flood", str(secs))
            if secs <= 300:
                db.ex("UPDATE join_batches SET error=? WHERE id=?", (f"Telegram {secs}s kutishni so'radi...", bid))
                ok = await self._sleep(bid, secs + 2)
                db.ex("UPDATE join_batches SET error=NULL WHERE id=?", (bid,))
                return "wait" if ok else "stop"
            self._wait_until(bid, datetime.now() + timedelta(seconds=secs + 60),
                             f"Telegram {secs // 60} daqiqa kutishni so'radi (FloodWait). Avtomatik davom etadi.")
            return "stop"
        except NotAGroup as e:
            save("invalid", str(e))
            return "invalid"
        except ValueError as e:      # Telethon: username topilmadi
            save("notfound", "Bunday username topilmadi")
            log.info("join ValueError %s: %s", t["key"], e)
            return "notfound"
        except Exception as e:
            name = type(e).__name__
            if name in STOP:
                save("limit", "Akkaunt kanal/guruh a'zolik limitiga yetdi (Telegram cheklovi)")
                db.ex("UPDATE join_batches SET status='failed', error=? WHERE id=?",
                      ("Akkaunt a'zo bo'lish limitiga yetdi. Ba'zi guruhlardan chiqib, keyin davom ettiring.", bid))
                return "stop"
            if name in KNOWN:
                st, msg = KNOWN[name]
                save(st, msg)
                return st
            log.exception("Join xatosi: %s", t["ref"])
            save("failed", f"{name}: {e}"[:250])
            return "failed"
