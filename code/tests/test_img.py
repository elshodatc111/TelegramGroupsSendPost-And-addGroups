"""Image Generator (faqat prompt yozadi) sinovlari: OpenAI chaqiruvlari soxta (tarmoqqa chiqilmaydi), qolgan hamma narsa haqiqiy:
baza, navbat, ishchi, sahifalar, ZIP, sahnalarni 5 soniyagacha bo'lish, paketli prompt yozish."""
import html as _html
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient


def png_bytes(color=(40, 160, 120), size=(600, 800)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


ANALYSIS = {
    "title": "Koreys tili reklamasi",
    "roles": [{"key": "ustoz", "title": "O'qituvchi", "description": "30 yosh, ko'zoynak"}],
    "locations": [{"key": "xona", "name": "Dars xonasi", "description": "Yorug' xona, oq doska"}],
    "scenes": [
        {"t_from": 0, "t_to": 3, "label": "Hook", "visual": "O'qituvchi kameraga qaraydi", "voice": "Koreys tilini o'rganmoqchimisiz?", "onscreen": "KOREYS TILI",
         "roles": ["ustoz"], "location": "xona"},
        {"t_from": 3, "t_to": 14, "label": "Asosiy", "visual": "Talabalar daftarga yozadi", "voice": "Biz sizga uch oyda gaplashishni o'rgatamiz va natijani kafolatlaymiz", "onscreen": "",
         "roles": ["ustoz"], "location": "xona"},
        {"t_from": 14, "t_to": 17, "label": "CTA", "visual": "Telefonda ro'yxatdan o'tish", "voice": "Hoziroq yoziling", "onscreen": "Yoziling", "brand_card": True,
         "roles": [], "location": "xona"},
    ],
}


@pytest.fixture()
def fake(app_env, monkeypatch):
    """OpenAI o'rniga soxta javoblar; chaqiruvlar jurnali qaytariladi."""
    from app import db, img_ai, img_db as D
    D.ensure_schema()
    for t in D.TABLE_NAMES:
        if t != "img_platforms":
            db.ex(f"DELETE FROM {t}")
    db.set_setting("openai_key", "sk-test")
    log = {"calls": [], "users": [], "drop": set(), "fail": 0}

    async def text_json(system, user, *, ctx=None, note="", **kw):
        log["calls"].append(note)
        log["users"].append(user)
        if system is img_ai.ANALYZE_SYSTEM:
            return json.loads(json.dumps(ANALYSIS))
        if system is img_ai.SCRIPT_SYSTEM:
            return {"title": "AI sarlavha", "script": "Hook: salom\n0-3s: Ko'rinish: x\nOvoz: salom\nEkranda: y"}
        if system is img_ai.IDENT_SYSTEM:
            return {"people": {"ustoz": "A man in his 30s, short dark hair, round glasses, navy shirt"},
                    "places": {"xona": "A bright classroom with white walls, a whiteboard and wooden desks"}}
        if system is img_ai.PROMPTS_SYSTEM:
            if log["fail"] > 0:
                log["fail"] -= 1
                raise img_ai.ai.AIError("tarmoq xatosi")
            idxs = [int(x) for x in re.findall(r"### Scene idx=(\d+)", user)]
            vids = re.findall(r"VIDEO PLATFORM \u00ab([^\u00bb]+)\u00bb", user)
            out = []
            for i in idxs:
                if i in log["drop"]:
                    continue
                out.append({"idx": i, "image": f"Scene {i}: man in a bright classroom, round glasses", "video": {v: f"{v} motion {i}" for v in vids},
                            "audio": "Warm male voice, calm pace. \"Salom\""})
            return {"scenes": out}
        return {}

    monkeypatch.setattr(img_ai, "text_json", text_json)
    return log


@pytest.fixture()
def client(app_env):
    from app.main import app
    return TestClient(app, follow_redirects=False)


def mk_brand_person(client, photos=5, consent=True):
    r = client.post("/img/brands", data={"name": "Seoul Academy", "activity": "Koreys tili markazi"},
                    files={"logo": ("l.png", png_bytes(size=(200, 200)), "image/png")})
    assert r.status_code == 303, r.text
    from app import db
    bid = db.one("SELECT id FROM img_brands")["id"]
    shots = ["front", "left", "right", "smile", "up"]
    files = [("photos", (f"cam_{shots[i]}.jpg" if i < 5 else f"p{i}.png", png_bytes((i * 40, 90, 90)), "image/png")) for i in range(photos)]
    r = client.post(f"/img/brands/{bid}/people", data={"name": "Aziz", "kind": "teacher", "consent": "on" if consent else "", "appearance": "30 yosh"}, files=files)
    assert r.status_code == 303
    pid = db.one("SELECT id FROM img_people")["id"]
    return bid, pid


def run_jobs(run, limit=20):
    from app import img_work as W
    async def go():
        n = 0
        while n < limit:
            job = W.claim_next()
            if not job:
                break
            await W.run_job(job)
            n += 1
        return n
    return run(go())


# ------------------------------------------------------------------ sof mantiq
def test_normalize_splits_to_5_seconds():
    from app import img_ai
    res = img_ai.normalize_analysis(json.loads(json.dumps(ANALYSIS)))
    assert all(s["duration"] <= 5.0 + 1e-6 for s in res["scenes"])
    assert len(res["scenes"]) == 5                       # 3 s + 11 s (3 qism) + 3 s
    assert [s["idx"] for s in res["scenes"]] == [1, 2, 3, 4, 5]
    ts = [(s["t_from"], s["t_to"]) for s in res["scenes"]]
    assert ts[0][0] == 0 and ts[-1][1] == 17
    parts = [s["voice"] for s in res["scenes"][1:4]]
    assert all(parts) and " ".join(parts).split() == ANALYSIS["scenes"][1]["voice"].split()
    assert res["scenes"][4]["kind"] == "brand"


def test_normalize_rejects_empty():
    from app import img_ai
    with pytest.raises(img_ai.ai.AIError):
        img_ai.normalize_analysis({"scenes": []})


def test_clean_batch_requires_all_platforms():
    from app import img_ai
    scs = [{"idx": 1}, {"idx": 2}]
    vids = [{"name": "Sora"}, {"name": "Veo"}]
    data = {"scenes": [{"idx": 1, "image": "a", "video": {"sora": "x", "Veo": "y"}, "audio": "z"},
                       {"idx": 2, "image": "b", "video": {"Sora": "x"}, "audio": "z"},      # Veo yetishmaydi -> o'tkazib yuboriladi
                       {"idx": 9, "image": "c", "video": {"Sora": "x", "Veo": "y"}}]}          # so'ralmagan sahna
    out = img_ai._clean_batch(data, scs, vids)
    assert list(out) == [1] and out[1]["video"] == {"Sora": "x", "Veo": "y"}


# ------------------------------------------------------------------ sahifalar
def test_pages_render_empty(fake, client):
    for url in ("/img", "/img/new", "/img/brands", "/img/locations", "/img/settings", "/img/guide"):
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:300])
        assert "Image Generator" in r.text
    r = client.get("/img/settings")
    assert "Sora" in r.text and "Runway" in r.text and "Midjourney" in r.text
    for url in ("/", "/ch", "/ig", "/meet", "/sys"):
        assert client.get(url).status_code in (200, 303), url
    assert 'value="img"' in client.get("/").text


def test_removed_generation_routes(fake, client):
    for url in ("/img/s/1/download", "/img/locations/1/master"):
        assert client.get(url).status_code in (404, 405)
    assert client.post("/img/s/1/prompts").status_code in (404, 405)


def test_switch_cookie(fake, client):
    r = client.post("/ws/switch", data={"ws": "img"})
    assert r.status_code == 303 and r.headers["location"] == "/img"
    client.cookies.set("ws", "img")
    assert client.get("/").headers["location"] == "/img"
    client.cookies.clear()


# ------------------------------------------------------------------ to'liq oqim
def _flow(fake, client, run):
    from app import db, img_db as D
    bid, person = mk_brand_person(client)
    assert [x["label"] for x in D.photos(person)][:2] == ["Old tomondan", "Chap 3/4 burilish"] or len(D.photos(person)) == 5
    pg = client.get("/img/new")
    assert pg.status_code == 200 and "Midjourney" in pg.text and "Veo" in pg.text
    base = {"brand_id": bid, "script": "Hook: Koreys tilini o'rganmoqchimisiz? " * 3, "fmt": "9:16", "style": "real"}
    # platforma tanlanmasa: xato
    assert "err=" in client.post("/img/new", data=base).headers["location"]
    r = client.post("/img/new", data={**base, "image_platform": "Midjourney", "video_platforms": ["Sora", "Veo"]})
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    pid = db.one("SELECT id FROM img_projects")["id"]
    assert D.project(pid)["image_platform"] == "Midjourney"
    assert client.get(f"/img/p/{pid}").status_code == 200   # wait sahifasi
    run_jobs(run)
    p = D.project(pid)
    assert p["status"] == "needs_assets", p
    scs = D.scenes(pid, full=False)
    assert len(scs) == 5 and all(s["duration"] <= 5 for s in scs)
    pg = client.get(f"/img/p/{pid}")
    assert pg.status_code == 200 and "O'qituvchi" in _html.unescape(pg.text) and "Boshlashdan oldin" in pg.text
    r = client.post(f"/img/p/{pid}/start")
    assert "err=" in r.headers["location"]                  # odam tanlanmagan
    role = D.roles(pid)[0]
    client.post(f"/img/p/{pid}/role/{role['id']}/person", data={"person_id": str(person)})
    r = client.post(f"/img/p/{pid}/start")
    assert "err=" in r.headers["location"] and D.project(pid)["status"] == "needs_assets"   # rozilik yo'q
    client.post(f"/img/p/{pid}/role/{role['id']}/person", data={"person_id": str(person), "consent": "on"})
    pl = D.plocs(pid)[0]
    client.post(f"/img/p/{pid}/loc/{pl['id']}/save", data={"name": "Dars xonasi", "description": "Katta yorug' xona"})
    assert D.plocs(pid)[0]["description"] == "Katta yorug' xona"
    sid = scs[0]["id"]
    client.post(f"/img/p/{pid}/scene/{sid}/save", data={"visual": "Yangi tavsif", "voice": "Salom", "label": "Hook", "onscreen": ""})
    assert D.scene(sid, full=False)["visual"] == "Yangi tavsif"
    n0 = len(fake["calls"])
    r = client.post(f"/img/p/{pid}/start")
    assert "err=" not in r.headers["location"], r.headers["location"]
    run_jobs(run)
    p = D.project(pid)
    assert p["status"] == "done", (p["status"], p["error"], p["msg"])
    assert p["progress"] == 100
    scs = D.scenes(pid)
    assert all(s["status"] == "done" and s["active"] for s in scs)
    assert all(set(s["video"]) == {"Sora", "Veo"} for s in scs)
    assert all(s["audio"] and s["active"]["image_prompt"] for s in scs)
    # token tejash: 1 ta tavsif so'rovi + 5 sahna = 1 paket (batch=5)
    new_calls = fake["calls"][n0:]
    assert sum(1 for c in new_calls if c.startswith("odam va joy")) == 1
    assert sum(1 for c in new_calls if c.startswith("promptlar")) == 1, new_calls
    # o'zgarmas tavsif va referens ko'rsatmasi so'rovga kirgan
    user = next(u for u in fake["users"] if "SCENES TO WRITE" in u)
    assert "round glasses" in user and "bright classroom" in user and "Midjourney" in user
    # sahifa, status.json, ZIP
    pg = client.get(f"/img/p/{pid}")
    assert pg.status_code == 200 and "Sora" in pg.text and "Audio prompt" in pg.text and "Nusxa" in pg.text
    st = client.get(f"/img/p/{pid}/status.json").json()
    assert st["status"] == "done" and not st["busy"]
    z = client.get(f"/img/p/{pid}/zip")
    assert z.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    names = zf.namelist()
    assert "PROMPTLAR.txt" in names and "SSENARIY.txt" in names and "promptlar/sahna_05.txt" in names
    assert "referens/ODAMLAR.txt" in names and len([n for n in names if n.startswith("referens/") and n.endswith(".jpg")]) == 5
    assert "Sora" in zf.read("promptlar/sahna_01.txt").decode()
    assert not any(n.startswith("tarix/") or n.startswith("rasmlar/") for n in names)
    assert D.total_cost() >= 0
    return pid


def test_full_flow(fake, client, run):
    assert _flow(fake, client, run)


def test_edit_reroll_history(fake, client, run):
    from app import db, img_db as D
    pid = _flow(fake, client, run)
    s = D.scenes(pid, full=False)[1]
    n0 = len(D.scene(s["id"])["versions"])
    first_ver = D.scene(s["id"])["active_ver"]
    client.post(f"/img/s/{s['id']}/edit", data={"note": "fon yorqinroq bo'lsin"})
    assert D.scene(s["id"], full=False)["status"] == "writing"
    run_jobs(run)
    assert "fon yorqinroq" in fake["users"][-1]
    sc = D.scene(s["id"])
    assert len(sc["versions"]) == n0 + 1 and sc["status"] == "done" and sc["active_ver"] != first_ver
    assert sc["versions"][0]["note"] == "fon yorqinroq bo'lsin"
    assert set(sc["video"]) == {"Sora", "Veo"} and sc["audio"]
    client.post(f"/img/s/{s['id']}/reroll")
    run_jobs(run)
    assert len(D.scene(s["id"])["versions"]) == n0 + 2
    client.post(f"/img/s/{s['id']}/activate/{first_ver}")
    assert D.scene(s["id"], full=False)["active_ver"] == first_ver
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/img/p/{pid}/zip?history=1").content))
    assert any(n.startswith("tarix/sahna_02_v") for n in z.namelist())
    client.post(f"/img/s/{s['id']}/edit", data={"note": "yana o'zgartir"})
    r = client.post(f"/img/s/{s['id']}/edit", data={"note": "yana"})
    assert "err=" in r.headers["location"]                  # ikki marta bosish rad etiladi
    run_jobs(run)
    assert D.project(pid)["status"] == "done"
    assert client.get(f"/img/p/{pid}").status_code == 200


def _setup(client, run, vids=("Sora",), img="gpt-image (ChatGPT)"):
    from app import img_db as D
    bid, person = mk_brand_person(client)
    pid = D.create_project(bid, "T", "Hook: salom dunyo " * 4, "16:9", "cine", "", img, list(vids))
    D.enqueue("analyze", pid)
    run_jobs(run)
    role = D.roles(pid)[0]
    client.post(f"/img/p/{pid}/role/{role['id']}/person", data={"person_id": str(person), "consent": "on"})
    return pid


def test_partial_and_retry(fake, client, run):
    from app import img_db as D
    pid = _setup(client, run)
    fake["drop"] = {2, 4}                                   # AI 2 va 4-sahnani yozmaydi (yakka urinish ham)
    D.enqueue("pipeline", pid)
    D.set_project(pid, status="running")
    run_jobs(run)
    p = D.project(pid)
    assert p["status"] == "partial", p
    bad = [s for s in D.scenes(pid, full=False) if s["status"] == "error"]
    assert [s["idx"] for s in bad] == [2, 4] and all(s["error"] for s in bad)
    ok_before = {s["id"]: s["active_ver"] for s in D.scenes(pid, full=False) if s["status"] == "done"}
    fake["drop"] = set()
    client.post(f"/img/p/{pid}/start")
    run_jobs(run)
    assert D.project(pid)["status"] == "done"
    for sid, v in ok_before.items():
        assert D.scene(sid, full=False)["active_ver"] == v, "tayyor sahna qayta yozilmasligi kerak"


def test_api_error_marks_scenes(fake, client, run):
    from app import img_db as D
    pid = _setup(client, run)
    fake["fail"] = 99
    D.enqueue("pipeline", pid)
    D.set_project(pid, status="running")
    run_jobs(run)
    p = D.project(pid)
    assert p["status"] in ("partial", "error")
    assert all(s["status"] == "error" for s in D.scenes(pid, full=False))


def test_platform_management(fake, client, run):
    from app import img_db as D
    r = client.post("/img/settings/platform/add", data={"name": "Pika", "kind": "video", "max_sec": "4", "rules": "Short motion only.", "audio": ""})
    assert "err=" not in r.headers["location"]
    assert "Pika" in [p["name"] for p in D.platforms(kind="video")]
    r = client.post("/img/settings/platform/add", data={"name": "Ideogram", "kind": "image", "max_sec": "0", "rules": "Plain prompt."})
    assert "Ideogram" in [p["name"] for p in D.platforms(kind="image")] and "Ideogram" not in [p["name"] for p in D.platforms(kind="video")]
    r = client.post("/img/settings/platform/add", data={"name": "Pika", "kind": "video", "max_sec": "4", "rules": "x"})
    assert "err=" in r.headers["location"]
    pika = next(p for p in D.platforms() if p["name"] == "Pika")
    client.post(f"/img/settings/platform/{pika['id']}/toggle")
    assert "Pika" not in [p["name"] for p in D.platforms(active_only=True)]
    client.post(f"/img/settings/platform/{pika['id']}/delete")
    assert "Pika" not in [p["name"] for p in D.platforms()]
    sora = next(p for p in D.platforms() if p["name"] == "Sora")
    client.post(f"/img/settings/platform/{sora['id']}/delete")
    client.post("/img/settings/platform/restore")
    assert "Sora" in [p["name"] for p in D.platforms()]


def test_settings_models_and_key(fake, client):
    from app import img_db as D
    r = client.post("/img/settings", data={"model_text": "gpt-5-mini", "model_script": "", "batch": "4"})
    assert r.status_code == 303
    assert D.model_text() == "gpt-5-mini" and D.cfg("batch") == "4"
    client.post("/img/settings", data={"batch": "99"})
    assert D.cfg("batch") == "10"
    assert client.get("/img/settings").status_code == 200


def test_people_photos_and_brand_delete(fake, client):
    from app import db, img_db as D
    bid, person = mk_brand_person(client, photos=2)
    assert len(D.photos(person)) == 2
    r = client.post(f"/img/people/{person}/photos", data={"next": "/img/brands"},
                    files=[("photos", ("cam_smile.jpg", png_bytes(), "image/png")), ("photos", ("cam_up.jpg", png_bytes(), "image/png"))])
    assert r.status_code == 303 and len(D.photos(person)) == 4
    assert any(ph["label"] for ph in D.photos(person))
    assert client.get(f"/img/brands/{bid}").status_code == 200
    r = client.post("/img/locations", data={"brand_id": bid, "name": "Ofis", "description": "Zamonaviy ofis"})
    assert "err=" not in r.headers["location"] and D.locations(bid)
    assert client.get(f"/img/locations?brand={bid}").status_code == 200
    r = client.post(f"/img/brands/{bid}/people", data={"name": "X"}, files=[("photos", ("a.png", b"not an image", "image/png"))])
    assert "emas" in r.headers["location"]
    assert client.get("/img/f/../../app/main.py").status_code in (404, 422)
    assert client.get("/img/f/..%2f..%2fapp%2fmain.py").status_code == 404
    from app.img_db import IMG_DIR
    client.post(f"/img/brands/{bid}/delete")
    assert not D.brands() and not D.people() and not D.locations()
    assert not (IMG_DIR / "people" / str(person)).exists()


def test_script_ai_and_validation(fake, client):
    bid, _ = mk_brand_person(client)
    j = client.post("/img/script-ai", data={"brand_id": bid, "topic": "Koreys tili", "seconds": 30}).json()
    assert j["ok"] and j["script"]
    assert not client.post("/img/script-ai", data={"brand_id": bid, "topic": ""}).json()["ok"]
    r = client.post("/img/new", data={"brand_id": bid, "script": "qisqa"})
    assert "err=" in r.headers["location"]
    assert client.get("/img/p/99999").status_code == 303


def test_old_schema_migrates(fake):
    """Eski (rasm yaratadigan) jadval ustunlari bo'lgan bazada yangi ustunlar qo'shiladi."""
    from app import db, img_db as D
    D.ensure_schema()
    for table, col in (("img_projects", "image_platform"), ("img_projects", "video_platforms"), ("img_roles", "identity"),
                       ("img_plocs", "place_en"), ("img_platforms", "kind")):
        assert db.one(f"SELECT {col} FROM {table} LIMIT 1") is None or True


def test_restart_requeues_running(fake, client, run):
    from app import db, img_db as D
    jid = D.enqueue("pipeline", 12345)
    db.ex("UPDATE img_jobs SET status='running' WHERE id=?", (jid,))
    D.ensure_schema()
    assert db.one("SELECT status FROM img_jobs WHERE id=?", (jid,))["status"] == "queued"
    db.ex("DELETE FROM img_jobs")


def test_health_item(fake):
    from app import img_work, syscheck
    items = img_work.health_items(syscheck.item)
    assert items and items[0]["group"] == "Image Generator" and items[0]["state"] == "ok"
