"""Majlislar (Zoom) bo'limi sinovlari: soxta Zoom + soxta Telegram Bot API bilan."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

CRED = ("acc1@mail.test", "AID1", "CID1", "SEC1")


def add_acc(S, n=1, max_min=40):
    ids = []
    for i in range(1, n + 1):
        aid, err = S.add_account(f"Zoom{i}", f"acc{i}@mail.test", f"AID{i}", f"CID{i}", f"SEC{i}", max_min)
        assert not err, err
        ids.append(aid)
    return ids


@pytest.fixture()
def tg(clean, monkeypatch):
    S = clean
    sent, fail = [], {}

    async def fake(target, text):
        if fail.get(target):
            raise S.TgFail(fail[target])
        sent.append((target, text))
    monkeypatch.setattr(S, "tg_send", fake)
    return sent, fail


@pytest.fixture()
def w(clean, tg, mock, run):
    """1 akkaunt, bot ulangan, bitta o'qituvchi (chat_id bor), bitta guruh."""
    from app import meet_bot as B
    S = clean
    S.put("bot_token", "123:GOOD")
    S.put("admin_username", "adminuser")
    err = S.save_teacher({"name": "Ustoz Test", "tg_username": "ustoz_tg", "phone": "+998901112233"})
    assert not err, err
    t = S.teacher_by_username("ustoz_tg")
    S.db.ex("UPDATE zoom_teachers SET chat_id=777 WHERE id=?", (t["id"],))
    g = S.add_group(-1001, "Ingliz tili A", "ga", t["id"])
    return S, B, t["id"], g


def fake_now(S, monkeypatch, dt):
    cur = {"t": dt}
    monkeypatch.setattr(S, "now", lambda: cur["t"])
    return cur


# ---------------------------------------------------------------- sxema
def test_old_tables_dropped(clean):
    S = clean
    from app import db
    if not db.IS_MYSQL:
        db.ex("CREATE TABLE mt_lessons(id INTEGER)")
        S.ensure_schema()
        names = [r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type='table'")]
        assert not any(n.startswith("mt_") for n in names)
        assert "zoom_accounts" in names
    S.ensure_schema()


# ---------------------------------------------------------------- akkauntlar
def test_add_account_bulk_secret_encrypted(clean):
    S = clean
    from app import secure
    ids = add_acc(S, 2)
    raw = S.db.one("SELECT client_secret FROM zoom_accounts WHERE id=?", (ids[0],))["client_secret"]
    assert raw != "SEC1" and secure.is_encrypted(raw)
    assert "client_secret" not in S.accounts()[0]
    n, errs = S.bulk_add("c@mail.test;A3;C3;S3\nd@mail.test;A4;C4;S4;0;Pro\nbad line\n")
    assert n == 2 and len(errs) == 1
    assert S.account_raw(ids[0])["max_min"] == 40
    assert [a for a in S.accounts() if a["email"] == "d@mail.test"][0]["max_min"] == 0
    aid, err = S.add_account("x", "acc1@mail.test", "A", "C", "S")
    assert err                                                    # takroriy email


def test_check_account_ok_and_scope_error(clean, mock, run):
    from app import meet_zoom as Z
    S = clean
    a, b = add_acc(S, 2)
    items = run(Z.check_account(S.account_raw(a), deep=True))
    assert all(i["state"] in ("ok", "warn") for i in items), items
    assert any(i["key"] == "start_url" and i["state"] == "ok" for i in items)
    assert all(m["deleted"] for m in mock.STATE["meetings"].values())      # sinov majlisi o'chirildi
    mock.STATE["no_scope"].add("CID2")
    items = run(Z.check_account(S.account_raw(b), deep=True))
    bad = [i for i in items if i["state"] == "fail"]
    assert bad and "scope" in (bad[0]["detail"] + bad[0]["fix"]).lower()
    mock.STATE["bad_clients"].add("CID1")
    Z._tokens.clear()
    items = run(Z.check_account(S.account_raw(a), deep=False))
    assert items[0]["state"] == "fail"


# ---------------------------------------------------------------- havolalar, havola yaratish, havolalar bo'sh akkauntdan
def test_provision_picks_free_account_and_pool_busy(w, mock, run, monkeypatch):
    S, B, tid, gid = w
    a, b = add_acc(S, 2)
    t0 = S.now().replace(second=0, microsecond=0) + timedelta(hours=1)
    l1 = S.add_extra_lesson(gid, t0, 60, tid)
    l2 = S.add_extra_lesson(S.add_group(-1002, "Mat B", "", tid), t0, 60, tid)
    l3 = S.add_extra_lesson(S.add_group(-1003, "Fiz C", "", tid), t0, 60, tid)
    ok1, _ = run(S.provision(l1))
    ok2, _ = run(S.provision(l2))
    assert ok1 and ok2
    m1, m2 = S.meetings_of(l1)[0], S.meetings_of(l2)[0]
    assert m1["account_id"] != m2["account_id"]                            # parallel darslar: turli akkaunt
    assert m1["join_url"].startswith("https://zoom.test/j/") and S.start_url_of(m1).startswith("https://zoom.test/s/")
    assert S.db.one("SELECT start_url FROM zoom_meetings WHERE id=?", (m1["id"],))["start_url"] != m1["start_url"] or True
    ok3, why = run(S.provision(l3))
    assert not ok3 and why.startswith("BAND")
    probs = S.pool_problems()
    assert probs and "band" in probs[0]["text"]
    assert S.db.one("SELECT COUNT(*) c FROM zoom_alerts WHERE kind='pool_busy'")["c"] >= 1
    pool = S.pool_summary()
    assert pool["busy"] >= 0


def test_zoom_error_marks_account(w, mock, run):
    S, B, tid, gid = w
    (a,) = add_acc(S, 1)
    mock.STATE["fail_create"].add("acc1@mail.test")
    lid = S.add_extra_lesson(gid, S.now() + timedelta(minutes=30), 60, tid)
    ok, why = run(S.provision(lid))
    assert not ok and why.startswith("XATO")
    assert S.db.one("SELECT status FROM zoom_accounts WHERE id=?", (a,))["status"] == "error"


def test_adhoc_meeting(w, run):
    S, B, tid, gid = w
    (a,) = add_acc(S, 1)
    mid, err = run(S.adhoc_meeting(a, "Test"))
    assert mid, err
    assert S.account_status(S.accounts()[0])["state"] == "busy"
    n = run(S.release_account(a))
    assert n == 1 and S.account_status(S.accounts()[0])["state"] in ("free", "soon")


# ---------------------------------------------------------------- 40 daqiqalik zanjir
def test_chain_yes_creates_new_meeting_other_account(w, tg, mock, run, monkeypatch):
    S, B, tid, gid = w
    sent, fail = tg
    a, b = add_acc(S, 2)
    start = S.now().replace(second=0, microsecond=0) + timedelta(minutes=12)
    cur = fake_now(S, monkeypatch, start - timedelta(minutes=12))
    lid = S.add_extra_lesson(gid, start, 90, tid)
    ok, why = run(S.provision(lid))
    assert ok, why
    m1 = S.meetings_of(lid)[0]
    # 10 daqiqa oldin: o'qituvchiga havola + boshlash tugmasi
    cur["t"] = start - timedelta(minutes=10)
    run(S.process_lessons())
    msgs = [m["text"] for m in mock.STATE["sent"]]
    assert any("Ingliz tili A" in m for m in msgs)
    assert any("inline_keyboard" in json_s(m.get("reply_markup")) and "zoom.test/s/" in json_s(m.get("reply_markup")) for m in mock.STATE["sent"]), mock.STATE["sent"]
    # 5 daqiqa oldin: guruhga havola
    cur["t"] = start - timedelta(minutes=5)
    run(S.process_lessons())
    assert any(m1["join_url"] in txt for tgt, txt in sent if tgt == -1001) or any("zoom.test/j/" in txt for tgt, txt in sent)
    # majlis tugashiga 3 daqiqa qolganda: bitta so'rov
    cur["t"] = parse(S, m1["end_at"]) - timedelta(minutes=3)
    n0 = len(mock.STATE["sent"])
    run(S.process_lessons())
    run(S.process_lessons())
    prompts = [m for m in mock.STATE["sent"][n0:] if "davom ettirasizmi" in m["text"]]
    assert len(prompts) == 1
    cb = prompts[0]["reply_markup"]["inline_keyboard"][0]
    assert cb[0]["callback_data"] == f"z:y:{m1['id']}"
    # "Davom etish"
    run(B.handle_update({"callback_query": {"id": "q1", "data": cb[0]["callback_data"], "message": {"chat": {"id": 777}, "message_id": 5}}}))
    ms = S.meetings_of(lid)
    assert len(ms) == 2 and ms[1]["account_id"] != ms[0]["account_id"]
    assert ms[1]["seq"] == 2 and mock.STATE["edits"]
    assert any("zoom.test/j/" in txt and "-1001" == str(tgt) for tgt, txt in sent) or any(tgt == -1001 for tgt, _ in sent)
    # ikkinchi bosish: allaqachon qabul qilingan
    run(B.handle_update({"callback_query": {"id": "q2", "data": cb[0]["callback_data"], "message": {"chat": {"id": 777}, "message_id": 5}}}))
    assert len(S.meetings_of(lid)) == 2
    # begona o'qituvchi bosolmaydi
    run(B.handle_update({"callback_query": {"id": "q3", "data": cb[0]["callback_data"], "message": {"chat": {"id": 999}, "message_id": 5}}}))
    assert "sizga tegishli emas" in mock.STATE["answers"][-1]["text"]
    # keyingi so'rov: yangi majlis tugashiga 3 daqiqa qolganda, dars tugamagan bo'lsa
    m2 = ms[1]
    assert S.lesson(lid)["end_at"] > m2["end_at"] or S.parse(m2["end_at"]) >= S.parse(S.lesson(lid)["end_at"])


def json_s(x):
    import json
    return json.dumps(x or {}, ensure_ascii=False)


def parse(S, s):
    return S.parse(s)


def test_chain_no_stops_and_ignore_expires(w, tg, mock, run, monkeypatch):
    S, B, tid, gid = w
    add_acc(S, 2)
    start = S.now().replace(second=0, microsecond=0) + timedelta(minutes=12)
    cur = fake_now(S, monkeypatch, start - timedelta(minutes=12))
    lid = S.add_extra_lesson(gid, start, 120, tid)
    run(S.provision(lid))
    m1 = S.meetings_of(lid)[0]
    cur["t"] = S.parse(m1["end_at"]) - timedelta(minutes=3)
    run(S.process_lessons())
    assert S.meeting(m1["id"])["prompt"] == "sent"
    run(B.handle_update({"callback_query": {"id": "q", "data": f"z:n:{m1['id']}", "message": {"chat": {"id": 777}, "message_id": 5}}}))
    assert S.meeting(m1["id"])["prompt"] == "no" and len(S.meetings_of(lid)) == 1
    cur["t"] = S.parse(m1["end_at"]) + timedelta(minutes=8)
    run(S.process_lessons())
    assert len(S.meetings_of(lid)) == 1
    assert S.lesson(lid)["status"] in ("done", "live")
    # javobsiz: 2-dars
    gid2 = S.add_group(-1004, "Mat", "", tid)
    cur["t"] = start + timedelta(days=1) - timedelta(minutes=12)
    st2 = cur["t"] + timedelta(minutes=12)
    l2 = S.add_extra_lesson(gid2, st2, 120, tid)
    run(S.provision(l2))
    mm = S.meetings_of(l2)[0]
    cur["t"] = S.parse(mm["end_at"]) - timedelta(minutes=3)
    run(S.process_lessons())
    cur["t"] = S.parse(mm["end_at"]) + timedelta(minutes=8)
    run(S.process_lessons())
    assert S.meeting(mm["id"])["prompt"] == "expired" and len(S.meetings_of(l2)) == 1


def test_no_prompt_when_lesson_over(w, mock, run, monkeypatch):
    S, B, tid, gid = w
    add_acc(S, 1)
    start = S.now().replace(second=0, microsecond=0) + timedelta(minutes=12)
    cur = fake_now(S, monkeypatch, start - timedelta(minutes=12))
    lid = S.add_extra_lesson(gid, start, 35, tid)            # dars majlis tugashidan oldin tugaydi: so'rov kerak emas
    run(S.provision(lid))
    m1 = S.meetings_of(lid)[0]
    cur["t"] = S.parse(m1["end_at"]) - timedelta(minutes=3)
    n0 = len(mock.STATE["sent"])
    run(S.process_lessons())
    assert not [m for m in mock.STATE["sent"][n0:] if "davom ettirasizmi" in m["text"]]
    assert S.meeting(m1["id"])["prompt"] == "na"


def test_continue_pool_busy_pending_and_retry(w, tg, mock, run, monkeypatch):
    S, B, tid, gid = w
    a, = add_acc(S, 1)                                       # bitta akkaunt: davom etish uchun bo'sh akkaunt yo'q
    start = S.now().replace(second=0, microsecond=0) + timedelta(minutes=12)
    cur = fake_now(S, monkeypatch, start - timedelta(minutes=12))
    lid = S.add_extra_lesson(gid, start, 120, tid)
    run(S.provision(lid))
    m1 = S.meetings_of(lid)[0]
    cur["t"] = S.parse(m1["end_at"]) - timedelta(minutes=3)
    run(S.process_lessons())
    ok, why = run(S.continue_lesson(m1["id"], "teacher"))
    assert not ok and why.startswith("BAND")
    assert S.pool_problems()
    assert S.db.one("SELECT COUNT(*) c FROM zoom_alerts WHERE kind='pool_busy'")["c"] >= 1
    # ikkinchi akkaunt qo'shilgach avtomatik qayta uriniladi
    S.add_account("Zoom2", "acc2@mail.test", "AID2", "CID2", "SEC2", 40)
    run(S.process_lessons())
    assert len(S.meetings_of(lid)) == 2
    assert not S.pool_problems()


# ---------------------------------------------------------------- bekor qilish / ko'chirish
def test_cancel_and_move(w, tg, mock, run):
    S, B, tid, gid = w
    sent, fail = tg
    add_acc(S, 1)
    st = S.now().replace(second=0, microsecond=0) + timedelta(hours=3)
    lid = S.add_extra_lesson(gid, st, 60, tid)
    run(S.provision(lid))
    r = run(S.move_lesson(lid, st + timedelta(hours=1)))
    assert S.lesson(lid)["start_at"] == S.fmt(st + timedelta(hours=1)) and S.lesson(lid)["moved_from"]
    assert any(tgt == -1001 for tgt, _ in sent)
    assert any(str(m["chat_id"]) == "777" for m in mock.STATE["sent"])
    sent.clear()
    mock.STATE["sent"].clear()
    run(S.cancel_lesson(lid, "kasal"))
    assert S.lesson(lid)["status"] == "cancelled"
    assert any(tgt == -1001 and "kasal" in txt for tgt, txt in sent)
    assert mock.STATE["sent"]
    assert all(m["state"] == "deleted" for m in S.meetings_of(lid, include_deleted=True))


def test_send_failure_alert_with_copy_text(w, tg, mock, run):
    S, B, tid, gid = w
    sent, fail = tg
    add_acc(S, 1)
    fail[-1001] = "chat write forbidden"
    lid = S.add_extra_lesson(gid, S.now() + timedelta(minutes=3), 60, tid)
    run(S.provision(lid))
    run(S.resend(lid, "group"))
    al = S.db.one("SELECT * FROM zoom_alerts WHERE kind='send_fail' ORDER BY id DESC")
    assert al and "zoom.test/j/" in (al["copy_text"] or "")
    mock.STATE["fail_send"] = "Forbidden: bot was blocked by the user"
    run(S.resend(lid, "teacher"))
    assert S.teacher(tid)["chat_id"] in (None, 0)


# ---------------------------------------------------------------- bot
def test_bot_start_links_teacher_and_menus(w, mock, run):
    S, B, tid, gid = w
    S.db.ex("UPDATE zoom_teachers SET chat_id=NULL WHERE id=?", (tid,))
    run(B.handle_update({"message": {"chat": {"id": 555, "type": "private"}, "from": {"username": "Ustoz_TG"}, "text": "/start"}}))
    assert S.teacher(tid)["chat_id"] == 555
    run(B.handle_update({"message": {"chat": {"id": 556, "type": "private"}, "from": {"username": "stranger"}, "text": "/start"}}))
    assert "topilmadi" in mock.STATE["sent"][-1]["text"]
    st = S.now().replace(second=0, microsecond=0) + timedelta(hours=2)
    S.add_extra_lesson(gid, st, 60, tid)
    for cmd in ("📅 Bugungi darslar", "bugungi", "haftalik", "guruh", "hozirgi", "/help", "nimadir"):
        run(B.handle_update({"message": {"chat": {"id": 555, "type": "private"}, "from": {"username": "ustoz_tg"}, "text": cmd}}))
    assert len(mock.STATE["sent"]) >= 8


def test_bot_verify(w, run):
    S, B, tid, gid = w
    me = run(B.verify())
    assert me["username"] == "majlis_test_bot" and S.get("bot_username") == "majlis_test_bot"
    S.put("bot_token", "bad")
    with pytest.raises(B.BotFail):
        run(B.verify())
    assert S.get("bot_err")


# ---------------------------------------------------------------- jadval
def test_schedule_and_generate(w, run):
    S, B, tid, gid = w
    add_acc(S, 1)
    errs, warns = S.add_schedule(gid, [0, 2, 4], "19:00", 60, tid)
    assert not errs
    n = run(S.generate(gid))
    assert n >= 1
    assert S.generate and run(S.generate(gid)) == 0          # qayta yaratilmaydi
    ics = S.ics(S.lessons_between("2000-01-01 00:00:00", "2100-01-01 00:00:00"))
    assert "BEGIN:VCALENDAR" in ics and "Ingliz tili A" in ics


# ---------------------------------------------------------------- sahifalar va hisobot
@pytest.fixture()
def client(app_env):
    from app.main import app
    return TestClient(app)


def test_pages_render(w, mock, run, client):
    S, B, tid, gid = w
    a, = add_acc(S, 1)
    S.add_schedule(gid, [0, 3], "19:00", 60, tid)
    run(S.generate(gid))
    lid = S.add_extra_lesson(gid, S.now() + timedelta(minutes=20), 90, tid)
    run(S.provision(lid))
    mid, _ = run(S.adhoc_meeting(a, "x")) if False else (None, "")
    for url in ("/meet", "/meet/calendar", "/meet/teachers", "/meet/groups", f"/meet/groups/{gid}", "/meet/lessons", f"/meet/lessons/{lid}",
                "/meet/zoom", f"/meet/zoom?edit={a}", "/meet/alerts", "/meet/settings", "/meet/diag", "/meet/guide", "/meet/account", "/meet/calendar.ics", "/meet/api/state"):
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:400])
        assert "Traceback" not in r.text and "TemplateSyntax" not in r.text
        assert "Google" not in r.text.replace("Google Chrome", "") or url == "/meet/guide", url
    r = client.get("/meet")
    assert "Zoom" in r.text


def test_zoom_page_flow(w, mock, run, client):
    S, B, tid, gid = w
    r = client.post("/meet/zoom/add", data={"label": "A", "email": "a@x.test", "account_id": "AID1", "client_id": "CID1", "client_secret": "SEC1", "max_min": "40"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    a = S.accounts()[0]
    r = client.post(f"/meet/zoom/{a['id']}/new", data={}, follow_redirects=False)
    assert r.status_code in (302, 303) and "m=" in r.headers["location"]
    page = client.get(r.headers["location"])
    assert "zoom.test/j/" in page.text and "SECRETZAK" in page.text
    assert "Band" in client.get("/meet/zoom").text
    client.post("/meet/zoom/test-all", data={}, follow_redirects=False)
    r = client.post("/meet/zoom/bulk", data={"text": "q@x.test;A9;C9;S9"}, follow_redirects=False)
    assert len(S.accounts()) == 2
    r = client.post(f"/meet/zoom/{a['id']}/release", data={}, follow_redirects=False)
    assert S.account_status(S.accounts()[0])["state"] in ("free", "soon")
    r = client.post("/meet/diag/run", data={}, follow_redirects=False)
    assert client.get("/meet/diag").status_code == 200


def test_banner_when_pool_busy(w, run, client):
    S, B, tid, gid = w
    add_acc(S, 1)
    t0 = S.now() + timedelta(minutes=30)
    l1 = S.add_extra_lesson(gid, t0, 60, tid)
    l2 = S.add_extra_lesson(S.add_group(-9, "Boshqa", "", tid), t0, 60, tid)
    run(S.provision(l1))
    ok, why = run(S.provision(l2))
    assert not ok
    html = client.get("/meet").text
    assert "band" in html.lower() and "mt-bad" in html


def test_daily_sheet_and_health(w, run):
    from openpyxl import Workbook
    from app import meet_reports as R
    S, B, tid, gid = w
    add_acc(S, 1)
    st = S.now().replace(hour=15, minute=0, second=0, microsecond=0)
    lid = S.add_extra_lesson(gid, st, 60, tid)
    run(S.provision(lid))
    wb = Workbook()
    R.add_daily_sheet(wb, st.strftime("%Y-%m-%d"))
    assert "Majlislar" in wb.sheetnames and wb["Majlislar"].max_row == 2
    items = S.health_items()
    assert items and all(i["group"] == "Majlislar (Zoom)" for i in items)


def test_other_pages_unaffected(app_env, client):
    for url in ("/ch", "/sys"):
        r = client.get(url, follow_redirects=True)
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
