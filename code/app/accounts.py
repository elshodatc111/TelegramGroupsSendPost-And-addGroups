"""Ko'p akkaunt boshqaruvi."""
import asyncio
import json
from pathlib import Path

from . import db
from .config import SESSION_DIR, log
from .limits import health
from .telegram_service import TelegramService


class AccountManager:
    def __init__(self):
        self.services: dict[int, TelegramService] = {}
        self.rules_progress: dict[int, dict] = {}

    # ---------- xizmatlar ----------
    def get(self, account_id: int) -> TelegramService:
        svc = self.services.get(account_id)
        if svc is None:
            row = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
            if not row:
                raise RuntimeError(f"Akkaunt #{account_id} topilmadi")
            svc = TelegramService(account_id, row["session"])
            self.services[account_id] = svc
        return svc

    def rows(self, uid=None):
        if uid is None:
            return db.q("SELECT * FROM accounts ORDER BY id")
        return db.q("SELECT * FROM accounts WHERE user_id=? ORDER BY id", (uid,))

    def list(self, uid=None) -> list[dict]:
        out = []
        for r in self.rows(uid):
            info = self.services[r["id"]].info if r["id"] in self.services else None
            d = dict(r)
            d.update(connected=bool(info), info=info, health=health(r["id"]),
                     unread=db.one("SELECT COUNT(*) c FROM inbox WHERE account_id=? AND is_read=0", (r["id"],))["c"])
            out.append(d)
        return out

    def create(self, name: str, uid: int = 1) -> int:
        aid = db.ex("INSERT INTO accounts(name,session,created_at,user_id) VALUES(?,?,?,?)", (name, "tmp", db.now(), uid))
        db.ex("UPDATE accounts SET session=? WHERE id=?", (f"acc_{aid}", aid))
        return aid

    async def delete(self, account_id: int):
        svc = self.services.pop(account_id, None)
        if svc:
            try:
                await svc.shutdown()
            except Exception:
                pass
        row = db.one("SELECT session FROM accounts WHERE id=?", (account_id,))
        if row:
            for f in SESSION_DIR.glob(row["session"] + ".session*"):
                try:
                    f.unlink()
                except OSError:
                    pass
        for sql in (
            "DELETE FROM join_batches WHERE account_id=?", "DELETE FROM jobs WHERE account_id=?",
            "DELETE FROM campaigns WHERE account_id=?", "DELETE FROM group_lists WHERE account_id=?",
            "DELETE FROM groups WHERE account_id=?", "DELETE FROM group_tags WHERE account_id=?",
            "DELETE FROM group_members_log WHERE account_id=?", "DELETE FROM blacklist WHERE account_id=?",
            "DELETE FROM inbox WHERE account_id=?", "DELETE FROM account_events WHERE account_id=?",
            "DELETE FROM warmup WHERE account_id=?", "DELETE FROM autojoin WHERE account_id=?", "DELETE FROM audit_cfg WHERE account_id=?", "DELETE FROM disc_candidates WHERE account_id=?", "DELETE FROM leave_log WHERE account_id=?",
            "DELETE FROM accounts WHERE id=?",
        ):
            db.ex(sql, (account_id,))

    async def start_all(self):
        """Barcha saqlangan akkauntlarni ulaydi (inbox va holat uchun)."""
        for r in self.rows():
            if (SESSION_DIR / f"{r['session']}.session").exists():
                try:
                    await self.get(r["id"]).status()
                except Exception:
                    log.warning("Akkaunt %s ulanmadi", r["id"], exc_info=True)

    async def shutdown(self):
        for s in list(self.services.values()):
            try:
                await s.shutdown()
            except Exception:
                pass
