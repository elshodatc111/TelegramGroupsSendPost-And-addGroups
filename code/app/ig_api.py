"""Instagram API (Instagram Login, graph.instagram.com). Facebook sahifasi kerak emas.

Barcha so'rovlar `_req` orqali o'tadi (sinov uchun almashtirish oson).
"""
import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from . import db
from .config import log

GRAPH = "https://graph.instagram.com"
OAUTH = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
SCOPES = "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_insights"


class IGError(Exception):
    def __init__(self, msg, code=0, sub=0, status=0):
        super().__init__(msg)
        self.code, self.sub, self.status = code, sub, status

    @property
    def token_dead(self):
        return self.code == 190 or self.status == 401


def ver() -> str:
    return db.get_setting("ig_api_ver") or "v23.0"


async def _req(method: str, url: str, *, params=None, data=None, token: str | None = None):
    hdr = {"Authorization": f"Bearer {token}"} if token else {}
    last = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0)) as c:
        for attempt in range(3):
            try:
                r = await c.request(method, url, params=params, data=data, headers=hdr)
            except httpx.HTTPError as e:
                last = IGError(f"Instagram bilan aloqa yo'q: {type(e).__name__}")
                await asyncio.sleep(2 + attempt * 3)
                continue
            if r.status_code >= 500:
                last = IGError(f"Instagram serveri xatosi {r.status_code}", status=r.status_code)
                await asyncio.sleep(3 + attempt * 4)
                continue
            try:
                js = r.json()
            except Exception:
                js = {}
            err = js.get("error") if isinstance(js, dict) else None
            if r.status_code >= 400 or err:
                err = err or {}
                msg = err.get("message") or js.get("error_message") or r.text[:300]
                raise IGError(msg, int(err.get("code") or js.get("code") or 0), int(err.get("error_subcode") or 0), r.status_code)
            return js
    raise last or IGError("Instagram javob bermadi")


async def _get(path: str, token: str, **params):
    params = {k: v for k, v in params.items() if v is not None}
    return await _req("GET", f"{GRAPH}/{ver()}/{path.lstrip('/')}", params=params, token=token)


async def _post(path: str, token: str, **data):
    data = {k: v for k, v in data.items() if v is not None}
    return await _req("POST", f"{GRAPH}/{ver()}/{path.lstrip('/')}", data=data, token=token)


# ---------------------------------------------------------------- ulanish
def auth_url(app_id: str, redirect: str, state: str) -> str:
    q = {"client_id": app_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES, "state": state,
         "force_reauth": "true"}
    return OAUTH + "?" + urlencode(q)


async def exchange_code(app_id: str, secret: str, redirect: str, code: str) -> dict:
    js = await _req("POST", TOKEN_URL, data={"client_id": app_id, "client_secret": secret, "grant_type": "authorization_code",
                                              "redirect_uri": redirect, "code": code.split("#")[0]})
    if "data" in js and js["data"]:
        js = js["data"][0]
    return js


async def long_lived(secret: str, short: str) -> dict:
    return await _req("GET", f"{GRAPH}/access_token", params={"grant_type": "ig_exchange_token", "client_secret": secret,
                                                               "access_token": short})


async def refresh(token: str) -> dict:
    return await _req("GET", f"{GRAPH}/refresh_access_token", params={"grant_type": "ig_refresh_token", "access_token": token})


def expires_at(js: dict) -> str | None:
    sec = js.get("expires_in")
    if not sec:
        return None
    return (datetime.now() + timedelta(seconds=int(sec))).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- profil
ME_FIELDS = "user_id,username,name,account_type,profile_picture_url,followers_count,follows_count,media_count,biography,website"


async def me(token: str) -> dict:
    return await _get("me", token, fields=ME_FIELDS)


# ---------------------------------------------------------------- insights
def _epoch(d: datetime) -> int:
    return int(d.timestamp())


async def total_value(token: str, uid: str, metrics: list[str], since: datetime, until: datetime) -> dict:
    """{metric: qiymat}. Qo'llab-quvvatlanmagan metrikalar jimgina tushirib qoldiriladi."""
    out = {}
    for m in metrics:
        try:
            js = await _get(f"{uid}/insights", token, metric=m, period="day", metric_type="total_value",
                            since=_epoch(since), until=_epoch(until))
            d = (js.get("data") or [{}])[0]
            out[m] = int(((d.get("total_value") or {}).get("value")) or 0)
        except IGError as e:
            if e.token_dead:
                raise
            log.debug("IG insight %s: %s", m, e)
    return out


async def series(token: str, uid: str, metric: str, days: int = 29) -> list[tuple[str, int]]:
    """Kunlik qator (reach, follower_count): oxirgi 30 kungacha."""
    until = datetime.now()
    since = until - timedelta(days=days)
    js = await _get(f"{uid}/insights", token, metric=metric, period="day", since=_epoch(since), until=_epoch(until))
    vals = ((js.get("data") or [{}])[0]).get("values") or []
    res = []
    for v in vals:
        et = (v.get("end_time") or "")[:10]
        if et:
            res.append((et, int(v.get("value") or 0)))
    return res


async def demographics(token: str, uid: str, breakdown: str) -> list[dict]:
    js = await _get(f"{uid}/insights", token, metric="follower_demographics", period="lifetime", metric_type="total_value",
                    breakdown=breakdown)
    try:
        res = js["data"][0]["total_value"]["breakdowns"][0]["results"]
    except (KeyError, IndexError, TypeError):
        return []
    out = [{"k": ", ".join(r.get("dimension_values") or []), "v": int(r.get("value") or 0)} for r in res]
    return sorted(out, key=lambda x: -x["v"])[:20]


# ---------------------------------------------------------------- media
MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count,thumbnail_url,media_url"


async def media_list(token: str, uid: str, limit: int = 50) -> list[dict]:
    js = await _get("me/media", token, fields=MEDIA_FIELDS, limit=min(limit, 100))
    return js.get("data") or []


async def media_insights(token: str, mid: str, product: str) -> dict:
    """reach, views, saved, shares, interactions (+ reels o'rtacha ko'rish vaqti)."""
    cands = ["reach,views,saved,shares,total_interactions", "reach,saved,shares,total_interactions", "reach,saved"]
    if product == "REELS":
        cands.insert(0, "reach,views,saved,shares,total_interactions,ig_reels_avg_watch_time")
    last = None
    for metrics in cands:
        try:
            js = await _get(f"{mid}/insights", token, metric=metrics)
        except IGError as e:
            if e.token_dead:
                raise
            last = e
            continue
        out = {}
        for d in js.get("data") or []:
            v = d.get("values") or []
            out[d.get("name")] = int((v[0].get("value") if v else 0) or 0)
        return out
    if last:
        raise last
    return {}


# ---------------------------------------------------------------- joylash
async def publish_limit(token: str, uid: str) -> dict:
    js = await _get(f"{uid}/content_publishing_limit", token, fields="quota_usage,config")
    d = (js.get("data") or [{}])[0]
    return {"used": d.get("quota_usage"), "total": (d.get("config") or {}).get("quota_total")}


async def _container(token, uid, **kw) -> str:
    js = await _post(f"{uid}/media", token, **kw)
    return js["id"]


async def _wait(token, cid, timeout=300):
    t0 = asyncio.get_event_loop().time()
    while True:
        js = await _get(cid, token, fields="status_code,status")
        st = js.get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise IGError(f"Instagram faylni qabul qilmadi ({st}): {js.get('status', '')}")
        if asyncio.get_event_loop().time() - t0 > timeout:
            raise IGError("Instagram faylni vaqtida qayta ishlamadi (timeout)")
        await asyncio.sleep(4)


async def publish(token: str, uid: str, mtype: str, urls: list[str], caption: str) -> dict:
    """mtype: IMAGE | REELS | CAROUSEL | STORIES. urls — Instagram yuklab oladigan ochiq HTTPS havolalar."""
    if mtype == "IMAGE":
        cid = await _container(token, uid, image_url=urls[0], caption=caption)
    elif mtype == "REELS":
        cid = await _container(token, uid, media_type="REELS", video_url=urls[0], caption=caption, share_to_feed="true")
    elif mtype == "STORIES":
        key = "video_url" if urls[0].lower().split("?")[0].endswith((".mp4", ".mov")) else "image_url"
        cid = await _container(token, uid, media_type="STORIES", **{key: urls[0]})
    elif mtype == "CAROUSEL":
        kids = []
        for u in urls[:10]:
            if u.lower().endswith((".mp4", ".mov")):
                k = await _container(token, uid, media_type="VIDEO", video_url=u, is_carousel_item="true")
            else:
                k = await _container(token, uid, image_url=u, is_carousel_item="true")
            await _wait(token, k)
            kids.append(k)
        cid = await _container(token, uid, media_type="CAROUSEL", children=",".join(kids), caption=caption)
    else:
        raise IGError(f"Noma'lum tur: {mtype}")
    await _wait(token, cid)
    js = await _post(f"{uid}/media_publish", token, creation_id=cid)
    mid = js["id"]
    link = ""
    try:
        link = (await _get(mid, token, fields="permalink")).get("permalink", "")
    except IGError:
        pass
    return {"id": mid, "permalink": link}
