"""Kiruvchi xabarlar (inbox): postlarga javoblar va shaxsiy xabarlar."""
from fastapi import APIRouter, Form, Request

from . import db
from .core import manager
from .web import acc_id, go, need_account, page

router = APIRouter()


@router.get("/inbox")
async def inbox_page(request: Request, f: str = "all"):
    aid = acc_id(request)
    where, args = "account_id=?", [aid]
    if f == "unread":
        where += " AND is_read=0"
    elif f in ("private", "reply", "mention"):
        where += " AND kind=?"
        args.append(f)
    rows = db.q(f"SELECT * FROM inbox WHERE {where} ORDER BY date DESC, id DESC LIMIT 300", tuple(args)) if aid else []
    counts = {}
    if aid:
        counts = {"all": db.one("SELECT COUNT(*) c FROM inbox WHERE account_id=?", (aid,))["c"],
                  "unread": db.one("SELECT COUNT(*) c FROM inbox WHERE account_id=? AND is_read=0", (aid,))["c"]}
    canned = db.q("SELECT * FROM canned_replies ORDER BY id")
    return page(request, "inbox.html", rows=rows, f=f, counts=counts, canned=canned)


@router.post("/inbox/read-all")
async def read_all(request: Request):
    aid = acc_id(request)
    if aid:
        db.ex("UPDATE inbox SET is_read=1 WHERE account_id=?", (aid,))
    return go("/inbox", msg="Hammasi o'qilgan deb belgilandi")


@router.post("/inbox/{mid}/read")
async def mark_read(mid: int):
    db.ex("UPDATE inbox SET is_read=1 WHERE id=?", (mid,))
    return go("/inbox")


@router.post("/inbox/{mid}/reply")
async def reply(request: Request, mid: int, text: str = Form(...)):
    m = db.one("SELECT * FROM inbox WHERE id=?", (mid,))
    if not m:
        return go("/inbox", err="Xabar topilmadi")
    text = text.strip()
    if not text:
        return go("/inbox", err="Javob matnini kiriting")
    try:
        await manager.get(m["account_id"]).reply(m["chat_id"], text, None if m["is_private"] else m["msg_id"])
    except Exception as e:
        return go("/inbox", err=f"Javob yuborilmadi: {type(e).__name__}: {e}")
    db.ex("UPDATE inbox SET replied=1, is_read=1 WHERE id=?", (mid,))
    return go("/inbox", msg="Javob yuborildi")


@router.post("/inbox/canned")
async def canned_add(title: str = Form(...), text: str = Form(...)):
    if not title.strip() or not text.strip():
        return go("/inbox", err="Sarlavha va matnni kiriting")
    db.ex("INSERT INTO canned_replies(title,text) VALUES(?,?)", (title.strip(), text.strip()))
    return go("/inbox", msg="Tayyor javob qo'shildi")


@router.post("/inbox/canned/{cid}/delete")
async def canned_delete(cid: int):
    db.ex("DELETE FROM canned_replies WHERE id=?", (cid,))
    return go("/inbox", msg="O'chirildi")
