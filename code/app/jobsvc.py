"""Post yuborish biznes mantiqi: qoralama, variantlarni taqsimlash, kampaniyalar, takrorlash."""
import hashlib
import json
from datetime import datetime, timedelta

from . import db
from .limits import blacklisted_ids


# ---------------- variantlar ----------------
def variants_of(job_id: int):
    return db.q("SELECT * FROM job_variants WHERE job_id=? ORDER BY idx", (job_id,))


def media_of(v) -> list[str]:
    try:
        return json.loads(v["media_json"] or "[]")
    except Exception:
        return []


def variants_hash(variants: list[dict]) -> str:
    raw = "|".join(f"{v['text']}#{','.join(v['media'])}" for v in variants)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def ptr_key(account_id: int, campaign_id: int | None, variants: list[dict]) -> str:
    return f"c{campaign_id}" if campaign_id else f"h{account_id}:{variants_hash(variants)}"


def get_ptr(key: str) -> int:
    r = db.one("SELECT ptr FROM variant_ptr WHERE key=?", (key,))
    return r["ptr"] if r else 0


def set_ptr(key: str, ptr: int):
    db.ex("INSERT INTO variant_ptr(key,ptr) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET ptr=excluded.ptr", (key, ptr))


def job_ptr_key(job) -> str:
    vs = [{"text": v["text"] or "", "media": media_of(v)} for v in variants_of(job["id"])]
    return ptr_key(job["account_id"], job["campaign_id"], vs)


def plan_assignment(job_id: int) -> list[int]:
    """Har bir 'pending' guruh uchun variant tartib raqami (0..n-1), joriy ko'rsatkichdan boshlab."""
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    n = len(variants_of(job_id))
    ptr = get_ptr(job_ptr_key(job)) % max(1, n)
    cnt = db.one("SELECT COUNT(*) c FROM job_targets WHERE job_id=? AND status='pending'", (job_id,))["c"]
    return [(ptr + k) % max(1, n) for k in range(cnt)]


def assign_variants(job_id: int):
    """Tasdiqlashda: guruhlarga variantlarni ketma-ket aylanma bilan biriktiradi va ko'rsatkichni siljitadi."""
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    vs = variants_of(job_id)
    n = len(vs)
    key = job_ptr_key(job)
    ptr = get_ptr(key) % n
    targets = db.q("SELECT id FROM job_targets WHERE job_id=? AND status='pending' ORDER BY id", (job_id,))
    for k, t in enumerate(targets):
        db.ex("UPDATE job_targets SET variant_id=? WHERE id=?", (vs[(ptr + k) % n]["id"], t["id"]))
    set_ptr(key, (ptr + len(targets)) % n)


# ---------------- qoralama ----------------
def create_draft(account_id: int, variants: list[dict], tg_ids: list[int], *, min_delay: int, max_delay: int,
                 scheduled_at: str | None = None, campaign_id: int | None = None, name: str = "",
                 utm_on: int = 0, utm_source: str = "", utm_campaign: str = "", skip_ads: int = 1) -> tuple[int, dict]:
    """Yangi qoralama job yaratadi. (job_id, statistika) qaytaradi."""
    groups = {r["tg_id"]: r for r in db.q("SELECT * FROM groups WHERE account_id=?", (account_id,))}
    bl = blacklisted_ids(account_id)
    first = variants[0]
    job_id = db.ex(
        "INSERT INTO jobs(account_id,campaign_id,text,parse_mode,media_path,media_type,status,min_delay,max_delay,"
        "scheduled_at,created_at,utm_on,utm_source,utm_campaign,name,total) VALUES(?,?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,0)",
        (account_id, campaign_id, first["text"], first["parse_mode"], (first["media"] or [None])[0], first["media_type"],
         min_delay, max_delay, scheduled_at, db.now(), utm_on, utm_source, utm_campaign, name))
    for i, v in enumerate(variants):
        db.ex("INSERT INTO job_variants(job_id,idx,text,parse_mode,media_json,media_type) VALUES(?,?,?,?,?,?)",
              (job_id, i, v["text"], v["parse_mode"], json.dumps(v["media"]), v["media_type"]))
    stats = {"total": 0, "blacklist": 0, "ads": 0, "missing": 0}
    rows = []
    for tg_id in tg_ids:
        g = groups.get(tg_id)
        if not g:
            stats["missing"] += 1
            continue
        if tg_id in bl:
            rows.append((job_id, tg_id, g["title"], "skipped", "Qora ro'yxatda"))
            stats["blacklist"] += 1
        elif skip_ads and g["ads_flag"] and not g["ads_ok"]:
            rows.append((job_id, tg_id, g["title"], "skipped", "Reklama taqiqlangan bo'lishi mumkin"))
            stats["ads"] += 1
        else:
            rows.append((job_id, tg_id, g["title"], "pending", None))
            stats["total"] += 1
    db.many("INSERT INTO job_targets(job_id,tg_id,title,status,error) VALUES(?,?,?,?,?)", rows)
    db.ex("UPDATE jobs SET total=? WHERE id=?", (stats["total"], job_id))
    return job_id, stats


def confirm_job(job_id: int, when: str | None = None) -> str:
    """Qoralamani tasdiqlaydi. 'queued' yoki 'scheduled' qaytaradi."""
    assign_variants(job_id)
    if when:
        try:
            dt = datetime.strptime(when, "%Y-%m-%dT%H:%M")
            if dt > datetime.now():
                db.ex("UPDATE jobs SET status='scheduled', scheduled_at=? WHERE id=?",
                      (dt.strftime("%Y-%m-%d %H:%M:00"), job_id))
                return "scheduled"
        except ValueError:
            pass
    db.ex("UPDATE jobs SET status='queued', scheduled_at=NULL WHERE id=?", (job_id,))
    return "queued"


# ---------------- kampaniyalar ----------------
def campaign_variants(cid: int) -> list[dict]:
    return [{"text": v["text"] or "", "parse_mode": v["parse_mode"], "media": json.loads(v["media_json"] or "[]"),
             "media_type": v["media_type"]} for v in db.q("SELECT * FROM campaign_variants WHERE campaign_id=? ORDER BY idx", (cid,))]


def save_campaign(account_id: int, name: str, variants: list[dict], tg_ids: list[int], *, min_delay: int, max_delay: int,
                  utm_on: int, utm_source: str, utm_campaign: str, skip_ads: int, recurrence: str, rec_time: str,
                  rec_days: str) -> int:
    row = db.one("SELECT id FROM campaigns WHERE account_id=? AND name=?", (account_id, name))
    fields = (min_delay, max_delay, utm_on, utm_source, utm_campaign, skip_ads, json.dumps(tg_ids), recurrence,
              rec_time, rec_days)
    if row:
        cid = row["id"]
        db.ex("UPDATE campaigns SET min_delay=?,max_delay=?,utm_on=?,utm_source=?,utm_campaign=?,skip_ads=?,target_json=?,"
              "recurrence=?,rec_time=?,rec_days=? WHERE id=?", fields + (cid,))
        db.ex("DELETE FROM campaign_variants WHERE campaign_id=?", (cid,))
    else:
        cid = db.ex("INSERT INTO campaigns(account_id,name,created_at,min_delay,max_delay,utm_on,utm_source,utm_campaign,"
                    "skip_ads,target_json,recurrence,rec_time,rec_days) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (account_id, name, db.now()) + fields)
    for i, v in enumerate(variants):
        db.ex("INSERT INTO campaign_variants(campaign_id,idx,text,parse_mode,media_json,media_type) VALUES(?,?,?,?,?,?)",
              (cid, i, v["text"], v["parse_mode"], json.dumps(v["media"]), v["media_type"]))
    return cid


def next_run(rec: str, time_s: str, days_s: str, after: datetime | None = None) -> str | None:
    if rec not in ("daily", "weekly"):
        return None
    now = after or datetime.now()
    try:
        h, m = (int(x) for x in time_s.split(":"))
    except Exception:
        h, m = 10, 0
    days = {int(x) for x in days_s.split(",") if x.strip().isdigit()} if rec == "weekly" else set(range(7))
    if not days:
        days = set(range(7))
    for i in range(0, 9):
        cand = (now + timedelta(days=i)).replace(hour=h, minute=m, second=0, microsecond=0)
        if cand > now and cand.weekday() in days:
            return cand.strftime("%Y-%m-%d %H:%M:%S")
    return None


def activate_campaign(cid: int):
    c = db.one("SELECT * FROM campaigns WHERE id=?", (cid,))
    if not c:
        return
    if c["recurrence"] in ("daily", "weekly"):
        db.ex("UPDATE campaigns SET rec_active=1, next_run=? WHERE id=?",
              (next_run(c["recurrence"], c["rec_time"], c["rec_days"]), cid))
    else:
        db.ex("UPDATE campaigns SET rec_active=0, next_run=NULL WHERE id=?", (cid,))


def run_campaign(cid: int) -> int | None:
    """Kampaniyadan yangi job yaratib, navbatga qo'yadi (takrorlash yoki qo'lda ishga tushirish)."""
    c = db.one("SELECT * FROM campaigns WHERE id=?", (cid,))
    if not c:
        return None
    variants = campaign_variants(cid)
    tg_ids = json.loads(c["target_json"] or "[]")
    if not variants or not tg_ids:
        return None
    job_id, stats = create_draft(c["account_id"], variants, tg_ids, min_delay=c["min_delay"] or 20,
                                 max_delay=c["max_delay"] or 60, campaign_id=cid, name=c["name"],
                                 utm_on=c["utm_on"], utm_source=c["utm_source"] or "", utm_campaign=c["utm_campaign"] or "",
                                 skip_ads=c["skip_ads"])
    if not stats["total"]:
        db.ex("DELETE FROM jobs WHERE id=?", (job_id,))
        return None
    confirm_job(job_id)
    db.ex("UPDATE campaigns SET last_run=? WHERE id=?", (db.now(), cid))
    return job_id
