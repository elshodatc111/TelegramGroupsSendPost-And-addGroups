"""Soxta Zoom (OAuth + REST) va soxta Telegram Bot API: Zoom Online sinovlari uchun. Haqiqiy tarmoqqa chiqilmaydi."""
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

STATE = {}


def reset():
    STATE.clear()
    STATE.update(n=1000, meetings={}, calls=[], bad_clients=set(), no_scope=set(), fail_create=set(), user_type=1,
                 sent=[], answers=[], edits=[], updates=[], fail_send=None, bot_ok=True)


reset()
app = FastAPI()


def err(status, code, message):
    return JSONResponse({"code": code, "message": message}, status_code=status)


@app.post("/oauth/token")
async def token(request: Request):
    q = request.query_params
    auth = request.headers.get("authorization", "")
    STATE["calls"].append(f"TOKEN {q.get('account_id')}")
    import base64
    cid = base64.b64decode(auth.split()[-1]).decode().split(":")[0] if auth else ""
    if cid in STATE["bad_clients"] or q.get("grant_type") != "account_credentials":
        return JSONResponse({"reason": "Invalid client_id or client_secret", "error": "invalid_client"}, status_code=401)
    return {"access_token": f"tok-{cid}", "token_type": "bearer", "expires_in": 3600, "scope": "meeting:write:meeting:admin"}


def _client(request):
    return request.headers.get("authorization", "").replace("Bearer tok-", "")


@app.get("/v2/users/{uid}")
async def user(uid: str, request: Request):
    STATE["calls"].append(f"GET user {uid}")
    if uid == "me":
        return err(400, 1001, "Invalid access token.")
    return {"id": "u1", "email": uid, "type": STATE["user_type"]}


@app.post("/v2/users/{uid}/meetings")
async def create(uid: str, request: Request):
    cid = _client(request)
    body = await request.json()
    STATE["calls"].append(f"POST meeting {uid}")
    if cid in STATE["no_scope"]:
        return err(400, 4711, "Invalid access token, does not contain scopes:[meeting:write:meeting:admin].")
    if uid in STATE["fail_create"]:
        return err(500, 5000, "boom")
    STATE["n"] += 1
    mid = STATE["n"]
    m = {"id": mid, "uid": uid, "topic": body.get("topic"), "duration": body.get("duration"), "start_time": body.get("start_time"),
         "join_url": f"https://zoom.test/j/{mid}?pwd=abc", "start_url": f"https://zoom.test/s/{mid}?zak=SECRETZAK", "password": "pw1",
         "settings": body.get("settings", {}), "deleted": False, "ended": False}
    STATE["meetings"][mid] = m
    return JSONResponse(m, status_code=201)


@app.get("/v2/meetings/{mid}")
async def get(mid: int):
    m = STATE["meetings"].get(mid)
    return m if m and not m["deleted"] else err(404, 3001, "Meeting does not exist")


@app.put("/v2/meetings/{mid}/status")
async def status(mid: int, request: Request):
    m = STATE["meetings"].get(mid)
    if not m:
        return err(404, 3001, "Meeting does not exist")
    m["ended"] = True
    STATE["calls"].append(f"END {mid}")
    return JSONResponse(None, status_code=204)


@app.delete("/v2/meetings/{mid}")
async def delete(mid: int):
    m = STATE["meetings"].get(mid)
    if not m:
        return err(404, 3001, "Meeting does not exist")
    m["deleted"] = True
    STATE["calls"].append(f"DELETE {mid}")
    return JSONResponse(None, status_code=204)


# ---------------- Telegram Bot API
@app.post("/bot{token}/{method}")
async def bot(token: str, method: str, request: Request):
    p = await request.json() if (await request.body()) else {}
    if not STATE["bot_ok"] or token != "123:GOOD":
        return JSONResponse({"ok": False, "error_code": 401, "description": "Unauthorized"}, status_code=401)
    if method == "getMe":
        return {"ok": True, "result": {"id": 1, "is_bot": True, "username": "majlis_test_bot"}}
    if method == "sendMessage":
        if STATE["fail_send"]:
            return JSONResponse({"ok": False, "error_code": 403, "description": STATE["fail_send"]}, status_code=403)
        STATE["sent"].append(p)
        return {"ok": True, "result": {"message_id": len(STATE["sent"]) + 100, "chat": {"id": p["chat_id"]}}}
    if method == "answerCallbackQuery":
        STATE["answers"].append(p)
        return {"ok": True, "result": True}
    if method == "editMessageText":
        STATE["edits"].append(p)
        return {"ok": True, "result": True}
    if method == "getUpdates":
        return {"ok": True, "result": []}
    return {"ok": True, "result": True}


def serve(port):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    return srv
