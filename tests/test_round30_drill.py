"""第三十轮：备份/恢复演练——首次实跑验证。

缺口：备份（scripts/backup.sh）与恢复（deploy/restore.sh）脚本、
文档里的演练节奏（每季度/每月）都已存在，但从未有任何一次实跑
证据；且 restore.sh 是 docker-compose 绑定的，在本 VM 上根本跑
不起来（容器起不来，setns）。本文件用直连 pg_dump/pg_restore
在临时库上把同一套语义实跑一遍：dump 往返、forgotten_id 合并
重删、alembic 版本校验、恢复后简报可用。

PG-only：与 test_postgres.py 同口径，无 TEST_DATABASE_URL 时跳过；
演练库固定 memory_drill_*，teardown 必删，绝不碰生产库。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.db import make_engine
from app.episodes import brief_for_task

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="needs Postgres")

REPO = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "")
_u = urlparse(ADMIN_URL)
HOST = _u.hostname or "127.0.0.1"
PORT = str(_u.port or 5432)
USER = _u.username or "memory"
PASS = _u.password or ""
SRC_DB = "memory_drill_src"
DST_DB = "memory_drill_dst"
DRILL_USER = "drill-alice"
APP = settings.app_id


def _db_url(dbname: str) -> str:
    auth = f"{USER}:{PASS}" if PASS else USER
    return f"postgresql+psycopg://{auth}@{HOST}:{PORT}/{dbname}"


def _run(cmd, env_extra, timeout=300):
    env = dict(os.environ)
    env.update(env_extra)
    r = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True,
                       text=True, timeout=timeout)
    assert r.returncode == 0, f"{cmd[2] if len(cmd) > 2 else cmd} failed:\n{r.stdout}\n{r.stderr}"
    return r


def _pgtool(tool, *args, timeout=300):
    exe = shutil.which(tool)
    assert exe, f"{tool} not found in PATH"
    r = subprocess.run([exe, "-h", HOST, "-p", PORT, "-U", USER, *args],
                       capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, f"{tool} failed:\n{r.stdout}\n{r.stderr}"
    return r


def _drop_create(admin_engine, dbname):
    with admin_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT") as c:
        c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{dbname}"')
        c.exec_driver_sql(f'CREATE DATABASE "{dbname}"')


def _counts(sess):
    return {
        "fact": sess.scalar(select(func.count()).select_from(m.MemoryFact)),
        "insight": sess.scalar(select(func.count()).select_from(m.MemoryInsight)),
        "fact_token": sess.scalar(select(func.count()).select_from(m.MemoryFactToken)),
        "insight_token": sess.scalar(select(func.count()).select_from(m.MemoryInsightToken)),
        "forgotten": sess.scalar(select(func.count()).select_from(m.ForgottenId)),
    }


def _version(sess):
    return sess.scalar(text("SELECT version_num FROM alembic_version"))


@pytest.fixture(scope="module")
def drill():
    """建两个临时库：src 跑迁移 + 种子 → dump → 恢复到 dst。"""
    admin_engine = make_engine(ADMIN_URL)
    tmpdir = tempfile.mkdtemp(prefix="restore-drill-")
    dump = str(Path(tmpdir) / "memory_drill.dump")
    try:
        _drop_create(admin_engine, SRC_DB)
        _drop_create(admin_engine, DST_DB)
        _run([sys.executable, "-m", "alembic", "upgrade", "head"],
             {"MIGRATE_DATABASE_URL": _db_url(SRC_DB),
              "DATABASE_URL": _db_url(SRC_DB)})
        src_engine = make_engine(_db_url(SRC_DB))
        SrcSession = sessionmaker(bind=src_engine, expire_on_commit=False)
        s = SrcSession()
        facts = []
        for subject, content in [
                ("演练事实一", "备份前要先停写再快照"),
                ("演练事实二", "恢复后先对行数再放流量"),
                ("演练事实三", "遗忘记录只存 id 不存原文")]:
            row = m.MemoryFact(app_id=APP, user_id=DRILL_USER,
                               subject=subject, content=content)
            s.add(row)
            facts.append(row)
        insight = m.MemoryInsight(
            app_id=APP, user_id=DRILL_USER, kind="lesson",
            subject="部署经验", content="上线前先查端口占用",
            applies_when="发布服务时")
        s.add(insight)
        s.add(m.MemoryInsight(
            app_id=APP, user_id=DRILL_USER, kind="pitfall",
            subject="演练教训", content="只测备份不测恢复等于没备份",
            applies_when="制定备份计划时"))
        # 备份前已遗忘：id 指向库中不存在的事实，行本身随备份走。
        s.add(m.ForgottenId(id="drill-forgotten-fact-0001", kind="fact"))
        s.commit()
        fact_ids = [f.id for f in facts]
        insight_id = insight.id
        src_counts = _counts(s)
        src_version = _version(s)
        s.close()
        _pgtool("pg_dump", "-Fc", "-d", SRC_DB, "-f", dump)
        assert Path(dump).stat().st_size > 0, "dump file is empty"
        _pgtool("pg_restore", "-d", DST_DB, "--clean", "--if-exists",
                "--no-owner", dump)
        dst_engine = make_engine(_db_url(DST_DB))
        yield {"src_counts": src_counts, "src_version": src_version,
               "fact_ids": fact_ids, "insight_id": insight_id,
               "dst_engine": dst_engine, "dump": dump}
        dst_engine.dispose()
        src_engine.dispose()
    finally:
        with admin_engine.connect().execution_options(
                isolation_level="AUTOCOMMIT") as c:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{DST_DB}"')
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{SRC_DB}"')
        admin_engine.dispose()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_drill_roundtrip_counts_and_version(drill):
    """往返行数相等；dst 的 alembic 版本等于 src（恢复后版本校验跑得通）。"""
    DstSession = sessionmaker(bind=drill["dst_engine"], expire_on_commit=False)
    d = DstSession()
    try:
        assert _counts(d) == drill["src_counts"]
        assert _version(d) == drill["src_version"]
        assert drill["src_version"], "src has no alembic version"
    finally:
        d.close()
    r = _run([sys.executable, "-m", "alembic", "current"],
             {"MIGRATE_DATABASE_URL": _db_url(DST_DB),
              "DATABASE_URL": _db_url(DST_DB)})
    assert drill["src_version"] in r.stdout


def test_drill_brief_works_after_restore(drill):
    """恢复后的库能直接出简报（不依赖向量，纯关键词路）。"""
    DstSession = sessionmaker(bind=drill["dst_engine"], expire_on_commit=False)
    d = DstSession()
    try:
        out = brief_for_task(d, UserContext(app_id=APP, user_id=DRILL_USER),
                             "上线前查端口占用", 5, embedder=None)
        assert drill["insight_id"] in [o.insight_id for o in out]
    finally:
        d.close()


def test_drill_forgotten_does_not_resurrect(drill):
    """放最后：改写 dst。模拟备份后才发生的遗忘（线上 forgotten_id
    合并语义）：插表 → 按表重删 → 该事实消失、其余行仍在。"""
    DstSession = sessionmaker(bind=drill["dst_engine"], expire_on_commit=False)
    d = DstSession()
    try:
        victim = drill["fact_ids"][0]
        d.add(m.ForgottenId(id=victim, kind="fact"))
        d.commit()
        d.execute(text(
            "DELETE FROM memory_fact WHERE id IN "
            "(SELECT id FROM forgotten_id WHERE kind='fact')"))
        d.commit()
        assert d.get(m.MemoryFact, victim) is None
        assert d.scalar(select(func.count()).select_from(m.MemoryFact)) == 2
        assert d.scalar(select(func.count()).select_from(m.ForgottenId)) == 2
    finally:
        d.close()
