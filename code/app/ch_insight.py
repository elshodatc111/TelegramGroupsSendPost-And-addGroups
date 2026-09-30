"""Kanallarim: kunlik/oylik statistika, auditoriya, rasmiy Telegram grafiklari va viral ogohlantirish."""
import asyncio
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from . import ch_data, ch_tg, ch_track, db, notify
from .config import log

FMT = "%Y-%m-%d %H:%M:%S"
MONTHS = ["", "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun", "Iyul", "Avgust", "Sentyabr", "Oktyabr", "Noyabr", "Dekabr"]
LABELS = {"joined": "Qo'shildi", "left": "Chiqib ketdi", "views": "Ko'rishlar", "shares": "Ulashishlar", "reactions": "Reaksiyalar",
          "enabled": "Bildirishnoma yoqilgan", "muted": "Bildirishnoma o'chiq", "followers": "Obunachilar"}


# ---------------------------------------------------------------- rasmiy grafiklarni o'qish
def parse_graph(g) -> dict | None:
    """Telegram grafik JSON -> {"x": [sana yoki nom], "series": {nom: [qiymat]}, "pie": bool}"""
    if not isinstance(g, dict) or not g.get("columns"):
        return None
    cols = {c[0]: c[1:] for c in g["columns"] if c}
    xs = cols.get("x")
    names = g.get("names") or {}
    series = {}
    for k, v in cols.items():
        if k == "x":
            continue
        nm = names.get(k, k)
        low = nm.lower()
        for key, lab in LABELS.items():
            if key in low:
                nm = lab
                break
        series[nm] = v
    if not series:
        return None
    if xs and len(xs) > 1 and isinstance(xs[0], (int, float)) and xs[0] > 10**9:
        x = [datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d") for t in xs]
        return {"x": x, "series": series, "pie": False, "hourly": (xs[1] - xs[0]) <= 3600 * 1000 * 1.5}
    return {"x": xs or [], "series": series, "pie": len(next(iter(series.values()))) <= 1}


def official(cid: int) -> dict:
    """Saqlangan rasmiy grafiklar (kalit -> tahlil qilingan grafik) va holat."""
    out, status = {}, None
    for r in db.q("SELECT kind, data, updated_at FROM ch_official WHERE channel_id=?", (cid,)):
        if r["kind"] == "_status":
            status = json.loads(r["data"] or "{}")
            status["at"] = r["updated_at"]
            continue
        try:
            d = json.loads(r["data"])
        except Exception:
            continue
        out[r["kind"]] = d if r["kind"] == "_summary" else parse_graph(d)
    return {"graphs": out, "status": status}


def _save_official(cid, data: dict):
    now = db.now()
    for k, v in data.items():
        db.ex("INSERT OR REPLACE INTO ch_official(channel_id,kind,data,updated_at) VALUES(?,?,?,?)",
              (cid, k, json.dumps(v, ensure_ascii=False), now))
    fg = parse_graph(data.get("followers_graph"))
    if fg:
        rows = []
        for i, d in enumerate(fg["x"]):
            j = next((v[i] for n, v in fg["series"].items() if n == LABELS["joined"] and i < len(v)), None)
            lft = next((v[i] for n, v in fg["series"].items() if n == LABELS["left"] and i < len(v)), None)
            rows.append((cid, d, j, lft))
        for cid_, d, j, lft in rows:
            db.ex("INSERT OR IGNORE INTO ch_daily(channel_id,day) VALUES(?,?)", (cid_, d))
            db.ex("UPDATE ch_daily SET joined=?, left_n=? WHERE channel_id=? AND day=?", (j, lft, cid_, d))


def _set_status(cid, ok: bool, msg: str):
    db.ex("INSERT OR REPLACE INTO ch_official(channel_id,kind,data,updated_at) VALUES(?,?,?,?)",
          (cid, "_status", json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False), db.now()))


# ---------------------------------------------------------------- yig'ish
async def collect_official(ch) -> bool:
    try:
        data = await ch_tg.official_graphs(ch["account_id"], ch["tg_id"])
        _save_official(ch["id"], data)
        _set_status(ch["id"], True, "Rasmiy statistika yangilandi")
        return True
    except ch_tg.TgError as e:
        _set_status(ch["id"], False, str(e))
    except Exception as e:
        log.warning("Rasmiy statistika xatosi", exc_info=True)
        _set_status(ch["id"], False, f"{type(e).__name__}: {e}")
    return False


async def collect_audience(ch) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    if db.one("SELECT 1 FROM ch_audience WHERE channel_id=? AND day=?", (ch["id"], today)):
        return True
    try:
        a = await ch_tg.audience_sample(ch["account_id"], ch["tg_id"])
    except ch_tg.TgError as e:
        db.set_setting(f"ch_aud_err_{ch['id']}", str(e))
        return False
    db.del_setting(f"ch_aud_err_{ch['id']}")
    db.ex("INSERT OR REPLACE INTO ch_audience(channel_id,day,total,sampled,premium,bots,deleted,a_online,a_recent,a_week,a_month,a_long) "
          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
          (ch["id"], today, a["total"], a["sampled"], a["premium"], a["bots"], a["deleted"], a["a_online"], 0, a["a_week"],
           a["a_month"], a["a_long"]))
    return True


async def collect_channel(ch, force_audience=False) -> dict:
    res = {"audience": await collect_audience(ch)}
    if (ch["members"] or 0) >= 500 or force_audience:
        res["official"] = await collect_official(ch)
    else:
        _set_status(ch["id"], False, "Rasmiy statistika 500+ obunachili kanalda ochiladi (Telegram sharti). Hozircha o'z hisobimiz ishlatilmoqda.")
        res["official"] = False
    return res


# ---------------------------------------------------------------- jadvallar
def _members_by_day(cid, since):
    rows = db.q("SELECT day, members FROM ch_members_log WHERE channel_id=? AND comp_id=0 AND day>=? ORDER BY day", (cid, since))
    return {r["day"]: r["members"] for r in rows}


def daily(cid: int, days: int = 30) -> list[dict]:
    start = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    mem = _members_by_day(cid, (datetime.now() - timedelta(days=days + 40)).strftime("%Y-%m-%d"))
    posts = defaultdict(lambda: [0, 0])
    for r in db.q("SELECT SUBSTR(date,1,10) d, COUNT(*) c, COALESCE(SUM(views),0) v FROM ch_posts WHERE channel_id=? AND comp_id=0 AND date>=? GROUP BY d",
                  (cid, start)):
        posts[r["d"]] = [r["c"], int(r["v"])]
    dl = {r["day"]: r for r in db.q("SELECT * FROM ch_daily WHERE channel_id=? AND day>=?", (cid, start))}
    clicks = {d: n for d, n in ch_track.clicks_daily(cid, days)}
    of = official(cid)["graphs"].get("interactions_graph")
    ofv = {}
    if of:
        vs = of["series"].get(LABELS["views"])
        if vs:
            ofv = dict(zip(of["x"], vs))
    out, last = [], None
    known = sorted(mem)
    for i in range(days):
        d = (datetime.now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        m = mem.get(d)
        if m is None:
            prev = [k for k in known if k <= d]
            m = mem[prev[-1]] if prev and (datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(prev[-1], "%Y-%m-%d")).days <= 3 else None
        net = (m - last) if (m is not None and last is not None) else None
        if m is not None:
            last = m
        p = posts.get(d, [0, 0])
        row = dl.get(d)
        out.append({"day": d, "members": m, "net": net, "joined": row["joined"] if row else None, "left": row["left_n"] if row else None,
                    "posts": p[0], "views": p[1], "avg": round(p[1] / p[0]) if p[0] else 0, "clicks": clicks.get(d, 0),
                    "official_views": ofv.get(d)})
    return out


def monthly(cid: int, months: int = 12) -> list[dict]:
    first = (datetime.now().replace(day=1) - timedelta(days=31 * (months - 1))).strftime("%Y-%m")
    mem = db.q("SELECT day, members FROM ch_members_log WHERE channel_id=? AND comp_id=0 ORDER BY day", (cid,))
    by_m = {}
    for r in mem:
        by_m[r["day"][:7]] = r["members"]              # oy oxirgi yozuvi
    posts = {r["m"]: r for r in db.q(
        "SELECT SUBSTR(date,1,7) m, COUNT(*) c, COALESCE(SUM(views),0) v FROM ch_posts WHERE channel_id=? AND comp_id=0 GROUP BY m", (cid,))}
    jl = {r["m"]: r for r in db.q(
        "SELECT SUBSTR(day,1,7) m, SUM(joined) j, SUM(left_n) l FROM ch_daily WHERE channel_id=? AND joined IS NOT NULL GROUP BY m", (cid,))}
    cl = {r["m"]: int(r["n"] or 0) for r in db.q("SELECT SUBSTR(day,1,7) m, SUM(n) n FROM ch_clicks WHERE channel_id=? GROUP BY m", (cid,))}
    out, prev = [], None
    keys = sorted(set(by_m) | set(posts) | set(cl) | set(jl))
    for m in keys:
        if m < first:
            prev = by_m.get(m, prev)
            continue
        end = by_m.get(m)
        p = posts.get(m)
        j = jl.get(m)
        out.append({"month": m, "label": f"{MONTHS[int(m[5:7])]} {m[:4]}", "members": end,
                    "net": (end - prev) if (end is not None and prev is not None) else None,
                    "joined": int(j["j"]) if j and j["j"] is not None else None, "left": int(j["l"]) if j and j["l"] is not None else None,
                    "posts": p["c"] if p else 0, "views": int(p["v"]) if p else 0,
                    "avg": round(p["v"] / p["c"]) if p and p["c"] else 0, "clicks": cl.get(m, 0)})
        if end is not None:
            prev = end
    return out


def audience_latest(cid: int) -> dict | None:
    r = db.one("SELECT * FROM ch_audience WHERE channel_id=? ORDER BY day DESC LIMIT 1", (cid,))
    if not r:
        return None
    a = dict(r)
    real = max(1, (a["sampled"] or 0) - (a["bots"] or 0) - (a["deleted"] or 0))
    a["real"] = real
    a["active_pct"] = round(((a["a_online"] or 0) + (a["a_week"] or 0)) / real * 100)
    a["premium_pct"] = round((a["premium"] or 0) / real * 100, 1)
    a["coverage"] = round((a["sampled"] or 0) / max(a["total"] or 1, 1) * 100)
    return a


def audience_trend(cid: int, n: int = 30) -> list[dict]:
    rows = db.q("SELECT day, sampled, bots, deleted, a_online, a_week FROM ch_audience WHERE channel_id=? ORDER BY day DESC LIMIT ?", (cid, n))
    out = []
    for r in reversed(rows):
        real = max(1, (r["sampled"] or 0) - (r["bots"] or 0) - (r["deleted"] or 0))
        out.append({"day": r["day"], "active_pct": round(((r["a_online"] or 0) + (r["a_week"] or 0)) / real * 100)})
    return out


# ---------------------------------------------------------------- viral ogohlantirish
def _baseline(cid, comp_id) -> float:
    cut = (datetime.now() - timedelta(days=30)).strftime(FMT)
    lim = (datetime.now() - timedelta(hours=30)).strftime(FMT)
    v = [r["views"] for r in db.q("SELECT views FROM ch_posts WHERE channel_id=? AND comp_id=? AND date>=? AND date<=? AND views>0",
                                   (cid, comp_id, cut, lim))]
    return statistics.median(v) if len(v) >= 5 else 0.0


def viral_scan(cid: int) -> list[int]:
    """Yangi postlarni o'z kanali/raqobatchi me'yori bilan solishtiradi. Yangi alert id'lari qaytadi."""
    x = float(db.get_setting("ch_viral_x", 2.5) or 2.5)
    min_views = int(db.get_setting("ch_viral_min", 150) or 150)
    new_ids = []
    sources = [(0, "Sizning kanal")] + [(c["id"], c["title"]) for c in ch_data.competitors(cid)]
    cut = (datetime.now() - timedelta(hours=48)).strftime(FMT)
    for comp_id, name in sources:
        base = _baseline(cid, comp_id)
        if base <= 0:
            continue
        for p in db.q("SELECT * FROM ch_posts WHERE channel_id=? AND comp_id=? AND date>=?", (cid, comp_id, cut)):
            try:
                age_h = (datetime.now() - datetime.strptime(p["date"], FMT)).total_seconds() / 3600
            except (TypeError, ValueError):
                continue
            if age_h < 2 or (p["views"] or 0) < min_views:
                continue
            expected = base * min(1.0, (age_h / 24) ** 0.6)
            ratio = (p["views"] or 0) / max(expected, 1)
            if ratio < x:
                continue
            kind = "own" if comp_id == 0 else "viral"
            if db.one("SELECT 1 FROM ch_alerts WHERE channel_id=? AND comp_id=? AND msg_id=? AND kind=?", (cid, comp_id, p["msg_id"], kind)):
                continue
            title = f"{name}: post odatdagidan {ratio:.1f} marta ko'p ko'rilmoqda"
            info = f"{p['views']:,} ko'rish ({int(age_h)} soat ichida), odatdagi ~{int(base):,}. " + (p["text"] or "")[:200].replace("\n", " ")
            aid = db.ex("INSERT INTO ch_alerts(channel_id,comp_id,msg_id,ts,kind,title,info,ratio,views) VALUES(?,?,?,?,?,?,?,?,?)",
                        (cid, comp_id, p["msg_id"], db.now(), kind, title[:395], info, round(ratio, 2), p["views"]))
            new_ids.append(aid)
            notify.toast("Viral post" if comp_id else "Postingiz yaxshi ketmoqda", title)
            db.ex("UPDATE ch_alerts SET notified=1 WHERE id=?", (aid,))
            notify.event("info", "viral", f"{cid}:{title}")
    return new_ids


def alerts(cid: int | None, limit: int = 100):
    if cid:
        return db.q("SELECT a.*, c.title comp_title FROM ch_alerts a LEFT JOIN ch_competitors c ON c.id=a.comp_id "
                    "WHERE a.channel_id=? ORDER BY a.id DESC LIMIT ?", (cid, limit))
    return db.q("SELECT a.*, c.title comp_title FROM ch_alerts a LEFT JOIN ch_competitors c ON c.id=a.comp_id ORDER BY a.id DESC LIMIT ?", (limit,))


def unseen() -> int:
    return db.one("SELECT COUNT(*) c FROM ch_alerts WHERE seen=0")["c"]


async def loop():
    await asyncio.sleep(300)
    tick = 0
    while True:
        try:
            if ch_data.channel_account():
                for ch in ch_data.channels():
                    viral_scan(ch["id"])
                    if tick % 8 == 0:                                   # ~har 6 soatda
                        await collect_channel(ch)
                        await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ch_insight.loop")
        tick += 1
        await asyncio.sleep(45 * 60)
