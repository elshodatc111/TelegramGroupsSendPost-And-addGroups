"""Majlislar: dars davomida davomatni kuzatish (Meet REST API polling), hisoblash, ogohlantirish va dars yakuni.

Davomat qoidasi yo'q: faqat KIM qatnashgani va qancha turgani yuritiladi (kirish/chiqish vaqti, jami daqiqa,
kech qolgan, erta chiqqan). O'quvchi ismi Meet'dagi nom bilan; bir odamning turli ismlari qo'lda birlashtiriladi.
Begonani aniqlash tizimi yo'q: faqat «ko'p odam» / «yangi nom» signali beriladi, chiqarishni o'qituvchi Meet ichida o'zi qiladi.
"""
import asyncio
import json
import re
from datetime import datetime, timedelta

from . import db, meet_google as G, meet_sched as S, notify
from .config import log


# ---------------------------------------------------------------- ism va birlashtirish
def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9а-яё ]+", "", (s or "").lower().replace("'", "").replace("ʻ", "").replace("`", "")).strip()


def aliases(gid) -> dict:
    return S.jget(f"alias_{gid}", {})


def person_of(gid, raw: str) -> str:
    raw = (raw or "").strip() or "Noma'lum"
    return aliases(gid).get(norm(raw), raw)


def roster_manual(gid) -> list[str]:
    return S.jget(f"roster_{gid}", [])


def merge_names(gid, from_name: str, to_name: str) -> int:
    """from_name nomi to_name bilan bir odam deb birlashtiriladi (shu guruhning barcha darslarida)."""
    from_name, to_name = from_name.strip(), to_name.strip()
    if not from_name or not to_name or norm(from_name) == norm(to_name):
        return 0
    a = aliases(gid)
    for k, v in list(a.items()):
        if norm(v) == norm(from_name):
            a[k] = to_name
    a[norm(from_name)] = to_name
    S.jput(f"alias_{gid}", a)
    n = 0
    for r in db.q("SELECT a.id, a.raw_name, a.person FROM mt_attendance a JOIN mt_lessons l ON l.id=a.lesson_id WHERE l.group_id=?", (gid,)):
        if norm(r["person"]) == norm(from_name) or norm(r["raw_name"]) == norm(from_name):
            db.ex("UPDATE mt_attendance SET person=? WHERE id=?", (to_name, r["id"]))
            n += 1
    rm = roster_manual(gid)
    if any(norm(x) == norm(from_name) for x in rm):
        S.jput(f"roster_{gid}", sorted({to_name if norm(x) == norm(from_name) else x for x in rm}))
    return n


def unmerge(gid, raw_name: str):
    a = aliases(gid)
    if a.pop(norm(raw_name), None) is not None:
        S.jput(f"alias_{gid}", a)
        for r in db.q("SELECT a.id FROM mt_attendance a JOIN mt_lessons l ON l.id=a.lesson_id WHERE l.group_id=? AND a.raw_name=?", (gid, raw_name)):
            db.ex("UPDATE mt_attendance SET person=raw_name WHERE id=?", (r["id"],))


def add_roster(gid, name: str):
    rm = roster_manual(gid)
    if name.strip() and norm(name) not in {norm(x) for x in rm}:
        S.jput(f"roster_{gid}", sorted(rm + [name.strip()]))


def remove_roster(gid, name: str):
    S.jput(f"roster_{gid}", [x for x in roster_manual(gid) if norm(x) != norm(name)])


def roster(gid) -> list[str]:
    """O'quvchilar ro'yxati: oldin qatnashgan o'quvchilar (o'qituvchisiz) + qo'lda qo'shilganlar."""
    names = {r["person"] for r in db.q("SELECT DISTINCT a.person FROM mt_attendance a JOIN mt_lessons l ON l.id=a.lesson_id "
                                       "WHERE l.group_id=? AND a.is_teacher=0 AND a.person IS NOT NULL", (gid,))}
    names |= set(roster_manual(gid))
    seen, out = set(), []
    for n in sorted(names, key=str.lower):
        if norm(n) not in seen:
            seen.add(norm(n))
            out.append(n)
    return out


# ---------------------------------------------------------------- o'qituvchini topish (heuristika + o'rganish)
def is_teacher(les, raw: str, user_id: str) -> bool:
    t = S.lesson_teacher(les)
    if not t:
        return False
    uids = S.jget(f"tuid_{t['id']}", [])
    if user_id and user_id in uids:
        return True
    rn = norm(raw)
    if not rn:
        return False
    names = [norm(t.get("name", "")), norm(t.get("meet_name", ""))]
    hit = any(n and (rn == n or n in rn or (len(rn) > 3 and rn in n)) for n in names)
    if not hit:
        toks = [x for x in names[0].split() if len(x) > 2]
        hit = len(toks) >= 2 and all(x in rn.split() or x in rn for x in toks)
    if not hit and t.get("gmail"):
        local = re.sub(r"[^a-z0-9]", "", t["gmail"].split("@")[0].lower())
        hit = len(local) >= 4 and local in re.sub(r"[^a-z0-9]", "", rn)
    if hit and user_id and user_id not in uids:
        S.jput(f"tuid_{t['id']}", uids + [user_id])
    return hit


def mark_teacher(att_id: int, flag: bool):
    """Qo'lda belgilash: shu ishtirokchi o'qituvchi (Meet user ID eslab qolinadi)."""
    a = db.one("SELECT * FROM mt_attendance WHERE id=?", (att_id,))
    if not a:
        return
    db.ex("UPDATE mt_attendance SET is_teacher=? WHERE id=?", (1 if flag else 0, att_id))
    les = S.lesson(a["lesson_id"])
    t = S.lesson_teacher(les) if les else None
    if t and a["user_id"]:
        uids = S.jget(f"tuid_{t['id']}", [])
        if flag and a["user_id"] not in uids:
            S.jput(f"tuid_{t['id']}", uids + [a["user_id"]])
        if not flag and a["user_id"] in uids:
            S.jput(f"tuid_{t['id']}", [u for u in uids if u != a["user_id"]])
    if flag and les:
        S.set_flag(les["id"], "t_in", a["first_in"] or S.fmt(S.now()))


# ---------------------------------------------------------------- hisoblash
def _sessions(raw: list[dict], conf_end: str | None, t_now: datetime) -> list[list]:
    out = []
    for s in raw:
        if not s.get("startTime"):
            continue
        st = S.from_utc(s["startTime"])
        en = S.from_utc(s["endTime"]) if s.get("endTime") else None
        out.append([S.fmt(st), S.fmt(en) if en else None])
    return sorted(out)


def union_seconds(sess: list[list], t_now: datetime) -> int:
    iv = sorted((S.parse(a), S.parse(b) if b else t_now) for a, b in sess)
    tot, cur_s, cur_e = 0, None, None
    for a, b in iv:
        if b < a:
            continue
        if cur_e is None or a > cur_e:
            if cur_e is not None:
                tot += int((cur_e - cur_s).total_seconds())
            cur_s, cur_e = a, b
        else:
            cur_e = max(cur_e, b)
    if cur_e is not None:
        tot += int((cur_e - cur_s).total_seconds())
    return tot


def peak_concurrency(all_sessions: list[list], t_now: datetime) -> int:
    ev = []
    for a, b in all_sessions:
        ev.append((S.parse(a), 1))
        ev.append(((S.parse(b) if b else t_now), -1))
    cur = best = 0
    for _, d in sorted(ev, key=lambda x: (x[0], x[1])):
        cur += d
        best = max(best, cur)
    return best


def _pinfo(p: dict):
    if p.get("signedinUser"):
        u = p["signedinUser"]
        return "signed", (u.get("displayName") or "").strip(), u.get("user", "")
    if p.get("anonymousUser"):
        return "anon", (p["anonymousUser"].get("displayName") or "").strip(), ""
    if p.get("phoneUser"):
        return "phone", (p["phoneUser"].get("displayName") or "").strip(), ""
    return "anon", "", ""


# ---------------------------------------------------------------- bitta darsni so'rash
async def poll_lesson(les: dict) -> dict:
    """Meet API'dan ishtirokchilarni o'qib mt_attendance ni yangilaydi. Qisqa natija qaytaradi."""
    lid, gid = les["id"], les["group_id"]
    t_now = S.now()
    recs = await G.conference_records(les["space_name"])
    if not recs:
        return {"conf": False, "active": False, "n": 0, "in_now": 0}
    starts = [S.from_utc(r["startTime"]) for r in recs if r.get("startTime")]
    ended = all(r.get("endTime") for r in recs)
    ends = [S.from_utc(r["endTime"]) for r in recs if r.get("endTime")]
    real_start = min(starts) if starts else None
    real_end = max(ends) if (ended and ends) else None
    db.ex("UPDATE mt_lessons SET real_start=?, real_end=?, status=? WHERE id=?",
          (S.fmt(real_start) if real_start else None, S.fmt(real_end) if real_end else None,
           "live" if not ended else (les["status"] if les["status"] != "planned" else "live"), lid))
    cached = {r["part_name"]: dict(r) for r in db.q("SELECT * FROM mt_attendance WHERE lesson_id=?", (lid,))}
    all_sessions = []
    for rec in recs:
        for p in await G.participants(rec["name"]):
            name = p["name"]
            sig = f"{p.get('earliestStartTime', '')}|{p.get('latestEndTime', '')}"
            kind, raw, uid = _pinfo(p)
            c = cached.get(name)
            if c and c["sig"] == sig and c["sessions_json"]:
                sess = json.loads(c["sessions_json"])
            else:
                sess = _sessions(await G.participant_sessions(name), None, t_now)
            if not sess and p.get("earliestStartTime"):
                sess = [[S.fmt(S.from_utc(p["earliestStartTime"])), S.fmt(S.from_utc(p["latestEndTime"])) if p.get("latestEndTime") else None]]
            if not sess:
                continue
            all_sessions += sess
            in_now = 1 if any(b is None for _, b in sess) else 0
            first_in = min(a for a, _ in sess)
            last_out = None if in_now else max(b for _, b in sess)
            total = union_seconds(sess, t_now)
            if c:
                person = c["person"] if norm(c["raw_name"]) == norm(raw) else person_of(gid, raw)
                teach = c["is_teacher"] or (1 if is_teacher(les, raw, uid) else 0)
                db.ex("UPDATE mt_attendance SET raw_name=?, person=?, kind=?, user_id=?, first_in=?, last_out=?, total_sec=?, in_now=?, "
                      "sessions_json=?, sig=?, is_teacher=?, updated_at=? WHERE id=?",
                      (raw or "Noma'lum", person, kind, uid, first_in, last_out, total, in_now, json.dumps(sess), sig, teach, S.fmt(t_now), c["id"]))
            else:
                teach = 1 if is_teacher(les, raw, uid) else 0
                db.ex("INSERT INTO mt_attendance(lesson_id,part_name,raw_name,person,kind,user_id,first_in,last_out,total_sec,in_now,sessions_json,sig,is_teacher,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (lid, name, raw or "Noma'lum", person_of(gid, raw or "Noma'lum"), kind, uid, first_in, last_out, total, in_now,
                       json.dumps(sess), sig, teach, S.fmt(t_now)))
    rows = [dict(r) for r in db.q("SELECT * FROM mt_attendance WHERE lesson_id=?", (lid,))]
    teacher_rows = [r for r in rows if r["is_teacher"]]
    if teacher_rows:
        S.set_flag(lid, "t_in", min(r["first_in"] for r in teacher_rows))
    peak = peak_concurrency(all_sessions, t_now)
    db.ex("UPDATE mt_lessons SET peak=? WHERE id=?", (max(peak, les.get("peak") or 0), lid))
    in_now = sum(r["in_now"] for r in rows)
    await check_crowd(S.lesson(lid), rows, in_now)
    return {"conf": True, "active": not ended, "n": len(rows), "in_now": in_now, "peak": peak}


async def check_crowd(les: dict, rows: list[dict], in_now: int):
    """«Ko'p odam» va «yangi nom» signallari (aniqlash tizimi emas; chiqarishni o'qituvchi o'zi qiladi)."""
    gid = les["group_id"]
    grp = S.group(gid)
    ros = roster(gid)
    limit = S.cfg("max_people") or (len(ros) + 2 if len(ros) >= 3 else 0)
    if limit and in_now > limit and "crowd" not in S.flags(les):
        S.set_flag(les["id"], "crowd", S.fmt(S.now()))
        await S.alert("crowd", f"«{grp['title']}» darsida hozir {in_now} kishi (kutilgan ko'pi bilan {limit}). Begona kirgan bo'lishi mumkin: "
                      f"o'qituvchi Meet ichida kerak bo'lsa chiqaradi.", level="warn", lesson_id=les["id"], group_id=gid, dedupe=f"crowd:{les['id']}")
    if len(ros) >= 3:
        done_before = db.one("SELECT COUNT(*) c FROM mt_lessons WHERE group_id=? AND status='done'", (gid,))["c"]
        if done_before >= 2:
            # roster shu darsdagi nomlarni ham o'z ichiga oladi, shuning uchun faqat OLDINGI darslarga solishtiramiz
            prev = {norm(r["person"]) for r in db.q("SELECT DISTINCT a.person FROM mt_attendance a JOIN mt_lessons l ON l.id=a.lesson_id "
                                                    "WHERE l.group_id=? AND l.id<>? AND a.is_teacher=0", (gid, les["id"]))} | {norm(x) for x in roster_manual(gid)}
            new = [r["person"] for r in rows if not r["is_teacher"] and norm(r["person"]) not in prev]
            for nm in new[:5]:
                await S.alert("unknown", f"«{grp['title']}» darsiga yangi nom kirdi: «{nm}». O'quvchi bo'lmasa, o'qituvchi Meet ichida chiqaradi; "
                              f"o'quvchi bo'lsa, ismini birlashtiring.", level="info", lesson_id=les["id"], group_id=gid,
                              dedupe=f"unk:{les['id']}:{norm(nm)}")


# ---------------------------------------------------------------- dars yakuni
def lesson_summary(lid) -> dict:
    """Dars bo'yicha qisqa natija (hisobot matni uchun): odamlar bo'yicha birlashtirilgan."""
    les = S.lesson(lid)
    people = person_rows(lid)
    studs = [p for p in people if not p["is_teacher"]]
    ros = roster(les["group_id"])
    present = {norm(p["person"]) for p in studs}
    absent = [n for n in ros if norm(n) not in present]
    return {"les": les, "people": people, "students": studs, "late": [p for p in studs if p["late"]],
            "early": [p for p in studs if p["early"]], "absent": absent, "teacher": next((p for p in people if p["is_teacher"]), None)}


def person_rows(lid) -> list[dict]:
    """Ishtirokchilar odam bo'yicha birlashtirilgan (turli qurilma/ism: oraliqlar birlashadi)."""
    les = S.lesson(lid)
    t_now = S.now()
    late_lim, early_lim = S.cfg("late_min"), S.cfg("early_min")
    start = S.parse(les["start_at"])
    end = S.parse(les["end_at"])
    ref_end = min(end, S.parse(les["real_end"])) if les.get("real_end") else end
    by: dict[str, dict] = {}
    for r in db.q("SELECT * FROM mt_attendance WHERE lesson_id=? ORDER BY first_in", (lid,)):
        r = dict(r)
        key = norm(r["person"]) or r["part_name"]
        sess = json.loads(r["sessions_json"] or "[]")
        p = by.setdefault(key, {"person": r["person"], "ids": [], "sess": [], "is_teacher": 0, "kind": r["kind"], "raws": []})
        p["ids"].append(r["id"])
        p["sess"] += sess
        p["is_teacher"] = p["is_teacher"] or r["is_teacher"]
        if r["raw_name"] not in p["raws"]:
            p["raws"].append(r["raw_name"])
    out = []
    for p in by.values():
        sess = p["sess"]
        if not sess:
            continue
        in_now = any(b is None for _, b in sess)
        first_in = S.parse(min(a for a, _ in sess))
        last_out = None if in_now else S.parse(max(b for _, b in sess))
        total = union_seconds(sess, t_now)
        late = (first_in - start) > timedelta(minutes=late_lim)
        finished = les["status"] in ("done", "missed") or les.get("real_end")
        early = bool(finished and last_out and last_out < ref_end - timedelta(minutes=early_lim))
        out.append({"person": p["person"], "ids": p["ids"], "raws": p["raws"], "is_teacher": p["is_teacher"], "kind": p["kind"],
                    "first_in": S.fmt(first_in), "last_out": S.fmt(last_out) if last_out else None, "in_now": in_now,
                    "minutes": round(total / 60), "late": late, "late_min": max(0, int((first_in - start).total_seconds() // 60)),
                    "early": early, "early_min": max(0, int((ref_end - last_out).total_seconds() // 60)) if early else 0})
    out.sort(key=lambda x: (not x["is_teacher"], x["first_in"]))
    return out


def report_text(lid) -> str:
    sm = lesson_summary(lid)
    les = S.decorate(dict(sm["les"]))
    g = les["group_title"]
    t = sm["teacher"]
    lines = [f"📊 Dars hisoboti: {g}", f"🗓 {les['day']} {les['hhmm']}–{les['end_hhmm']} · o'tgan vaqt: {les['elapsed']} daq"
             + (f" (rejada {les['duration_min']})" if les["elapsed"] else ""),
             f"👩‍🏫 O'qituvchi: {les['teacher_name'] or '—'}" + (f" ({t['first_in'][11:16]} da kirdi)" if t else " (Meet'da topilmadi)"),
             f"👥 Qatnashdi: {len(sm['students'])} · eng ko'pi bilan bir vaqtda: {les.get('peak') or 0}"]
    if sm["late"]:
        lines.append("⏱ Kech qoldi: " + ", ".join(f"{p['person']} (+{p['late_min']})" for p in sm["late"][:12]))
    if sm["early"]:
        lines.append("🚪 Erta chiqdi: " + ", ".join(f"{p['person']} (−{p['early_min']})" for p in sm["early"][:12]))
    if sm["absent"]:
        lines.append("❌ Kelmadi: " + ", ".join(sm["absent"][:20]))
    return "\n".join(lines)


async def finalize(les: dict, missed: bool):
    lid = les["id"]
    fl = S.flags(les)
    if "report" in fl:
        return
    db.ex("UPDATE mt_lessons SET status=? WHERE id=?", ("missed" if missed else "done", lid))
    S.set_flag(lid, "report", S.fmt(S.now()))
    grp = S.group(les["group_id"])
    if missed:
        await S.alert("missed", f"«{grp['title']}» darsi ({les['start_at'][:16]}) o'tmadi: Meet'da hech kim bo'lmagan.", level="warn",
                      lesson_id=lid, group_id=grp["id"], dedupe=f"missed:{lid}")
        return
    text = report_text(lid)
    if S.cfg("report_on"):
        try:
            await S.tg_send("@" + S.cfg("admin_username").lstrip("@") if S.cfg("admin_username") else "me", text)
        except S.TgFail as e:
            log.info("Dars hisoboti adminga yetmadi: %s", e)
        if S.cfg("report_to_group") and grp.get("tg_id"):
            try:
                await S.tg_send(int(grp["tg_id"]), text)
            except S.TgFail:
                pass
    S.add_alert("report", text.replace("\n", " · "), level="info", lesson_id=lid, group_id=grp["id"], copy=text, dedupe=f"rep:{lid}")


# ---------------------------------------------------------------- sikl
def _poll_ok():
    S.put("last_poll_at", S.fmt(S.now()))
    S.drop("last_poll_err")


def _poll_err(e: Exception):
    msg = (e.short() if isinstance(e, G.GoogleError) else f"{type(e).__name__}: {e}")[:300]
    S.put("last_poll_at", S.fmt(S.now()))
    S.put("last_poll_err", msg)
    day = S.fmt(S.now())[:10]
    if S.get("poll_err_day") != day:
        S.put("poll_err_day", day)
        S.put("poll_err_24", "0")
    S.put("poll_err_24", str(int(S.get("poll_err_24", "0") or 0) + 1))


async def tick():
    """Bitta aylanish: boshlanishiga 15 daqiqa qolgan darslardan yakunlanmaganlarigacha."""
    if not G.connected():
        return
    t_now = S.now()
    rows = db.q("SELECT * FROM mt_lessons WHERE status IN ('planned','live') AND space_name IS NOT NULL AND space_name<>'' AND start_at<=?",
                (S.fmt(t_now + timedelta(minutes=15)),))
    grace = timedelta(minutes=15)
    for r in rows:
        les = dict(r)
        end = S.parse(les["end_at"])
        try:
            res = await poll_lesson(les)
            _poll_ok()
        except Exception as e:
            _poll_err(e)
            log.warning("Majlis polling xatosi (dars %s): %s", les["id"], e)
            if t_now > end + grace + timedelta(hours=6):          # juda eski va o'qib bo'lmaydi: yopamiz
                await finalize(les, missed=True)
            continue
        les = S.lesson(les["id"])
        if not res["conf"]:
            if t_now >= end + grace:
                await finalize(les, missed=True)
            continue
        if not res["active"] and (t_now >= S.parse(les["real_end"]) + timedelta(minutes=5) or t_now >= end + grace):
            await finalize(les, missed=False)
        elif res["active"] and t_now >= end + timedelta(hours=3):
            await finalize(les, missed=False)                      # unutilgan ochiq konferensiya


async def loop():
    await asyncio.sleep(25)
    while True:
        try:
            S.ready()
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("meet_track.loop")
            notify.event("error", "majlis", "Davomat kuzatuvida xato (loglarga qarang)")
        await asyncio.sleep(max(30, min(60, S.cfg("poll_sec"))))
