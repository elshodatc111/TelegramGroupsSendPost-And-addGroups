"""Yuborilgan postlar ustidan amallar: statistika yangilash, o'chirish, tahrirlash."""
import asyncio
import json
from datetime import datetime, timedelta

from telethon import errors

from . import db
from .config import CAPTION_LIMIT, log
from .core import manager
from .jobsvc import media_of
from .limits import event
from .textutil import render_variant

ops: dict[int, dict] = {}     # job_id -> {"kind","done","total","fail","running"}


def _start(job_id, kind, total):
    ops[job_id] = {"kind": kind, "done": 0, "total": total, "fail": 0, "running": True}
    return ops[job_id]


def is_busy(job_id) -> bool:
    return bool(ops.get(job_id, {}).get("running"))


def _targets(job_id):
    return db.q("SELECT * FROM job_targets WHERE job_id=? AND status='sent' AND msg_ids IS NOT NULL ORDER BY id", (job_id,))


async def refresh_stats(job_id: int):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or is_busy(job_id):
        return
    ts = [t for t in _targets(job_id) if not t["deleted"]]
    op = _start(job_id, "stats", len(ts))
    svc = manager.get(job["account_id"])
    try:
        for t in ts:
            ids = json.loads(t["msg_ids"] or "[]")
            if not ids:
                op["done"] += 1
                continue
            try:
                r = (await svc.fetch_stats(t["tg_id"], ids[:1]))[0]
                if r.get("deleted"):
                    db.ex("UPDATE job_targets SET deleted=1, stat_at=? WHERE id=?", (db.now(), t["id"]))
                else:
                    db.ex("UPDATE job_targets SET views=?, forwards=?, reactions=?, replies=?, stat_at=? WHERE id=?",
                          (r["views"], r["forwards"], r["reactions"], r["replies"], db.now(), t["id"]))
            except errors.FloodWaitError as e:
                event(job["account_id"], "flood", str(e.seconds))
                if e.seconds > 120:
                    break
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                op["fail"] += 1
                log.warning("Statistika xatosi: %s", t["title"], exc_info=True)
            op["done"] += 1
            await asyncio.sleep(0.7)
    finally:
        op["running"] = False


async def delete_messages(job_id: int):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or is_busy(job_id):
        return
    ts = [t for t in _targets(job_id) if t["deleted"] != 2]
    op = _start(job_id, "delete", len(ts))
    svc = manager.get(job["account_id"])
    try:
        for t in ts:
            try:
                await svc.delete_messages(t["tg_id"], json.loads(t["msg_ids"]))
                db.ex("UPDATE job_targets SET deleted=2, error=? WHERE id=?", ("Xabar o'chirildi", t["id"]))
            except errors.FloodWaitError as e:
                await asyncio.sleep(min(e.seconds + 1, 120))
                op["fail"] += 1
            except Exception as e:
                op["fail"] += 1
                db.ex("UPDATE job_targets SET error=? WHERE id=?", (f"O'chirib bo'lmadi: {type(e).__name__}", t["id"]))
            op["done"] += 1
            await asyncio.sleep(1.0)
    finally:
        op["running"] = False


async def edit_messages(job_id: int, new_texts: dict[int, str]):
    """new_texts: variant_id -> yangi matn. Yuborilgan xabarlar matni/izohi almashtiriladi."""
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or is_busy(job_id):
        return
    for vid, text in new_texts.items():
        db.ex("UPDATE job_variants SET text=? WHERE id=? AND job_id=?", (text, vid, job_id))
    variants = {v["id"]: v for v in db.q("SELECT * FROM job_variants WHERE job_id=?", (job_id,))}
    ts = [t for t in _targets(job_id) if t["deleted"] != 2 and t["variant_id"] in new_texts]
    op = _start(job_id, "edit", len(ts))
    svc = manager.get(job["account_id"])
    utm = {"source": job["utm_source"] or "telegram", "campaign": job["utm_campaign"] or ""} if job["utm_on"] else None
    try:
        for t in ts:
            v = variants[t["variant_id"]]
            g = db.one("SELECT username FROM groups WHERE account_id=? AND tg_id=?", (job["account_id"], t["tg_id"]))
            text = render_variant(v["text"] or "", {"title": t["title"], "username": g["username"] if g else "", "tg_id": t["tg_id"]}, utm)
            ids = json.loads(t["msg_ids"])
            long_caption = bool(media_of(v)) and len(text) > CAPTION_LIMIT
            target_id = ids[-1] if (long_caption and len(ids) > 1) else ids[0]
            try:
                await svc.edit_message(t["tg_id"], target_id, text, v["parse_mode"])
            except errors.MessageNotModifiedError:
                pass
            except errors.FloodWaitError as e:
                await asyncio.sleep(min(e.seconds + 1, 120))
                op["fail"] += 1
            except Exception as e:
                op["fail"] += 1
                db.ex("UPDATE job_targets SET error=? WHERE id=?", (f"Tahrirlab bo'lmadi: {type(e).__name__}", t["id"]))
            op["done"] += 1
            await asyncio.sleep(1.0)
    finally:
        op["running"] = False


async def auto_refresh_loop():
    """Har soatda so'nggi 3 kunlik yuborishlar statistikasini yangilaydi."""
    await asyncio.sleep(120)
    while True:
        try:
            since = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
            for j in db.q("SELECT id, account_id FROM jobs WHERE status IN ('done','running') AND started_at>=?", (since,)):
                svc = manager.services.get(j["account_id"])
                if svc and svc.info and not is_busy(j["id"]):
                    await refresh_stats(j["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("auto_refresh_loop")
        await asyncio.sleep(3600)
