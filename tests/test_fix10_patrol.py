"""修复第 10 轮：审计 Top 10——巡检性能与健壮性。

覆盖：锚点候选完备性、小规模合并语义不变、规模亚平方、预算轮转、
巡检失败退避 + 任务优先、巡检锁、边唯一索引 + SAVEPOINT 回查、
租约续租、分片提交。Fake + 临时库，零联网。
"""

import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select, text

from app import models as m
from app.config import settings
from app.episodes import consolidate_insights
from app.extract import Candidate, same_subject
from app.links import _insert_link, create_link
from app.patrol import (
    candidate_pairs,
    pair_window,
    release_patrol_lock,
    try_patrol_lock,
)
from app.schemas import CONSENT_VERSION
from app.worker import (
    LeaseRenewer,
    claim_job,
    renew_lease,
    run_cycle,
    run_patrol,
)
from tests.conftest import AdminSession

APP = settings.app_id
NOW = datetime.now(timezone.utc)
PG = bool(os.environ.get("TEST_DATABASE_URL"))


def _insight(db, user="alice", subject="部署端口", content="部署前先探测端口占用", project=None, embedding=None, **kw):
    fields = {"applies_when": "部署服务绑定端口时"}
    fields.update(kw)
    row = m.MemoryInsight(
        app_id=APP,
        user_id=user,
        kind="pitfall",
        subject=subject,
        content=content,
        valid_from=NOW,
        project=project,
        embedding=embedding,
        embed_model="fake" if embedding is not None else None,
        **fields,
    )
    db.add(row)
    db.commit()
    return row


def _set_switch(db, user, on=True):
    s = db.scalar(select(m.UserSetting).where(m.UserSetting.user_id == user))
    if s is None:
        s = m.UserSetting(app_id=APP, user_id=user, memory_enabled=on, consent_version=CONSENT_VERSION)
        db.add(s)
    else:
        s.memory_enabled = on
    db.commit()


def _job_with_msg(db, user, key, content):
    job = m.MemoryJob(app_id=APP, user_id=user, idempotency_key=key, session_id="s")
    db.add(job)
    db.commit()
    db.add(m.SessionMessage(app_id=APP, user_id=user, session_id="s", job_id=job.id, msg_id="m1", role="user", content=content))
    db.commit()
    return db.get(m.MemoryJob, job.id)


class FakeExtractor:
    def __init__(self, items):
        self.items = items

    def extract(self, text):
        return list(self.items)


# ---------- §1 锚点候选 ----------


def test_candidate_covers_all_same_subject_pairs(db):
    """same_subject 真对（等值/包含/Jaccard 三类）必在候选对内；无关对不在内。"""
    rows = [
        _insight(db, subject="部署端口", content="甲"),
        _insight(db, subject="部署端口", content="乙"),  # 等值
        _insight(db, subject="部署端口选择", content="丙"),  # Jaccard 0.75
        _insight(db, subject="数据备份", content="丁"),
        _insight(db, subject="数据备份策略", content="戊"),  # Jaccard 0.6
        _insight(db, subject="苹果", content="水果"),
        _insight(db, subject="汽车", content="引擎"),  # 无关
    ]
    pairs, deferred = candidate_pairs(rows, budget=100_000)
    assert deferred == 0
    pairset = set(pairs)
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if same_subject(rows[i].subject, rows[j].subject):
                assert (i, j) in pairset, (rows[i].subject, rows[j].subject)
    assert (5, 6) not in pairset  # 苹果/汽车：无共享主题 bigram、无共享内容 token


def test_pair_window_rotates_by_hour():
    pairs = [(i, j) for i in range(10) for j in range(i + 1, 10)]
    assert len(pairs) == 45
    w0, d0 = pair_window(pairs, 10, now=0.0)
    w1, d1 = pair_window(pairs, 10, now=3600.0)
    assert len(w0) == len(w1) == 10 and d0 == d1 == 35
    assert w0 == pairs[:10] and w1 == pairs[10:20]  # 小时旋转，窗口滑动
    assert w0 == sorted(w0) and w1 == sorted(w1)


# ---------- §1 合并语义不变 + 规模 ----------


def test_consolidate_small_matches_old_semantics(db):
    """小规模下候选循环的合并/冲突/跨项目结果与旧全配对一致。"""
    _insight(db, subject="端口探测", content="先探测端口再绑定", confidence=0.8)
    _insight(db, subject="端口探测", content="先探测端口再绑定", confidence=0.5)
    # 跨主题 0.98 合并：主题无共享 bigram（晨跑/夜跑），内容共享 token“运动”
    _insight(db, subject="晨跑", content="运动前热身十分钟", embedding=[1.0, 0.0])
    _insight(db, subject="夜跑", content="运动前热身十分钟", embedding=[1.0, 0.0])
    # 同主题不同内容、无向量 → 冲突 1
    _insight(db, subject="部署端口", content="优先用 8080")
    _insight(db, subject="部署端口", content="绝不要用 8080")
    # 跨项目同主题 → 不合并、不计冲突
    _insight(db, subject="端口探测", content="先探测端口再绑定", project="p1")
    stats = consolidate_insights(db, now=NOW)
    # 冲突 3 = (部署端口对) + (端口探测, 部署端口) ×2：后两者共享“端口”bigram，
    # 旧全配对同样比较并计入——新口径与旧口径一致。
    assert stats["merged"] == 2 and stats["conflicts"] == 3
    alive = list(db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.valid_to.is_(None))))
    assert len(alive) == 5  # 7 行 - 2 次合并


def test_consolidate_scales_subquadratically(db):
    """N=1000/2000 随机主题随机内容 Fake 向量：候选对为 0（锚点无交集），
    耗时比 < 3（平方增长时约 4x）。计时取三次最小值。
    注：本测试曾用 N=300/600、取两次最小值，在全套件里以 3.39 抖动失败
    （绝对耗时仅 9ms/30ms，单次 fsync 抖动即可 swing 比值）——加大 N 与
    取三次最小值是把计时抬出噪声层，不是放宽阈值（阈值仍为 3.0）。"""

    def build(n):
        for i in range(n):
            _insight(db, subject=chr(0x4E00 + i), content=f"{i:04d}")
        return list(db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.valid_to.is_(None))))

    def timed(n):
        rows = build(n)
        cand, _ = candidate_pairs(rows, budget=100_000)
        assert cand == []
        best = None
        for _ in range(3):
            t0 = time.perf_counter()
            consolidate_insights(db, now=NOW)
            dt = time.perf_counter() - t0
            best = dt if best is None else min(best, dt)
        return best

    t1000 = timed(1000)
    # 清库再建 2000 行（fresh_db 只在测试间清库，测试内手动清）
    db.execute(text("DELETE FROM memory_insight"))
    db.commit()
    t2000 = timed(2000)
    assert t2000 / max(t1000, 1e-6) < 3.0


# ---------- §2 任务优先 + 失败退避 ----------


def test_run_cycle_job_first_and_patrol_backoff(db, monkeypatch):
    _set_switch(db, "alice", True)
    order = []

    class RecExtractor(FakeExtractor):
        def extract(self, text):
            order.append("job")
            return super().extract(text)

    job = _job_with_msg(db, "alice", "k1", "部署前先探测端口")
    calls = {"n": 0}

    def boom(db, linker=None, **kw):
        calls["n"] += 1
        order.append("patrol")
        raise RuntimeError("patrol boom")

    monkeypatch.setattr("app.worker.run_patrol", boom)
    state = {"next_patrol": 0.0}
    result = run_cycle(db, RecExtractor([Candidate(kind="fact", subject="端口", content="先探测端口")]), None, None, None, state)
    assert order == ["job", "patrol"]  # 任务先于巡检处理
    assert db.get(m.MemoryJob, job.id).status == "done"
    assert "boom" in result["patrol"]["error"]
    assert state["next_patrol"] > time.time()  # 失败推后一小时，不热重试
    # 间隔内第二轮：巡检不再跑，任务照常处理
    _job_with_msg(db, "alice", "k2", "部署前先探测端口")
    result2 = run_cycle(db, RecExtractor([Candidate(kind="fact", subject="端口", content="先探测端口")]), None, None, None, state)
    assert calls["n"] == 1 and result2["patrol"] is None


# ---------- §4 巡检锁 ----------


def test_patrol_lock_skip_when_held(db, monkeypatch):
    monkeypatch.setattr("app.patrol.try_patrol_lock", lambda db: False)
    assert run_patrol(db) == {"skipped": "patrol_lock"}


@pytest.mark.skipif(PG, reason="SQLite branch only")
def test_patrol_lock_sqlite_noop(db):
    assert try_patrol_lock(db) is True


@pytest.mark.skipif(not PG, reason="needs Postgres")
def test_patrol_lock_pg_exclusive():
    s1 = AdminSession()
    s2 = AdminSession()
    try:
        assert try_patrol_lock(s1) is True
        assert try_patrol_lock(s2) is False  # 另一会话持有，全局锁排他
        release_patrol_lock(s1)
        s1.commit()
        assert try_patrol_lock(s2) is True
    finally:
        release_patrol_lock(s2)
        s1.close()
        s2.close()


# ---------- §5 边唯一索引 + SAVEPOINT ----------


def test_create_link_twice_keeps_one_current(db):
    a = create_link(db, APP, "alice", ("insight", "x1"), ("insight", "x2"), "related", 0.8, "rule")
    b = create_link(db, APP, "alice", ("insight", "x1"), ("insight", "x2"), "related", 0.9, "rule")
    db.commit()
    assert a.id == b.id
    rows = list(db.scalars(select(m.MemoryLink).where(m.MemoryLink.valid_to.is_(None))))
    assert len(rows) == 1


def test_insert_link_savepoint_fallback(db):
    """绕过先查直接调 _insert_link：撞唯一索引后回查既有行，会话仍可用。"""
    a = create_link(db, APP, "alice", ("insight", "x1"), ("insight", "x2"), "related", 0.8, "rule")
    db.commit()
    dup = m.MemoryLink(
        app_id=APP,
        user_id="alice",
        src_kind="insight",
        src_id="x1",
        dst_kind="insight",
        dst_id="x2",
        relation="related",
        strength=0.5,
        origin="rule",
    )
    got = _insert_link(db, dup)
    assert got is not None and got.id == a.id
    # 会话未被失败的插入拖垮：新边照常建
    c = create_link(db, APP, "alice", ("insight", "x1"), ("insight", "x3"), "related", 0.7, "rule")
    assert c is not None and c.id != a.id
    db.commit()


def test_closed_link_allows_new_current(db):
    a = create_link(db, APP, "alice", ("insight", "x1"), ("insight", "x2"), "related", 0.8, "rule")
    db.commit()
    a.valid_to = NOW
    db.commit()
    b = create_link(db, APP, "alice", ("insight", "x1"), ("insight", "x2"), "related", 0.9, "rule")
    assert b is not None and b.id != a.id  # 已关闭边不占唯一位
    db.commit()
    current = list(db.scalars(select(m.MemoryLink).where(m.MemoryLink.valid_to.is_(None))))
    assert len(current) == 1 and current[0].id == b.id


def test_link_dedupe_sql(db):
    """迁移 0025 的去重 SQL 语义：同键多现行行只留 strength 最大者。"""
    db.execute(text("DROP INDEX IF EXISTS ix_link_current_unique"))
    db.commit()
    for strength in (0.5, 0.9, 0.7):
        db.add(
            m.MemoryLink(
                app_id=APP,
                user_id="alice",
                src_kind="insight",
                src_id="x1",
                dst_kind="insight",
                dst_id="x2",
                relation="related",
                strength=strength,
                origin="rule",
            )
        )
    db.commit()
    db.execute(
        text(
            "UPDATE memory_link SET valid_to = CURRENT_TIMESTAMP WHERE id IN ("
            " SELECT id FROM ("
            "  SELECT id, ROW_NUMBER() OVER (PARTITION BY app_id, user_id, src_kind, src_id,"
            "   dst_kind, dst_id, relation ORDER BY strength DESC, created_at ASC) AS rn"
            "  FROM memory_link WHERE valid_to IS NULL"
            " ) WHERE rn > 1)"
        )
    )
    db.commit()
    current = list(db.scalars(select(m.MemoryLink).where(m.MemoryLink.valid_to.is_(None))))
    assert len(current) == 1 and current[0].strength == pytest.approx(0.9)
    # 去重后建索引必须成功（否则迁移 0025 会在脏库上失败）
    db.execute(
        text(
            "CREATE UNIQUE INDEX ix_link_current_unique ON memory_link"
            " (app_id, user_id, src_kind, src_id, dst_kind, dst_id, relation)"
            " WHERE valid_to IS NULL"
        )
    )
    db.commit()


# ---------- §6 租约续租 ----------


def test_renew_lease_guards(db):
    _set_switch(db, "alice", True)
    job = _job_with_msg(db, "alice", "k1", "x")
    claimed = claim_job(db)
    assert claimed is not None and claimed.id == job.id
    assert renew_lease(db, job.id) is True  # 未过期可续
    done = db.get(m.MemoryJob, job.id)
    done.status = "done"
    db.commit()
    assert renew_lease(db, job.id) is False  # 已终态不续
    done.status = "running"
    done.locked_until = NOW - timedelta(seconds=10)
    db.commit()
    assert renew_lease(db, job.id) is False  # 已过期不续（防抢回他人任务）


def test_lease_renewer_keeps_job_claimed(db):
    _set_switch(db, "alice", True)
    job = _job_with_msg(db, "alice", "k1", "x")
    claimed = claim_job(db)
    assert claimed is not None
    renewer = LeaseRenewer(db, job.id, interval=0.05).start()
    try:
        time.sleep(0.3)  # 多个续租周期，远超单次续租间隔
        db.expire_all()
        row = db.get(m.MemoryJob, job.id)
        # SQLite 读回 datetime 丢 tzinfo，先补 UTC 再比（与 episodes._aware 同口径）
        locked = row.locked_until
        if locked.tzinfo is None:
            locked = locked.replace(tzinfo=timezone.utc)
        assert locked > datetime.now(timezone.utc)
        assert claim_job(db) is None  # 续租中，第二领取拿不到该任务
    finally:
        renewer.stop()
    assert not renewer._thread.is_alive()


# ---------- §3 分片提交 ----------


def test_consolidate_commits_per_user(db):
    _insight(db, user="alice", subject="主题甲", content="甲内容")
    _insight(db, user="bob", subject="主题乙", content="乙内容")
    commits = {"n": 0}

    def _count(session):
        commits["n"] += 1

    event.listen(db, "after_commit", _count)
    try:
        consolidate_insights(db, now=NOW)
    finally:
        event.remove(db, "after_commit", _count)
    # 衰减提交 + 每用户组提交 + 最终提交 ≥ 3
    assert commits["n"] >= 3
