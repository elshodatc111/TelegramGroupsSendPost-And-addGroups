"""Soxta Google (OAuth, Meet REST v2, Calendar v3) — Majlislar sinovlari uchun. Haqiqiy Google'ga hech narsa yuborilmaydi."""
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

STATE = {}


def reset():
    STATE.clear()
    STATE.update(n=0, spaces={}, members={}, calendars={}, events={}, acl=[], records={}, calls=[], fail_refresh=False,
                 meet_disabled=False, bad_members=set(), email="host@gmail.com", hd=None, tokens=0)


reset()
app = FastAPI()


def err(status, message, reason="", details=None):
    body = {"error": {"code": status, "message": message, "status": reason}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status)


def log(request, extra=""):
    STATE["calls"].append(f"{request.method} {request.url.path} {extra}".strip())


@app.post("/token")
async def token(request: Request):
    f = await request.form()
    log(request, f.get("grant_type", ""))
    if f.get("grant_type") == "authorization_code":
        if f.get("code") != "good-code" or not f.get("code_verifier"):
            return JSONResponse({"error": "invalid_grant", "error_description": "Bad code"}, status_code=400)
        STATE["tokens"] += 1
        return {"access_token": f"at-{STATE['tokens']}", "refresh_token": "rt-secret-123", "expires_in": 3600, "token_type": "Bearer"}
    if STATE["fail_refresh"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "Token has been expired or revoked."}, status_code=400)
    if f.get("refresh_token") != "rt-secret-123":
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    STATE["tokens"] += 1
    return {"access_token": f"at-{STATE['tokens']}", "expires_in": 3600, "token_type": "Bearer"}


@app.get("/userinfo")
async def userinfo(request: Request):
    log(request)
    out = {"email": STATE["email"], "sub": "1"}
    if STATE["hd"]:
        out["hd"] = STATE["hd"]
    return out


# ---------------- Meet
@app.post("/meet/spaces")
async def spaces_create(request: Request):
    log(request)
    if STATE["meet_disabled"]:
        return err(403, "Google Meet API has not been used in project 1 before or it is disabled.", "PERMISSION_DENIED",
                   [{"reason": "SERVICE_DISABLED", "metadata": {"activationUrl": "https://console.developers.google.com/apis/api/meet.googleapis.com/overview?project=1"}}])
    body = await request.json()
    STATE["n"] += 1
    sid = f"sp{STATE['n']}"
    sp = {"name": f"spaces/{sid}", "meetingUri": f"https://meet.google.com/abc-defg-h{STATE['n']:02d}", "meetingCode": f"abc-defg-h{STATE['n']:02d}",
          "config": {"accessType": body.get("config", {}).get("accessType", "TRUSTED"), "moderation": body.get("config", {}).get("moderation", "OFF")}}
    STATE["spaces"][sp["name"]] = sp
    STATE["members"][sp["name"]] = []
    return sp


@app.get("/meet/spaces/{sid}")
async def space_get(sid: str, request: Request):
    log(request)
    sp = STATE["spaces"].get(f"spaces/{sid}")
    return sp or err(404, "not found", "NOT_FOUND")


@app.post("/meet/spaces/{sid}/members")
async def member_create(sid: str, request: Request):
    log(request)
    body = await request.json()
    name = f"spaces/{sid}"
    if body.get("email") in STATE["bad_members"]:
        return err(400, "Invalid email or user has no Google account", "INVALID_ARGUMENT")
    m = {"name": f"{name}/members/m{len(STATE['members'][name]) + 1}", "email": body["email"], "role": body.get("role", "ROLE_UNSPECIFIED")}
    STATE["members"][name].append(m)
    return m


@app.get("/meet/spaces/{sid}/members")
async def member_list(sid: str, request: Request):
    log(request)
    return {"members": STATE["members"].get(f"spaces/{sid}", [])}


@app.get("/meet/conferenceRecords")
async def conf_list(request: Request):
    log(request)
    flt = request.query_params.get("filter", "")
    recs = []
    if "space.name" in flt:
        sp = flt.split('"')[1]
        recs = [{k: v for k, v in r.items() if k != "participants"} for r in STATE["records"].get(sp, [])]
    else:
        for rs in STATE["records"].values():
            recs += [{k: v for k, v in r.items() if k != "participants"} for r in rs]
    return {"conferenceRecords": recs[: int(request.query_params.get("pageSize", 20))]}


def _find(cid):
    for rs in STATE["records"].values():
        for r in rs:
            if r["name"] == f"conferenceRecords/{cid}":
                return r


@app.get("/meet/conferenceRecords/{cid}/participants")
async def parts(cid: str, request: Request):
    log(request)
    r = _find(cid)
    out = []
    for p in (r or {}).get("participants", []):
        q = {k: v for k, v in p.items() if k != "sessions"}
        out.append(q)
    return {"participants": out}


@app.get("/meet/conferenceRecords/{cid}/participants/{pid}/participantSessions")
async def sessions(cid: str, pid: str, request: Request):
    log(request, pid)
    r = _find(cid)
    for p in (r or {}).get("participants", []):
        if p["name"].endswith("/" + pid):
            return {"participantSessions": [dict(s, name=f"{p['name']}/participantSessions/s{i}") for i, s in enumerate(p["sessions"])]}
    return {}


# ---------------- Calendar
@app.post("/cal/calendars")
async def cal_create(request: Request):
    log(request)
    body = await request.json()
    cid = f"cal{len(STATE['calendars']) + 1}@group.calendar.google.com"
    STATE["calendars"][cid] = body
    STATE["events"][cid] = {}
    return {"id": cid, **body}


@app.delete("/cal/calendars/{cid}")
async def cal_delete(cid: str, request: Request):
    log(request)
    STATE["calendars"].pop(cid, None)
    return JSONResponse(None, status_code=204)


@app.post("/cal/calendars/{cid}/acl")
async def cal_acl(cid: str, request: Request):
    log(request)
    STATE["acl"].append((cid, await request.json()))
    return {"id": "acl1"}


@app.post("/cal/calendars/{cid}/events")
async def ev_create(cid: str, request: Request):
    log(request)
    if cid not in STATE["events"]:
        return err(404, "Not Found", "NOT_FOUND")
    body = await request.json()
    eid = f"ev{sum(len(v) for v in STATE['events'].values()) + 1}"
    STATE["events"][cid][eid] = body
    return {"id": eid, **body}


@app.patch("/cal/calendars/{cid}/events/{eid}")
async def ev_patch(cid: str, eid: str, request: Request):
    log(request)
    if eid not in STATE["events"].get(cid, {}):
        return err(404, "Not Found", "NOT_FOUND")
    STATE["events"][cid][eid].update(await request.json())
    return {"id": eid, **STATE["events"][cid][eid]}


@app.delete("/cal/calendars/{cid}/events/{eid}")
async def ev_delete(cid: str, eid: str, request: Request):
    log(request)
    STATE["events"].get(cid, {}).pop(eid, None)
    return JSONResponse(None, status_code=204)


def serve(port: int):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    return srv
