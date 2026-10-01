import asyncio
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


DB = os.environ.get("TGP_TEST_DB", "sqlite")
DATA = tempfile.mkdtemp(prefix="tgp_meet_")
MOCK_PORT = _free_port()
os.environ["TGP_DATA_DIR"] = DATA
os.environ["TGP_DB"] = DB
base = f"http://127.0.0.1:{MOCK_PORT}"
os.environ.update(TGP_ZOOM_API=base + "/v2", TGP_ZOOM_OAUTH=base + "/oauth/token", TGP_BOT_API=base)
if DB == "mysql":
    import pymysql
    c = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="", autocommit=True)
    c.cursor().execute("DROP DATABASE IF EXISTS telegram_group_post")
    c.close()


@pytest.fixture(scope="session")
def mock():
    import mock_zoom
    srv = mock_zoom.serve(MOCK_PORT)
    yield mock_zoom
    srv.should_exit = True


@pytest.fixture(scope="session")
def app_env(mock):
    from app import db
    db.init()
    from app import meet_sched
    # eski Google Meet jadvallarini ham yaratib qo'yamiz: ensure_schema ularni o'chirishi kerak
    db.ex("CREATE TABLE IF NOT EXISTS mt_teachers(id INTEGER PRIMARY KEY, name TEXT)") if DB != "mysql" else None
    meet_sched.ensure_schema()
    yield
    shutil.rmtree(DATA, ignore_errors=True)


@pytest.fixture()
def clean(app_env, mock):
    from app import db, meet_sched as S, meet_zoom as Z
    for t in ("zoom_alerts", "zoom_meetings", "zoom_lessons", "zoom_schedule", "zoom_groups", "zoom_teachers", "zoom_accounts", "zoom_settings"):
        db.ex(f"DELETE FROM {t}")
    Z._tokens.clear()
    mock.reset()
    yield S


@pytest.fixture(scope="session")
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


@pytest.fixture()
def run(loop):
    return lambda coro: loop.run_until_complete(coro)
