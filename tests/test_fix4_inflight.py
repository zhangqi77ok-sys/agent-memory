"""修复第 4 轮：审计 Top 4——在途任务绕过删除/关闭 + 巡检/提炼的记忆开关。

口径（见 fix4-design-draft.md）：事实与提炼在慢调用窗口内复查三重门
（行仍在、开关仍开、无新遗忘水位），命中则回滚整批；删行是正常终态，
不抛错、不崩 worker；巡检按“显式关闭”过滤用户（入口快照，轮内翻转
下一轮起效）；clear_all 先删任务行再快照（PG 顺序修复）。
测试用 Fake 抽取器/提炼器 + 独立会话模拟窗口内状态变更，零联网。
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import models as m
from app.api.me import clear_all
from app.auth import UserContext
from app.config import settings
from app.episodes import (
    _Cand,
    consolidate_insights,
    run_episode_once,
    skeletonize_episodes,
    submit_episode,
)
from app.extract import Candidate, InvalidExtraction
from app.links import weave_links
from app.schemas import CONSENT_VERSION
from app.sleep import sleep_consolidate
from app.tombstone import record_tombstone
from app.worker import claim_job, expire_memories, process_job, run_once
from tests.conftest import AdminSession, consent

APP = settings.app_id


def _u(user):
    return UserContext(app_id=APP, user_id=user)


def _set_switch(db, user, on):
    """直接写设置行（绕过 API，便于在 worker 层测试里精确控制）。"""
    s = db.scalar(select(m.UserSetting).where(m.UserSetting.user_id == user))
    if s is None:
        s = m.UserSetting(app_id=APP, user_id=user, memory_enabled=on, consent_version=CONSENT_VERSION)
        db.add(s)
    else:
        s.memory_enabled = on
    db.commit()


def _job_with_msg(db, user, key, content, created_at=None):
    job = m.MemoryJob(app_id=APP, user_id=user, idempotency_key=key, session_id="s", **({"created_at": created_at} if created_at else {}))
    db.add(job)
    db.commit()
    db.add(m.SessionMessage(app_id=APP, user_id=user, session_id="s", job_id=job.id, msg_id="m1", role="user", content=content))
    db.commit()
    return db.get(m.MemoryJob, job.id)


def _live_facts(db, user):
    return list(db.scalars(select(m.MemoryFact).where(m.MemoryFact.user_id == user, m.MemoryFact.valid_to.is_(None))))


def _live_insights(db, user):
    return list(db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.user_id == user, m.MemoryInsight.valid_to.is_(None))))


class MutatingExtractor:
    """第一次 extract 时先用独立会话提交一次状态变更，再返回候选——
    模拟抽取慢调用窗口内用户关开关/遗忘/清空。"""

    def __init__(self, items, mutate):
        self.items = items
        self.mutate = mutate
        self.done = False

    def extract(self, text):
        if not self.done:
            self.done = True
            self.mutate()
        return list(self.items)


class MutatingDistiller:
    def __init__(self, items, mutate):
        self.items = items
        self.mutate = mutate
        self.done = False

    def distill(self, text):
        if not self.done:
            self.done = True
            self.mutate()
        return list(self.items)


def _flip_off(user):
    def mutate():
        s2 = AdminSession()
        row = s2.scalar(select(m.UserSetting).where(m.UserSetting.user_id == user))
        row.memory_enabled = False
        s2.commit()
        s2.close()

    return mutate


def _forget_unrelated(user):
    """在独立会话里记一条无关主题的墓碑——只推水位，不触候选内容，
    用于区分水位重查与第 2 轮逐候选墓碑。"""

    def mutate():
        s2 = AdminSession()
        record_tombstone(s2, APP, user, "fact", "茶叶", "喜欢喝茶")
        s2.commit()
        s2.close()

    return mutate


def _clear_all(user):
    def mutate():
        s2 = AdminSession()
        clear_all(_u(user), s2)
        s2.close()

    return mutate


def _delete_job(job_id):
    def mutate():
        s2 = AdminSession()
        row = s2.get(m.MemoryJob, job_id)
        if row is not None:
            s2.delete(row)
        s2.commit()
        s2.close()

    return mutate


def _delete_episode(ep_id):
    def mutate():
        s2 = AdminSession()
        row = s2.get(m.TaskEpisode, ep_id)
        if row is not None:
            s2.delete(row)
        s2.commit()
        s2.close()

    return mutate


# ---- 事实路径 ----


def test_switch_off_mid_extraction_skips_and_consumes(client, db):
    consent(client, "alice")
    _job_with_msg(db, "alice", "f4-k1", "我喜欢咖啡")
    out = run_once(db, MutatingExtractor([Candidate(kind="fact", subject="咖啡", content="喜欢咖啡", action="ADD")], _flip_off("alice")))
    assert out is not None and out.status == "done"
    assert _live_facts(db, "alice") == []
    assert db.scalars(select(m.AuditLog).where(m.AuditLog.action == "write", m.AuditLog.decision == "skip")).all()


def test_forget_unrelated_mid_extraction_drops_whole_job(client, db):
    consent(client, "alice")
    past = datetime.now(UTC) - timedelta(hours=1)
    _job_with_msg(db, "alice", "f4-k2", "我喜欢咖啡", created_at=past)
    out = run_once(
        db, MutatingExtractor([Candidate(kind="fact", subject="咖啡", content="喜欢咖啡", action="ADD")], _forget_unrelated("alice"))
    )
    assert out is not None and out.status == "done"
    # 候选与遗忘主题无关：逐候选墓碑拦不住它，整单丢弃只能来自水位重查。
    assert _live_facts(db, "alice") == []


@pytest.mark.skipif(
    bool(os.environ.get("TEST_DATABASE_URL")),
    reason="PG 下认领行持有 FOR UPDATE 锁，第二会话删除会阻塞到 worker 提交，竞态被锁串行化，missing 路径只在 SQLite 侧测",
)
def test_clear_all_mid_extraction_returns_missing_no_crash(client, db):
    consent(client, "alice")
    _job_with_msg(db, "alice", "f4-k3", "我喜欢咖啡")
    out = run_once(db, MutatingExtractor([Candidate(kind="fact", subject="咖啡", content="喜欢咖啡", action="ADD")], _clear_all("alice")))
    assert out is None, "任务行被删是正常终态，run_once 应返回 None 而非崩溃"
    assert _live_facts(db, "alice") == []
    assert db.scalars(select(m.MemoryJob).where(m.MemoryJob.user_id == "alice")).all() == []


@pytest.mark.skipif(
    bool(os.environ.get("TEST_DATABASE_URL")),
    reason="PG 下认领行持有 FOR UPDATE 锁，第二会话删除会阻塞到 worker 提交，竞态被锁串行化，missing 路径只在 SQLite 侧测",
)
def test_run_once_deleted_job_then_error_does_not_raise(client, db):
    consent(client, "alice")
    job = _job_with_msg(db, "alice", "f4-k4", "我喜欢咖啡")

    class BoomExtractor:
        def extract(self, text):
            _delete_job(job.id)()
            raise RuntimeError("boom")

    assert run_once(db, BoomExtractor()) is None


@pytest.mark.skipif(
    bool(os.environ.get("TEST_DATABASE_URL")),
    reason="PG 下认领行持有 FOR UPDATE 锁，第二会话删除会阻塞到 worker 提交，竞态被锁串行化，missing 路径只在 SQLite 侧测",
)
def test_process_job_invalid_extraction_deleted_job_returns_missing(client, db):
    consent(client, "alice")
    job = _job_with_msg(db, "alice", "f4-k5", "我喜欢咖啡")
    claimed = claim_job(db)
    assert claimed is not None and claimed.id == job.id

    class BadExtractor:
        def extract(self, text):
            _delete_job(job.id)()
            raise InvalidExtraction("bad output")

    assert process_job(db, claimed, BadExtractor()) == "missing"


# ---- 提炼路径 ----


def _episode(db, user, title="部署服务"):
    steps = [{"action": "在 8080 启动", "error": "EADDRINUSE"}, {"action": "改用 3000 启动", "observation": "成功"}]
    return submit_episode(db, _u(user), source="test", title=title, outcome="partial", steps=steps, cost={"tokens": 400})


def test_distill_switch_off_mid_distill_skips_batch(client, db):
    consent(client, "alice")
    _episode(db, "alice")
    out = run_episode_once(db, MutatingDistiller([_Cand("pitfall", "部署端口选择", "部署前先探测端口占用", "部署时")], _flip_off("alice")))
    assert out is not None and out.distill_status == "done"
    assert _live_insights(db, "alice") == []
    assert db.scalars(select(m.AuditLog).where(m.AuditLog.action == "distill", m.AuditLog.decision == "skipped")).all()


def test_distill_switch_already_off_at_start_skips(client, db):
    _set_switch(db, "alice", False)
    _episode(db, "alice")
    out = run_episode_once(db, MutatingDistiller([_Cand("pitfall", "部署端口选择", "部署前先探测端口占用", "部署时")], lambda: None))
    assert out is not None and out.distill_status == "done"
    assert _live_insights(db, "alice") == []


@pytest.mark.skipif(
    bool(os.environ.get("TEST_DATABASE_URL")),
    reason="PG 下认领行持有 FOR UPDATE 锁，第二会话删除会阻塞到 worker 提交，竞态被锁串行化，missing 路径只在 SQLite 侧测",
)
def test_distill_episode_deleted_mid_distill_returns_none(client, db):
    consent(client, "alice")
    ep = _episode(db, "alice")
    out = run_episode_once(
        db, MutatingDistiller([_Cand("pitfall", "部署端口选择", "部署前先探测端口占用", "部署时")], _delete_episode(ep.id))
    )
    assert out is None
    assert _live_insights(db, "alice") == []


# ---- 巡检开关过滤（显式关闭语义） ----


def test_patrol_consolidate_skips_switched_off_user(client, db):
    consent(client, "alice")
    _set_switch(db, "bob", False)
    old = datetime.now(UTC) - timedelta(days=60)
    for user in ("alice", "bob", "carol"):  # carol 无设置行：legacy 默认照常处理
        db.add(
            m.MemoryInsight(
                app_id=APP, user_id=user, kind="lesson", subject="旧经验", content=f"{user}的内容", confidence=0.9, valid_from=old
            )
        )
    db.commit()
    consolidate_insights(db)
    conf = {u: db.scalar(select(m.MemoryInsight.confidence).where(m.MemoryInsight.user_id == u)) for u in ("alice", "bob", "carol")}
    assert conf["alice"] < 0.9 and conf["carol"] < 0.9
    assert conf["bob"] == 0.9, "显式关闭的用户巡检不得动其置信度"


def test_patrol_weave_prune_skips_switched_off_user(client, db):
    consent(client, "alice")
    _set_switch(db, "bob", False)
    for user in ("alice", "bob"):
        dead = m.MemoryInsight(app_id=APP, user_id=user, kind="lesson", subject="已关", content="旧", valid_to=datetime.now(UTC))
        db.add(dead)
        db.commit()
        db.add(m.MemoryInsight(app_id=APP, user_id=user, kind="lesson", subject="活", content="新"))
        db.commit()
        db.add(
            m.MemoryLink(app_id=APP, user_id=user, src_kind="insight", src_id=dead.id, dst_kind="insight", dst_id="x", relation="related")
        )
        db.commit()
    weave_links(db)
    for user, should_prune in (("alice", True), ("bob", False)):
        link = db.scalar(select(m.MemoryLink).where(m.MemoryLink.user_id == user))
        assert (link.valid_to is not None) == should_prune, user


def test_patrol_sleep_no_report_for_switched_off_user(client, db):
    consent(client, "alice")
    _set_switch(db, "bob", False)
    for user in ("alice", "bob"):
        db.add(m.MemoryFact(app_id=APP, user_id=user, subject="s", content="c"))
    db.commit()
    sleep_consolidate(db)
    users = {r.user_id for r in db.scalars(select(m.ConsolidationReport))}
    assert "alice" in users and "bob" not in users


def test_patrol_expire_skips_switched_off_user(client, db):
    consent(client, "alice")
    _set_switch(db, "bob", False)
    old = datetime.now(UTC) - timedelta(days=40)
    for user in ("alice", "bob"):
        db.add(m.MemoryFact(app_id=APP, user_id=user, subject="旧", content="旧", valid_from=old))
    db.commit()
    assert expire_memories(db, ttl_days=30) == 1
    for user, should_expire in (("alice", True), ("bob", False)):
        fact = db.scalar(select(m.MemoryFact).where(m.MemoryFact.user_id == user))
        assert (fact.valid_to is not None) == should_expire, user


def test_patrol_skeletonize_skips_switched_off_user(client, db):
    consent(client, "alice")
    _set_switch(db, "bob", False)
    for user in ("alice", "bob"):
        ep = m.TaskEpisode(
            app_id=APP,
            user_id=user,
            title="旧轨迹",
            outcome="success",
            steps=[{"action": f"a{i}"} for i in range(5)],
            distill_status="done",
            created_at=datetime.now(UTC) - timedelta(days=100),
        )
        db.add(ep)
    db.commit()
    assert skeletonize_episodes(db) == 1
    for user, should_skel in (("alice", True), ("bob", False)):
        ep = db.scalar(select(m.TaskEpisode).where(m.TaskEpisode.user_id == user))
        assert bool(ep.skeletonized) == should_skel, user


# ---- clear_all 顺序 ----


def test_clear_all_removes_job_fact_and_tokens(client, db):
    consent(client, "alice")
    job = _job_with_msg(db, "alice", "f4-k6", "我喜欢咖啡")
    _live_facts(db, "alice")  # 触发事实写路径外，直接造一行带倒排的事实
    db.add(m.MemoryFact(app_id=APP, user_id="alice", subject="咖啡", content="喜欢咖啡"))
    db.commit()
    fact = db.scalar(select(m.MemoryFact).where(m.MemoryFact.user_id == "alice"))
    assert db.scalars(select(m.MemoryFactToken).where(m.MemoryFactToken.fact_id == fact.id)).all(), "直接造行也应有倒排事件"
    r = client.delete("/v1/me/memories", params={"user_id": "alice"})
    assert r.status_code == 204, r.text
    assert db.scalars(select(m.MemoryJob).where(m.MemoryJob.id == job.id)).all() == []
    assert _live_facts(db, "alice") == []
    assert db.scalars(select(m.MemoryFactToken).where(m.MemoryFactToken.fact_id == fact.id)).all() == []
    assert db.scalars(select(m.ForgottenContent).where(m.ForgottenContent.user_id == "alice")).all()
