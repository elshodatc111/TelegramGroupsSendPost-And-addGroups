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
os.environ.update(TGP_GOOGLE_TOKEN=base + "/token", TGP_GOOGLE_USERINFO=base + "/userinfo", TGP_GOOGLE_MEET=base + "/meet",
                  TGP_GOOGLE_CAL=base + "/cal", TGP_GOOGLE_AUTH=base + "/auth")
if DB == "mysql":
    import pymysql
    c = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="", autocommit=True)
    c.cursor().execute("DROP DATABASE IF EXISTS telegram_group_post")
    c.close()


@pytest.fixture(scope="session")
def mock():
    import mock_google
    srv = mock_google.serve(MOCK_PORT)
    yield mock_google
    srv.should_exit = True


@pytest.fixture(scope="session")
def app_env(mock):
    from app import db
    db.init()
    from app import meet_sched
    meet_sched.ensure_schema()
    yield
    shutil.rmtree(DATA, ignore_errors=True)


@pytest.fixture()
def clean(app_env, mock):
    """Har sinovdan oldin Majlislar jadvallari va soxta Google holati tozalanadi."""
    from app import db, meet_sched as S
    for t in ("mt_attendance", "mt_lessons", "mt_schedule", "mt_groups", "mt_teachers", "mt_alerts", "mt_settings"):
        db.ex(f"DELETE FROM {t}")
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
