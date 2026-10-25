"""修复第 3 轮：生产恢复五类重删 + 墓碑 sidecar——实跑验证。

审计 Top 3：deploy/restore.sh 只重删事实，且墓碑随活库一起丢。
本文件跑的是生产 restore.sh 本文件（直连参数覆盖 docker 路径，
docker 路径本身在 VM 上跑不起来，见报告与文档声明），不是语义复刻：

- 场景 1（线上库 + sidecar）：src 备出来源数据，五类各有 id 遗忘、
  哈希遗忘、正常行三档；跨用户同内容行必须存活；被删经验的
  token/边/交付边清零、存活经验的边/交付边 intact；恢复脚本重跑幂等。
- 场景 2（活库损坏）：LIVE_DB 指不存在的库（取数容错为空），仅
  sidecar 生效 → sidecar 覆盖的两类仍重删、正常行 intact。
- 场景 3（备份侧）：直连参数跑 scripts/backup.sh → dump 非空 +
  sidecar JSON 可解析且含探针墓碑行。

PG-only：与 test_postgres.py 同口径，无 TEST_DATABASE_URL 时跳过；
演练库固定 memory_fix3_*，teardown 必删，绝不碰生产库。
"""

import json
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
from app.tombstone import content_hash, record_tombstone

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs Postgres")

REPO = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "")
_u = urlparse(ADMIN_URL)
HOST = _u.hostname or "127.0.0.1"
PORT = str(_u.port or 5432)
USER = _u.username or "memory"
SRC_DB = "memory_fix3_src"
LIVE_DB = "memory_fix3_live"
DST_DB = "memory_fix3_dst"
DST2_DB = "memory_fix3_dst2"
DRILL_USER = "fix3-alice"
OTHER_USER = "fix3-bob"
APP = settings.app_id


def _db_url(dbname: str) -> str:
    return f"postgresql+psycopg://{USER}@{HOST}:{PORT}/{dbname}"


def _run(cmd, env_extra, timeout=300):
    env = dict(os.environ)
    env.update(env_extra)
    r = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, f"{cmd} failed:\n{r.stdout}\n{r.stderr}"
    return r


def _pgtool(tool, *args, timeout=300):
    exe = shutil.which(tool)
    assert exe, f"{tool} not found in PATH"
    r = subprocess.run([exe, "-h", HOST, "-p", PORT, "-U", USER, *args], capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, f"{tool} failed:\n{r.stdout}\n{r.stderr}"
    return r


def _drop_create(admin_engine, dbname):
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{dbname}"')
        c.exec_driver_sql(f'CREATE DATABASE "{dbname}"')


def _version(sess):
    return sess.scalar(text("SELECT version_num FROM alembic_version"))


def _run_restore(dump, target, live_db, sidecar, timeout=300):
    env = {
        "RESTORE_PSQL": f"psql -h {HOST} -p {PORT} -U {USER} -v ON_ERROR_STOP=1 -qAt",
        "RESTORE_PG_RESTORE": f"pg_restore -h {HOST} -p {PORT} -U {USER}",
        "LIVE_DB": live_db,
        "ADMIN_DB": "postgres",
    }
    if sidecar:
        env["SIDECAR"] = sidecar
    return _run(["bash", str(REPO / "deploy" / "restore.sh"), dump, target], env, timeout=timeout)


@pytest.fixture(scope="module")
def drill():
    """src（源数据）+ live（线上墓碑）→ dump → 生产 restore.sh → dst。"""
    admin_engine = make_engine(ADMIN_URL)
    tmpdir = tempfile.mkdtemp(prefix="fix3-drill-")
    dump = str(Path(tmpdir) / "fix3.dump")
    sidecar = str(Path(tmpdir) / "fix3.tombstones.json")
    try:
        for db in (SRC_DB, LIVE_DB, DST_DB):
            _drop_create(admin_engine, db)
        for db in (SRC_DB, LIVE_DB):
            _run([sys.executable, "-m", "alembic", "upgrade", "head"], {"MIGRATE_DATABASE_URL": _db_url(db), "DATABASE_URL": _db_url(db)})
        src_engine = make_engine(_db_url(SRC_DB))
        SrcSession = sessionmaker(bind=src_engine, expire_on_commit=False)
        s = SrcSession()
        # ---- 事实：id 遗忘 / 哈希遗忘 / 正常 / sidecar-id 遗忘 / 跨用户同内容
        f_id = m.MemoryFact(app_id=APP, user_id=DRILL_USER, subject="fix3事实甲", content="id遗忘的事实内容")
        f_hash = m.MemoryFact(app_id=APP, user_id=DRILL_USER, subject="fix3事实乙", content="哈希遗忘的事实内容")
        f_ok = m.MemoryFact(app_id=APP, user_id=DRILL_USER, subject="fix3事实丁", content="存活的事实内容")
        f_side = m.MemoryFact(app_id=APP, user_id=DRILL_USER, subject="fix3事实丙", content="sidecar遗忘的事实内容")
        f_other = m.MemoryFact(app_id=APP, user_id=OTHER_USER, subject="fix3事实乙", content="哈希遗忘的事实内容")
        s.add_all([f_id, f_hash, f_ok, f_side, f_other])
        # ---- 偏好：id / 哈希 / 正常
        p_id = m.MemoryPreference(app_id=APP, user_id=DRILL_USER, key="fix3偏好甲", value="id遗忘的偏好值")
        p_hash = m.MemoryPreference(app_id=APP, user_id=DRILL_USER, key="fix3偏好乙", value="哈希遗忘的偏好值")
        p_ok = m.MemoryPreference(app_id=APP, user_id=DRILL_USER, key="fix3偏好丁", value="存活的偏好值")
        s.add_all([p_id, p_hash, p_ok])
        # ---- 经验：id / 哈希 / sidecar-哈希 / 正常（带边与交付边）
        i_id = m.MemoryInsight(
            app_id=APP, user_id=DRILL_USER, kind="lesson", subject="fix3经验甲", content="id遗忘的经验内容", applies_when="fix3场景甲"
        )
        i_hash = m.MemoryInsight(
            app_id=APP, user_id=DRILL_USER, kind="lesson", subject="fix3经验乙", content="哈希遗忘的经验内容", applies_when="fix3场景乙"
        )
        i_side = m.MemoryInsight(
            app_id=APP, user_id=DRILL_USER, kind="lesson", subject="fix3经验丙", content="sidecar遗忘的经验内容", applies_when="fix3场景丙"
        )
        i_ok = m.MemoryInsight(
            app_id=APP, user_id=DRILL_USER, kind="lesson", subject="fix3经验丁", content="存活的经验内容", applies_when="fix3场景丁"
        )
        s.add_all([i_id, i_hash, i_side, i_ok])
        s.flush()
        link = m.MemoryLink(
            app_id=APP,
            user_id=DRILL_USER,
            src_kind="insight",
            src_id=i_ok.id,
            dst_kind="insight",
            dst_id=i_id.id,
            relation="related",
            strength=0.5,
        )
        s.add(link)
        s.flush()  # link.id 是 Python 侧 default，flush 后才有值，边才能引用它
        s.add(m.BriefDeliveryEdge(app_id=APP, user_id=DRILL_USER, delivery_id="fix3-delivery-1", link_id=link.id, insight_id=i_id.id))
        s.add(
            m.BriefDeliveryEdge(
                app_id=APP, user_id=DRILL_USER, delivery_id="fix3-delivery-2", link_id="fix3-link-standalone", insight_id=i_ok.id
            )
        )
        # 显式 token 行（不用 ORM 事件口径，保证断言非空）：被删者清零。
        s.add(m.MemoryFactToken(app_id=APP, user_id=DRILL_USER, fact_id=f_id.id, token="zzfix3fact"))
        s.add(m.MemoryFactToken(app_id=APP, user_id=DRILL_USER, fact_id=f_hash.id, token="zzfix3facthash"))
        s.add(m.MemoryInsightToken(app_id=APP, user_id=DRILL_USER, insight_id=i_id.id, token="zzfix3ins"))
        s.add(m.MemoryInsightToken(app_id=APP, user_id=DRILL_USER, insight_id=i_hash.id, token="zzfix3inshash"))
        # ---- 轨迹：id 遗忘 / 正常
        e_id = m.TaskEpisode(app_id=APP, user_id=DRILL_USER, title="fix3轨迹甲", steps=[{"action": "做甲"}])
        e_ok = m.TaskEpisode(app_id=APP, user_id=DRILL_USER, title="fix3轨迹丁", steps=[{"action": "做丁"}])
        s.add_all([e_id, e_ok])
        # ---- 提醒：id / 哈希 / 正常
        r_id = m.MemoryReminder(app_id=APP, user_id=DRILL_USER, condition_text="fix3条件甲", action_text="fix3动作甲")
        r_hash = m.MemoryReminder(app_id=APP, user_id=DRILL_USER, condition_text="fix3条件乙", action_text="fix3动作乙")
        r_ok = m.MemoryReminder(app_id=APP, user_id=DRILL_USER, condition_text="fix3条件丁", action_text="fix3动作丁")
        s.add_all([r_id, r_hash, r_ok])
        # ---- 备份侧探针墓碑（只测 backup.sh 导出，不干扰恢复断言）
        s.add(m.ForgottenId(id="fix3-backup-probe-1", kind="fact"))
        record_tombstone(s, APP, DRILL_USER, "fact", "fix3探针主题", "fix3探针内容")
        s.commit()
        ids = {
            "f_id": f_id.id,
            "f_hash": f_hash.id,
            "f_ok": f_ok.id,
            "f_side": f_side.id,
            "f_other": f_other.id,
            "p_id": p_id.id,
            "p_hash": p_hash.id,
            "p_ok": p_ok.id,
            "i_id": i_id.id,
            "i_hash": i_hash.id,
            "i_side": i_side.id,
            "i_ok": i_ok.id,
            "e_id": e_id.id,
            "e_ok": e_ok.id,
            "r_id": r_id.id,
            "r_hash": r_hash.id,
            "r_ok": r_ok.id,
            "link": link.id,
        }
        src_version = _version(s)
        s.close()
        # ---- live 库：线上墓碑（id 段 + 哈希段）
        live_engine = make_engine(_db_url(LIVE_DB))
        LiveSession = sessionmaker(bind=live_engine, expire_on_commit=False)
        lv = LiveSession()
        for row_id, kind in [(f_id.id, "fact"), (p_id.id, "preference"), (i_id.id, "insight"), (e_id.id, "episode"), (r_id.id, "reminder")]:
            lv.merge(m.ForgottenId(id=row_id, kind=kind))
        record_tombstone(lv, APP, DRILL_USER, "fact", "fix3事实乙", "哈希遗忘的事实内容")
        record_tombstone(lv, APP, DRILL_USER, "preference", "fix3偏好乙", "哈希遗忘的偏好值")
        record_tombstone(lv, APP, DRILL_USER, "insight", "fix3经验乙", "哈希遗忘的经验内容")
        record_tombstone(lv, APP, DRILL_USER, "reminder", "fix3条件乙", "fix3动作乙")
        lv.commit()
        lv.close()
        # ---- sidecar：事实 id 段 + 经验哈希段
        sidecar_doc = {
            "version": 1,
            "exported_at": "2026-10-25T00:00:00Z",
            "forgotten_id": [{"id": f_side.id, "kind": "fact", "forgotten_at": "2026-10-25T00:00:00Z"}],
            "forgotten_content": [
                {
                    "app_id": APP,
                    "user_id": DRILL_USER,
                    "kind": "insight",
                    "content_hash": content_hash("fix3经验丙", "sidecar遗忘的经验内容"),
                    "forgotten_at": "2026-10-25T00:00:00Z",
                }
            ],
        }
        Path(sidecar).write_text(json.dumps(sidecar_doc), encoding="utf-8")
        _pgtool("pg_dump", "-Fc", "-d", SRC_DB, "-f", dump)
        assert Path(dump).stat().st_size > 0, "dump file is empty"
        r = _run_restore(dump, DST_DB, LIVE_DB, sidecar)
        assert "pgcrypto available" in r.stdout, r.stdout
        dst_engine = make_engine(_db_url(DST_DB))
        yield {"ids": ids, "dst_engine": dst_engine, "dump": dump, "sidecar": sidecar, "src_version": src_version, "tmpdir": tmpdir}
    finally:
        # 先 dispose 再删库：setup 中途出错时连接池仍占着库，DROP 会报 in use。
        for eng in (locals().get("dst_engine"), locals().get("live_engine"), locals().get("src_engine")):
            if eng is not None:
                eng.dispose()
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            for db in (DST2_DB, DST_DB, LIVE_DB, SRC_DB):
                c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{db}"')
        admin_engine.dispose()
        shutil.rmtree(tmpdir, ignore_errors=True)


def _dst_session(drill):
    return sessionmaker(bind=drill["dst_engine"], expire_on_commit=False)


def test_fix3_five_kinds_redeleted(drill):
    """场景 1：五类 id 段 + 哈希段 + sidecar 段全部重删，正常行 intact。"""
    DstSession = _dst_session(drill)
    d = DstSession()
    ids = drill["ids"]
    try:
        for key in ("f_id", "f_hash", "f_side"):
            assert d.get(m.MemoryFact, ids[key]) is None, key
        assert d.get(m.MemoryFact, ids["f_ok"]) is not None
        # 跨用户同内容行必须存活：哈希段按 (app, user) 作用域匹配。
        assert d.get(m.MemoryFact, ids["f_other"]) is not None
        for key in ("p_id", "p_hash"):
            assert d.get(m.MemoryPreference, ids[key]) is None, key
        assert d.get(m.MemoryPreference, ids["p_ok"]) is not None
        for key in ("i_id", "i_hash", "i_side"):
            assert d.get(m.MemoryInsight, ids[key]) is None, key
        assert d.get(m.MemoryInsight, ids["i_ok"]) is not None
        assert d.get(m.TaskEpisode, ids["e_id"]) is None
        assert d.get(m.TaskEpisode, ids["e_ok"]) is not None
        for key in ("r_id", "r_hash"):
            assert d.get(m.MemoryReminder, ids[key]) is None, key
        assert d.get(m.MemoryReminder, ids["r_ok"]) is not None
    finally:
        d.close()


def test_fix3_derived_rows_cleaned(drill):
    """场景 1 派生数据：被删者的 token/边/交付边清零，存活者 intact。"""
    DstSession = _dst_session(drill)
    d = DstSession()
    ids = drill["ids"]
    try:
        assert (
            d.scalar(select(func.count()).select_from(m.MemoryFactToken).where(m.MemoryFactToken.fact_id.in_([ids["f_id"], ids["f_hash"]])))
            == 0
        )
        assert (
            d.scalar(
                select(func.count())
                .select_from(m.MemoryInsightToken)
                .where(m.MemoryInsightToken.insight_id.in_([ids["i_id"], ids["i_hash"]]))
            )
            == 0
        )
        assert (
            d.scalar(
                select(func.count())
                .select_from(m.MemoryLink)
                .where((m.MemoryLink.src_id == ids["i_id"]) | (m.MemoryLink.dst_id == ids["i_id"]))
            )
            == 0
        )
        assert d.scalar(select(func.count()).select_from(m.BriefDeliveryEdge).where(m.BriefDeliveryEdge.insight_id == ids["i_id"])) == 0
        assert d.scalar(select(func.count()).select_from(m.BriefDeliveryEdge).where(m.BriefDeliveryEdge.insight_id == ids["i_ok"])) == 1
        # 墓碑三源并集落到目标库：线上 + sidecar + 备份自带。
        assert d.get(m.ForgottenId, ids["f_side"]) is not None
        row = d.scalar(
            select(m.ForgottenContent).where(
                m.ForgottenContent.app_id == APP,
                m.ForgottenContent.user_id == DRILL_USER,
                m.ForgottenContent.kind == "insight",
                m.ForgottenContent.content_hash == content_hash("fix3经验丙", "sidecar遗忘的经验内容"),
            )
        )
        assert row is not None
    finally:
        d.close()


def test_fix3_brief_and_version_after_restore(drill):
    """场景 1：恢复后简报可用、alembic 版本一致、恢复重跑幂等。"""
    DstSession = _dst_session(drill)
    d = DstSession()
    try:
        assert _version(d) == drill["src_version"]
        out = brief_for_task(d, UserContext(app_id=APP, user_id=DRILL_USER), "fix3场景丁 存活的经验内容", 5, embedder=None)
        assert drill["ids"]["i_ok"] in [o.insight_id for o in out]
    finally:
        d.close()
    # 重跑前先 dispose 引擎：连接池的空闲连接会让 restore.sh 的
    # DROP DATABASE 报 in use（生产上恢复前停应用就是这个原因）。
    drill["dst_engine"].dispose()
    r = _run_restore(drill["dump"], DST_DB, LIVE_DB, drill["sidecar"])
    assert r.returncode == 0
    eng = make_engine(_db_url(DST_DB))
    try:
        S = sessionmaker(bind=eng, expire_on_commit=False)
        s = S()
        try:
            assert s.get(m.MemoryInsight, drill["ids"]["i_ok"]) is not None
            assert s.get(m.MemoryFact, drill["ids"]["f_ok"]) is not None
            assert s.get(m.MemoryFact, drill["ids"]["f_hash"]) is None
        finally:
            s.close()
    finally:
        eng.dispose()


def test_fix3_sidecar_only_when_live_damaged(drill):
    """场景 2：活库不存在（取数容错为空），仅 sidecar 仍重删其覆盖的两类。"""
    admin_engine = make_engine(ADMIN_URL)
    try:
        _drop_create(admin_engine, DST2_DB)
        r = _run_restore(drill["dump"], DST2_DB, "memory_fix3_live_missing", drill["sidecar"])
        assert r.returncode == 0
        eng = make_engine(_db_url(DST2_DB))
        S = sessionmaker(bind=eng, expire_on_commit=False)
        s = S()
        try:
            assert s.get(m.MemoryFact, drill["ids"]["f_side"]) is None
            assert s.get(m.MemoryInsight, drill["ids"]["i_side"]) is None
            assert s.get(m.MemoryFact, drill["ids"]["f_ok"]) is not None
            assert s.get(m.MemoryInsight, drill["ids"]["i_ok"]) is not None
        finally:
            s.close()
            eng.dispose()
    finally:
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{DST2_DB}"')
        admin_engine.dispose()


def test_fix3_backup_sidecar_export(drill):
    """场景 3：直连参数跑 backup.sh，dump 非空且 sidecar 含探针墓碑行。"""
    outdir = Path(drill["tmpdir"]) / "backup-out"
    outdir.mkdir(exist_ok=True)
    env = {"BACKUP_DIR": str(outdir), "DB_NAME": SRC_DB, "DB_CONTAINER": "fix3-no-such-container", "PGHOST": HOST, "PGPORT": PORT}
    r = _run(["bash", str(REPO / "scripts" / "backup.sh")], env)
    assert "sidecar written" in r.stdout, r.stdout
    dumps = sorted(outdir.glob("*.dump"))
    sidecars = sorted(outdir.glob("*.tombstones.json"))
    assert len(dumps) == 1 and dumps[0].stat().st_size > 0
    assert len(sidecars) == 1
    doc = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert any(row["id"] == "fix3-backup-probe-1" for row in doc["forgotten_id"])
    assert any(row["content_hash"] == content_hash("fix3探针主题", "fix3探针内容") for row in doc["forgotten_content"])
