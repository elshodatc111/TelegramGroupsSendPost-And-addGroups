"""Cloudflare R2: bulutli fayl xotirasi (S3 bilan mos API, qo'shimcha kutubxonasiz: o'zimizning SigV4 imzo).

Har fayl o'z nomi bilan saqlanadi (cf_files jadvali), R2'da esa ig/<id>-<nom>.<kengaytma> kaliti bilan.
Hajm jadvaldan hisoblanadi; 9 GB (sozlanadi) dan oshganda ogohlantirish, limitga yetganda yuklash to'xtaydi.
"""
import asyncio
import hashlib
import hmac
import mimetypes
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

from . import db, notify
from .config import TMP_DIR, log

GB = 1024 ** 3
SAFE = "-_.~"
PREFIX = "ig/"
IMG = {".jpg", ".jpeg", ".png", ".webp"}
VID = {".mp4", ".mov", ".m4v"}


class CFError(Exception):
    pass


# ---------------------------------------------------------------- sozlamalar
def conf() -> dict:
    g = db.get_setting
    return {"account": (g("r2_account") or "").strip(), "key_id": (g("r2_key_id") or "").strip(), "secret": g("r2_secret") or "",
            "bucket": (g("r2_bucket") or "").strip(), "public": (g("r2_public") or "").strip().rstrip("/"),
            "warn_gb": _num(g("r2_warn_gb"), 9.0), "limit_gb": _num(g("r2_limit_gb"), 10.0),
            "autodelete": (g("r2_autodelete", "1") or "1") != "0"}


def _num(v, d):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def configured() -> bool:
    c = conf()
    return bool(c["account"] and c["key_id"] and c["secret"] and c["bucket"] and c["public"])


# ---------------------------------------------------------------- SigV4
def sign(method, host, path, query, headers, payload_hash, amzdate, access, secret, region="auto", service="s3"):
    """AWS Signature V4. (imzo, imzolangan_sarlavhalar) qaytaradi."""
    hdrs = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    hdrs["host"], hdrs["x-amz-date"] = host, amzdate
    names = sorted(hdrs)
    signed = ";".join(names)
    canon_h = "".join(f"{k}:{hdrs[k]}\n" for k in names)
    canon_q = "&".join(f"{quote(str(k), safe=SAFE)}={quote(str(v), safe=SAFE)}" for k, v in sorted(query.items()))
    creq = "\n".join([method, quote(path, safe="/" + SAFE), canon_q, canon_h, signed, payload_hash])
    scope = f"{amzdate[:8]}/{region}/{service}/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope, hashlib.sha256(creq.encode()).hexdigest()])

    def h(k, m):
        return hmac.new(k, m.encode(), hashlib.sha256).digest()
    k = h(h(h(h(("AWS4" + secret).encode(), amzdate[:8]), region), service), "aws4_request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    return sig, signed, f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={sig}"


async def _send(method: str, key: str = "", query: dict | None = None, path: Path | None = None, ctype: str | None = None,
                body: bytes | None = None) -> tuple[int, bytes]:
    """R2'ga bitta so'rov. Sinovda shu funksiya almashtiriladi."""
    c = conf()
    if not (c["account"] and c["key_id"] and c["secret"] and c["bucket"]):
        raise CFError("Cloudflare R2 sozlanmagan (Instagram SMM → Cloudflare → Sozlamalar).")
    host = f"{c['account']}.r2.cloudflarestorage.com"
    p = f"/{c['bucket']}" + (f"/{key}" if key else "")
    q = query or {}
    amz = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = {"x-amz-content-sha256": "UNSIGNED-PAYLOAD"}
    if ctype:
        headers["content-type"] = ctype
    size = path.stat().st_size if path else len(body or b"")
    if method == "PUT":
        headers["content-length"] = str(size)
    _, _, auth = sign(method, host, p, q, {k: v for k, v in headers.items() if k != "content-length"}, "UNSIGNED-PAYLOAD", amz, c["key_id"], c["secret"])
    headers["x-amz-date"], headers["Authorization"] = amz, auth
    url = f"https://{host}{quote(p, safe='/' + SAFE)}"

    async def gen():
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                yield chunk
    content = gen() if path else body
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=20)) as cl:
            r = await cl.request(method, url, params=q or None, headers=headers, content=content)
    except httpx.HTTPError as e:
        raise CFError(f"Cloudflare bilan aloqa yo'q: {type(e).__name__}")
    return r.status_code, r.content


def _err(status: int, data: bytes) -> str:
    m = re.search(rb"<Message>(.*?)</Message>", data or b"", re.S)
    code = re.search(rb"<Code>(.*?)</Code>", data or b"")
    txt = (m.group(1).decode(errors="ignore") if m else (data or b"")[:160].decode(errors="ignore"))
    if code and code.group(1) in (b"SignatureDoesNotMatch", b"InvalidAccessKeyId"):
        return "Kalit noto'g'ri (Access Key ID yoki Secret). Tekshirib qayta kiriting."
    if code and code.group(1) == b"NoSuchBucket":
        return "Bucket topilmadi: nomini tekshiring."
    return f"HTTP {status}: {txt}"


async def put_object(key: str, path: Path, ctype: str):
    st, data = await _send("PUT", key, path=path, ctype=ctype)
    if st not in (200, 201):
        raise CFError("Yuklanmadi: " + _err(st, data))


async def delete_object(key: str):
    st, data = await _send("DELETE", key)
    if st not in (200, 204, 404):
        raise CFError("O'chirilmadi: " + _err(st, data))


async def list_objects(prefix: str = PREFIX) -> list[dict]:
    out, token = [], None
    while True:
        q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            q["continuation-token"] = token
        st, data = await _send("GET", "", query=q)
        if st != 200:
            raise CFError("Ro'yxat olinmadi: " + _err(st, data))
        root = ET.fromstring(re.sub(rb' xmlns="[^"]+"', b"", data, count=1))
        for c in root.findall("Contents"):
            out.append({"key": c.findtext("Key"), "size": int(c.findtext("Size") or 0), "mod": (c.findtext("LastModified") or "")[:19].replace("T", " ")})
        if (root.findtext("IsTruncated") or "").lower() == "true" and root.findtext("NextContinuationToken"):
            token = root.findtext("NextContinuationToken")
        else:
            return out


# ---------------------------------------------------------------- fayllar
def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name.encode("ascii", "ignore").decode()).strip("-").lower()
    return (s or "fayl")[:40]


def kind_of(ext: str) -> str:
    return "video" if ext.lower() in VID else "image"


def public_url(row) -> str:
    return f"{conf()['public']}/{row['key']}" if row else ""


def get(fid):
    return db.one("SELECT * FROM cf_files WHERE id=?", (fid,)) if fid else None


def files(q: str = "", order: str = "new"):
    sql, args = "SELECT * FROM cf_files", []
    if q.strip():
        sql += " WHERE name LIKE ?"
        args.append(f"%{q.strip()}%")
    sql += {"size": " ORDER BY size DESC", "name": " ORDER BY name", "old": " ORDER BY created_at ASC", "unused": " ORDER BY used_n ASC, created_at ASC"}.get(order, " ORDER BY created_at DESC")
    return db.q(sql + " LIMIT 1000", args)


def unique_name(name: str, exclude_id=None) -> str:
    base, n = (name.strip() or "Fayl")[:200], 1
    cand = base
    while db.one("SELECT id FROM cf_files WHERE LOWER(name)=LOWER(?)" + (" AND id<>?" if exclude_id else ""), (cand,) + ((exclude_id,) if exclude_id else ())):
        n += 1
        cand = f"{base} ({n})"
    return cand


def usage() -> dict:
    c = conf()
    used = int(db.one("SELECT COALESCE(SUM(size),0) s FROM cf_files")["s"] or 0)
    n = db.one("SELECT COUNT(*) c FROM cf_files")["c"]
    warn, limit = c["warn_gb"] * GB, c["limit_gb"] * GB
    level = "none" if not configured() else "full" if used >= limit else "warn" if used >= warn else "ok"
    return {"used": used, "files": n, "warn": warn, "limit": limit, "pct": min(100, round(used * 100.0 / limit, 1)) if limit else 0,
            "warn_pct": round(warn * 100.0 / limit, 1) if limit else 90, "level": level, "free": max(0, limit - used)}


def fmt_size(b) -> str:
    b = float(b or 0)
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024 or u == "GB":
            return f"{b:.0f} {u}" if u == "B" else f"{b:.1f} {u}"
        b /= 1024


def check_alert():
    """Hajm 9 GB (sozlanadi) dan oshganda bir marta ogohlantiradi; pasayganda bekor qiladi."""
    u = usage()
    last = db.get_setting("cf_last_level", "ok")
    lvl = u["level"] if u["level"] in ("warn", "full") else "ok"
    if lvl != last:
        db.set_setting("cf_last_level", lvl)
        if lvl == "warn":
            msg = f"Cloudflare xotirasi {fmt_size(u['used'])} ({u['pct']}%): ogohlantirish chegarasidan oshdi. Keraksiz fayllarni o'chiring."
            notify.event("warn", "Cloudflare", msg)
            notify.toast("Cloudflare xotirasi to'lib boryapti", msg)
        elif lvl == "full":
            msg = f"Cloudflare xotirasi limitga yetdi ({fmt_size(u['used'])}). Yangi fayl yuklanmaydi: eskilarini o'chiring."
            notify.event("error", "Cloudflare", msg)
            notify.toast("Cloudflare xotirasi to'ldi", msg)
    return u


async def upload(path: Path, name: str, filename: str) -> dict:
    """Mahalliy faylni R2'ga yuklaydi va nom bilan saqlaydi. Rasm JPEG'ga o'giriladi (Instagram faqat JPEG qabul qiladi)."""
    ext = Path(filename).suffix.lower()
    if ext not in IMG | VID:
        raise CFError(f"«{filename}»: faqat rasm (jpg, png, webp) va video (mp4, mov) yuklanadi")
    if ext in IMG and ext not in (".jpg", ".jpeg"):
        try:
            from PIL import Image
            out = path.with_suffix(".jpg")
            Image.open(path).convert("RGB").save(out, "JPEG", quality=92)
            if out != path:
                path.unlink(missing_ok=True)
            path, ext = out, ".jpg"
        except Exception as e:
            raise CFError(f"«{filename}» rasmini o'qib bo'lmadi: {e}")
    size = path.stat().st_size
    u = usage()
    if u["used"] + size > u["limit"]:
        raise CFError(f"Limit ({fmt_size(u['limit'])}) to'ladi: «{filename}» ({fmt_size(size)}) sig'maydi. Avval keraksiz fayllarni o'chiring.")
    name = unique_name(name or Path(filename).stem)
    key = f"{PREFIX}{uuid.uuid4().hex[:8]}-{slug(name)}{ext}"
    ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
    await put_object(key, path, ctype)
    fid = db.ex("INSERT INTO cf_files(name,`key`,size,ctype,kind,created_at,used_n) VALUES(?,?,?,?,?,?,0)", (name, key, size, ctype, kind_of(ext), db.now()))
    check_alert()
    return dict(get(fid))


def referenced_by(fid: int, only_active=True) -> list:
    sts = "('draft','scheduled','reminded','publishing','failed','missed')" if only_active else "('draft','scheduled','reminded','publishing','failed','missed','published','cancelled')"
    return db.q(f"SELECT id, title, status FROM ig_plan WHERE media_json LIKE ? AND status IN {sts}", (f'%"cf:{fid}"%',))


async def delete_files(ids: list[int], force=False) -> dict:
    done, skipped, errs = 0, [], []
    for fid in ids:
        r = get(fid)
        if not r:
            continue
        refs = referenced_by(fid)
        if refs and not force:
            skipped.append(f"«{r['name']}» (rejada: {', '.join(x['title'] or '#' + str(x['id']) for x in refs[:2])})")
            continue
        try:
            await delete_object(r["key"])
        except CFError as e:
            errs.append(f"«{r['name']}»: {e}")
            continue
        db.ex("DELETE FROM cf_files WHERE id=?", (fid,))
        done += 1
    check_alert()
    return {"deleted": done, "skipped": skipped, "errors": errs}


def rename(fid: int, name: str):
    name = unique_name(name, exclude_id=fid)
    db.ex("UPDATE cf_files SET name=? WHERE id=?", (name, fid))
    return name


async def sync() -> dict:
    """R2 bilan jadvalni solishtiradi: bucketda bor-u jadvalda yo'q fayllar qo'shiladi, o'chib ketganlar jadvaldan olinadi."""
    objs = await list_objects()
    by_key = {o["key"]: o for o in objs}
    rows = db.q("SELECT * FROM cf_files")
    known = {r["key"] for r in rows}
    added = removed = 0
    for k, o in by_key.items():
        if k in known:
            db.ex("UPDATE cf_files SET size=? WHERE `key`=?", (o["size"], k))
            continue
        ext = Path(k).suffix.lower()
        if ext not in IMG | VID:
            continue
        stem = re.sub(r"^[0-9a-f]{8}-", "", Path(k).stem)
        db.ex("INSERT INTO cf_files(name,`key`,size,ctype,kind,created_at,used_n) VALUES(?,?,?,?,?,?,0)",
              (unique_name(stem), k, o["size"], mimetypes.guess_type(k)[0] or "", kind_of(ext), o["mod"] or db.now()))
        added += 1
    for r in rows:
        if r["key"] not in by_key:
            db.ex("DELETE FROM cf_files WHERE id=?", (r["id"],))
            removed += 1
    check_alert()
    return {"added": added, "removed": removed, "total": len(by_key)}


async def test_connection() -> list[tuple[str, bool, str]]:
    """Ulanishni bosqichma-bosqich tekshiradi: yozish, ochiq havola, o'chirish."""
    res = []
    if not conf()["public"]:
        return [("Ochiq havola", False, "Public URL kiritilmagan (R2 bucket → Settings → Public access)")]
    key = f"{PREFIX}_test-{uuid.uuid4().hex[:6]}.txt"
    tmp = TMP_DIR / "cf_test.txt"
    tmp.write_text("ok", encoding="utf-8")
    try:
        await put_object(key, tmp, "text/plain")
        res.append(("R2'ga yozish", True, "Kalitlar va bucket to'g'ri"))
    except CFError as e:
        return [("R2'ga yozish", False, str(e))]
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.get(f"{conf()['public']}/{key}")
        ok = r.status_code == 200 and r.text.strip() == "ok"
        res.append(("Ochiq havola (Instagram shu orqali yuklab oladi)", ok, "Ishlayapti" if ok else f"HTTP {r.status_code}: bucketda Public access (r2.dev) yoqilganini va URL to'g'riligini tekshiring"))
    except Exception as e:
        res.append(("Ochiq havola", False, f"{type(e).__name__}: {e}"))
    try:
        await delete_object(key)
        res.append(("O'chirish", True, "Ishlayapti"))
    except CFError as e:
        res.append(("O'chirish", False, str(e)))
    finally:
        tmp.unlink(missing_ok=True)
    return res


# ---------------------------------------------------------------- rejadagi postlar bilan bog'lanish
def ref_id(name: str):
    m = re.fullmatch(r"cf:(\d+)", name or "")
    return int(m.group(1)) if m else None


async def after_post(plan_id: int):
    """Post joylangach: ishlatilgan deb belgilaydi va (sozlamaga ko'ra) boshqa rejada kerak bo'lmasa bulutdan o'chiradi."""
    it = db.one("SELECT media_json FROM ig_plan WHERE id=?", (plan_id,))
    if not it:
        return
    import json
    try:
        names = json.loads(it["media_json"] or "[]")
    except Exception:
        names = []
    for n in names:
        fid = ref_id(n)
        if not fid or not get(fid):
            continue
        db.ex("UPDATE cf_files SET used_n=used_n+1, last_used_at=? WHERE id=?", (db.now(), fid))
        if conf()["autodelete"] and not referenced_by(fid):
            try:
                await delete_files([fid])
            except Exception:
                log.warning("Bulut faylni avto o'chirib bo'lmadi: %s", fid, exc_info=True)


def health_items(item):
    g = "Cloudflare R2"
    if not configured():
        return [item(g, "Bulutli xotira", "info", "Sozlanmagan (ixtiyoriy)", "Instagram SMM → Cloudflare → Sozlamalar.", key="cf_conf")]
    u = usage()
    st = {"ok": "ok", "warn": "warn", "full": "fail"}[u["level"]]
    return [item(g, "Bulutli xotira hajmi", st, f"{fmt_size(u['used'])} / {fmt_size(u['limit'])} ({u['pct']}%), {u['files']} ta fayl",
                 "" if st == "ok" else "Instagram SMM → Cloudflare: keraksiz fayllarni o'chiring.", key="cf_usage")]


async def loop():
    await asyncio.sleep(120)
    while True:
        try:
            if configured():
                try:
                    await sync()
                except CFError as e:
                    notify.event("warn", "Cloudflare", str(e))
                check_alert()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("cf.loop")
        await asyncio.sleep(6 * 3600)
