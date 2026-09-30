"""MySQL zaxira nusxasi: har kuni bir marta data/backups/ ga (oxirgi 7 tasi saqlanadi).

mysqldump (XAMPP bilan keladi) bo'lsa o'shani ishlatadi, bo'lmasa Python orqali SQL fayl yozadi.
Tiklash: mysql -u root telegram_group_post < fayl.sql  (gz bo'lsa avval ochiladi)
"""
import asyncio
import gzip
import os
import shutil
import subprocess
import sys
from datetime import datetime

import pymysql

from . import db
from .config import BACKUP_DIR, log
from .xampp import find_xampp


def _dump_exe(conf):
    x = find_xampp(conf.get("xampp_dir") or None)
    names = ["mysqldump.exe", "mariadb-dump.exe"] if sys.platform == "win32" else ["mysqldump", "mariadb-dump"]
    if x:
        for n in names:
            p = x / "mysql" / "bin" / n
            if p.exists():
                return str(p)
    for n in ("mysqldump", "mariadb-dump"):
        w = shutil.which(n)
        if w:
            return w
    return None


def _python_dump(conf, out_path):
    from . import mysqldb
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        f.write("SET FOREIGN_KEY_CHECKS=0;\nSET NAMES utf8mb4;\n")
        conn = mysqldb.pool.get()
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES")
                tables = [list(r.values())[0] for r in cur.fetchall()]
                for t in tables:
                    cur.execute(f"SHOW CREATE TABLE `{t}`")
                    f.write(f"DROP TABLE IF EXISTS `{t}`;\n{cur.fetchone()['Create Table']};\n")
                    cur.execute(f"SELECT * FROM `{t}`")
                    while True:
                        rows = cur.fetchmany(500)
                        if not rows:
                            break
                        cols = ",".join(f"`{c}`" for c in rows[0].keys())
                        vals = ",".join("(" + ",".join(pymysql.converters.escape_item(v, "utf8mb4") if v is not None else "NULL"
                                                        for v in r.values()) + ")" for r in rows)
                        f.write(f"INSERT INTO `{t}`({cols}) VALUES {vals};\n")
        finally:
            mysqldb.pool.put(conn)
        f.write("SET FOREIGN_KEY_CHECKS=1;\n")


def dump() -> str | None:
    if not db.IS_MYSQL:
        return None
    from . import mysqldb
    conf = mysqldb.pool.conf
    BACKUP_DIR.mkdir(exist_ok=True)
    out = BACKUP_DIR / f"tgp-{datetime.now():%Y%m%d-%H%M%S}.sql.gz"
    exe = _dump_exe(conf)
    try:
        if exe:
            env = dict(os.environ, MYSQL_PWD=conf["password"] or "")
            cmd = [exe, f"--host={conf['host']}", f"--port={conf['port']}", f"--user={conf['user']}", "--single-transaction",
                   "--default-character-set=utf8mb4", conf["name"]]
            flags = 0x08000000 if sys.platform == "win32" else 0
            with gzip.open(out, "wb") as f:
                p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, creationflags=flags, timeout=600)
                if p.returncode != 0:
                    raise RuntimeError(p.stderr.decode("utf-8", "ignore")[-300:])
                f.write(p.stdout)
        else:
            _python_dump(conf, out)
    except Exception as e:
        log.warning("Zaxira mysqldump bilan olinmadi (%s), Python usuli sinab ko'riladi", e)
        try:
            _python_dump(conf, out)
        except Exception:
            log.exception("Zaxira olinmadi")
            try:
                out.unlink()
            except OSError:
                pass
            return None
    prune()
    return str(out)


def prune(keep: int = 7):
    files = sorted(BACKUP_DIR.glob("tgp-*.sql.gz"), reverse=True)
    for f in files[keep:]:
        try:
            f.unlink()
        except OSError:
            pass


async def loop():
    if not db.IS_MYSQL:
        return
    await asyncio.sleep(600)
    while True:
        try:
            today = datetime.now().strftime("%Y%m%d")
            if not list(BACKUP_DIR.glob(f"tgp-{today}-*.sql.gz")):
                path = await asyncio.to_thread(dump)
                if path:
                    log.info("MySQL zaxira: %s", path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("backup.loop")
        await asyncio.sleep(3600)
