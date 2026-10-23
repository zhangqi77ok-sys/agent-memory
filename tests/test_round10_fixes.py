"""第十轮回归：反馈闭环（计数/置信度/继承）与项目作用域（继承/加权/不排除）。

核心断言：经验的声誉属于血统不属于某一行；门槛仍是原始相关性，
置信度与项目只动排序不动入选——标定结论（COS_MIN/MIN_SCORE）不被污染。"""
import pytest
from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.distill import InsightCandidate
from app.episodes import brief_for_task, record_feedback, run_episode_once, submit_episode
from tests.conftest import consent

U = UserContext(app_id=settings.app_id, user_id="alice")
BOB = UserContext(app_id=settings.app_id, user_id="bob")

CAND = InsightCandidate(
    kind="pitfall", subject="部署端口选择",
    content="部署前先探测端口占用，8080、8081 常被占用，不要逐个试错",
    applies_when="部署或启动本地服务、需要绑定端口时")


class FakeDistiller:
    def __init__(self, items):
        self.items = items

    def distill(self, text):
        return list(self.items)


def _ep(db, project=None, title="部署服务"):
    return submit_episode(db, U, source="test", title=title, outcome="partial",
                          steps=[{"action": "在 8080 启动", "error": "EADDRINUSE"}],
                          cost={}, project=project)


def _insight(db):
    return db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.valid_to.is_(None))).first()


def test_project_inherited_from_episode_to_insight(db):
    _ep(db, project="repo-a")
    assert run_episode_once(db, FakeDistiller([CAND])) is not None
    row = _insight(db)
    assert row.project == "repo-a" and row.confidence == 0.5 and row.use_count == 0


def test_feedback_math_and_success_link(db):
    _ep(db)
    run_episode_once(db, FakeDistiller([CAND]))
    row = _insight(db)
    record_feedback(db, U, row.id, helpful=True, task_outcome="success")
    db.commit()
    assert row.feedback_pos == 1 and row.success_count == 1
    assert row.confidence == pytest.approx(2 / 3)
    record_feedback(db, U, row.id, helpful=False)
    record_feedback(db, U, row.id, helpful=False)
    db.commit()
    assert row.feedback_neg == 2 and row.confidence == pytest.approx(2 / 5)


def test_feedback_rejects_other_users_insight(db):
    _ep(db)
    run_episode_once(db, FakeDistiller([CAND]))
    row = _insight(db)
    with pytest.raises(ValueError):
        record_feedback(db, BOB, row.id, helpful=True)


def test_reputation_inherited_on_new_version(db):
    """同主题新内容产生 v2 时，反馈计数与置信度跟着走，不清零。"""
    _ep(db)
    run_episode_once(db, FakeDistiller([CAND]))
    v1 = _insight(db)
    record_feedback(db, U, v1.id, helpful=True, task_outcome="success")
    record_feedback(db, U, v1.id, helpful=True)
    db.commit()
    better = InsightCandidate(kind="pitfall", subject="部署端口选择",
                              content="部署前先用脚本批量探测端口段，避开 8080、8081、3000",
                              applies_when="部署或启动本地服务、需要绑定端口时")
    _ep(db, title="再次部署")
    run_episode_once(db, FakeDistiller([better]))
    v2 = _insight(db)
    assert v2.version == 2 and v2.feedback_pos == 2 and v2.success_count == 1
    assert v2.confidence == pytest.approx(3 / 4)


def _two_project_insights(db):
    """两条除项目外完全同文的经验：基线分相同，排序差异只能来自项目乘子。"""
    for proj in ("repo-a", "repo-b"):
        db.add(m.MemoryInsight(app_id=settings.app_id, user_id="alice", kind="pitfall",
                               subject="部署端口选择",
                               content="部署前先探测端口占用，8080、8081 常被占用",
                               applies_when="部署服务绑定端口时", project=proj))
    db.commit()


def test_brief_project_weighting_not_exclusion(db):
    _two_project_insights(db)
    out = brief_for_task(db, U, "部署服务需要绑定端口", 5, project="repo-a")
    assert len(out) == 2  # 跨项目只是降权，不排除
    assert out[0].project == "repo-a" and out[1].project == "repo-b"
    assert out[0].score > out[1].score
    # 不带项目时两条同分（乘子不启用）
    out2 = brief_for_task(db, U, "部署服务需要绑定端口", 5)
    assert out2[0].score == out2[1].score


def test_brief_confidence_weighting_and_provenance(db):
    _two_project_insights(db)
    rows = list(db.scalars(select(m.MemoryInsight)))
    low, high = rows[0], rows[1]
    for _ in range(3):
        record_feedback(db, U, low.id, helpful=False)
    for _ in range(3):
        record_feedback(db, U, high.id, helpful=True, task_outcome="success")
    db.commit()
    out = brief_for_task(db, U, "部署服务需要绑定端口", 5)
    assert out[0].insight_id == high.id and out[0].confidence == pytest.approx(0.8)
    assert out[1].insight_id == low.id and out[1].confidence == pytest.approx(0.2)
    assert out[0].source_episode_id is None and out[0].project in ("repo-a", "repo-b")


def test_brief_counts_exposure(db):
    _ep(db)
    run_episode_once(db, FakeDistiller([CAND]))
    row = _insight(db)
    out = brief_for_task(db, U, "部署新服务，需要绑定端口", 5)
    db.commit()
    assert len(out) == 1 and out[0].source_episode_id is not None
    assert row.use_count == 1 and row.last_used_at is not None


def test_rest_feedback_roundtrip(client, db):
    consent(client, "alice", "bob")
    _ep(db)
    run_episode_once(db, FakeDistiller([CAND]))
    row = _insight(db)
    r = client.post("/v1/insights/feedback", json={
        "user_id": "alice", "insight_id": row.id, "helpful": True, "task_outcome": "success"})
    assert r.status_code == 200 and r.json()["confidence"] == pytest.approx(2 / 3, abs=1e-3)
    # 别人的经验：404，不泄露存在性
    r = client.post("/v1/insights/feedback", json={
        "user_id": "bob", "insight_id": row.id, "helpful": True})
    assert r.status_code == 404


def test_mcp_feedback_and_project_flow(client, db, monkeypatch):
    pytest.importorskip("mcp")
    import app.mcp_server as ms
    monkeypatch.setenv("MEMORY_USER_ID", "alice")
    consent(client, "alice")
    sub = ms.memory_episode_submit("部署服务", outcome="partial",
                                   steps=[{"action": "在 8080 启动", "error": "EADDRINUSE"}],
                                   project="repo-a")
    assert sub["skipped"] is False
    ep = db.get(m.TaskEpisode, sub["episode_id"])
    assert ep.project == "repo-a"
    run_episode_once(db, FakeDistiller([CAND]))
    row = _insight(db)
    fb = ms.memory_feedback(row.id, True, "success")
    assert fb["ok"] is True and fb["confidence"] == pytest.approx(2 / 3, abs=1e-3)
    assert ms.memory_feedback("nonexistent-id", True)["ok"] is False
