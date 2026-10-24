"""第十六轮「信任三件套」：自动结果回流、提炼证据校验、矛盾裁决与真遗忘。

审查（类人记忆）点名的三个 P0/P1：反馈全靠自觉（回流缺失 + 曝光续命的马太
回路）、提炼无校验（假经验平权入场）、矛盾只数不裁 + 衰减不退出（假遗忘）。
本轮考：简报递出有账、任务成败自动结账且只结一次、曝光不再刷新活跃时间、
经验带轨迹引文验真、验不过降权带标、矛盾证据悬殊时自动裁决、闲置半年且
从未被用过的经验真退出召回。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.distill import InsightCandidate, parse_insights
from app.episodes import (
    brief_for_task,
    consolidate_insights,
    record_feedback,
    run_episode_once,
    submit_episode,
)
from app.links import create_link

U = UserContext(app_id=settings.app_id, user_id="alice")
NOW = datetime.now(timezone.utc)
APP = settings.app_id


class FakeDistiller:
    def __init__(self, items):
        self.items = items

    def distill(self, text):
        return list(self.items)


def _insight(db, subject="部署端口", content="部署前先探测端口占用，避开常用端口",
             applies="部署服务绑定端口时", user=U, **kw):
    row = m.MemoryInsight(app_id=user.app_id, user_id=user.user_id, kind="pitfall",
                          subject=subject, content=content, applies_when=applies, **kw)
    db.add(row)
    db.commit()
    return row


def _ep(db, outcome="success", project=None, title="部署服务"):
    return submit_episode(db, U, source="test", title=title, outcome=outcome,
                          steps=[{"action": "探测端口后启动", "observation": "成功"}],
                          cost={}, project=project)


def _delivery(db):
    return db.scalars(select(m.BriefDelivery)).first()


# ---------- 自动结果回流 ----------

def test_backflow_success_credits_delivered_insights(db):
    row = _insight(db)
    out = brief_for_task(db, U, "部署服务绑定端口", 5)
    assert [o.insight_id for o in out] == [row.id]
    d = _delivery(db)
    assert d is not None and d.consumed_at is None and d.insight_ids == [row.id]
    _ep(db, outcome="success")
    db.expire_all()
    row = db.get(m.MemoryInsight, row.id)
    assert row.applied_count == 1 and row.auto_pos == 1 and row.auto_neg == 0
    # 置信度：无人工反馈、auto_pos=1 半权 → (1+0.5)/(2+0.5) = 0.6
    assert row.confidence == 0.6
    assert row.last_used_at is not None  # 应用才刷新活跃时间
    assert _delivery(db).consumed_at is not None


def test_backflow_failure_counts_auto_neg(db):
    row = _insight(db)
    brief_for_task(db, U, "部署服务绑定端口", 5)
    _ep(db, outcome="failure")
    db.expire_all()
    row = db.get(m.MemoryInsight, row.id)
    assert row.applied_count == 1 and row.auto_neg == 1
    assert row.confidence == 0.4  # (1+0)/(2+0.5)


def test_backflow_partial_only_counts_applied(db):
    row = _insight(db)
    brief_for_task(db, U, "部署服务绑定端口", 5)
    _ep(db, outcome="partial")
    db.expire_all()
    row = db.get(m.MemoryInsight, row.id)
    assert row.applied_count == 1 and row.auto_pos == 0 and row.auto_neg == 0
    assert row.confidence == 0.5


def test_backflow_consumes_delivery_once(db):
    row = _insight(db)
    brief_for_task(db, U, "部署服务绑定端口", 5)
    _ep(db, outcome="success")
    _ep(db, outcome="success", title="又一次部署")
    db.expire_all()
    assert db.get(m.MemoryInsight, row.id).applied_count == 1  # 同一份简报只结一次账


def test_backflow_respects_project(db):
    row = _insight(db, project="repo-a")
    brief_for_task(db, U, "部署服务绑定端口", 5, project="repo-a")
    _ep(db, outcome="success", project="repo-b")  # 项目不符：不结账，交付留着
    db.expire_all()
    assert db.get(m.MemoryInsight, row.id).applied_count == 0
    assert _delivery(db).consumed_at is None
    _ep(db, outcome="success", project="repo-a")
    db.expire_all()
    assert db.get(m.MemoryInsight, row.id).applied_count == 1


def test_backflow_credits_successor_version(db):
    old = _insight(db, content="旧版指导：部署前先探测端口占用")
    brief_for_task(db, U, "部署服务绑定端口", 5)
    # 交付之后经验被更新：旧行关行、新行接棒——功过记到活着的那行
    new = _insight(db, content="新版指导：部署前先探测端口占用并预留回退端口", version=2)
    old.valid_to = NOW
    db.commit()
    _ep(db, outcome="success")
    db.expire_all()
    assert db.get(m.MemoryInsight, new.id).applied_count == 1
    assert db.get(m.MemoryInsight, old.id).applied_count == 0


def test_feedback_recalc_includes_auto_signals(db):
    row = _insight(db, auto_pos=2)
    record_feedback(db, U, row.id, helpful=True)
    db.commit()
    db.expire_all()
    row = db.get(m.MemoryInsight, row.id)
    # pos=1, auto_pos=2(半权=1) → (1+1+1)/(2+1+1) = 0.75
    assert row.confidence == 0.75
    assert row.last_used_at is not None  # 人工反馈也算真使用


# ---------- 提炼证据校验（verifier） ----------

def test_parse_insights_keeps_evidence():
    items = parse_insights({"items": [{
        "kind": "pitfall", "subject": "端口占用", "content": "先探测端口",
        "applies_when": "部署时", "evidence": "在 8080 启动失败 EADDRINUSE"}]})
    assert items[0].evidence == "在 8080 启动失败 EADDRINUSE"


def test_distill_with_verbatim_evidence_is_verified(db):
    submit_episode(db, U, source="test", title="部署服务", outcome="failure",
                   steps=[{"action": "在 8080 启动服务", "error": "EADDRINUSE 端口被占用"}],
                   cost={})
    cand = InsightCandidate(
        kind="pitfall", subject="部署端口", content="部署前先探测端口占用",
        applies_when="部署服务时", evidence="在 8080 启动服务")
    run_episode_once(db, FakeDistiller([cand]))
    row = db.scalars(select(m.MemoryInsight)).first()
    assert row is not None and row.verified is True


def test_distill_with_fabricated_evidence_is_unverified(db):
    submit_episode(db, U, source="test", title="部署服务", outcome="failure",
                   steps=[{"action": "在 8080 启动服务", "error": "EADDRINUSE"}], cost={})
    cand = InsightCandidate(
        kind="pitfall", subject="部署端口", content="部署前先探测端口占用",
        applies_when="部署服务时", evidence="数据库连接池耗尽导致超时重试风暴")
    run_episode_once(db, FakeDistiller([cand]))
    row = db.scalars(select(m.MemoryInsight)).first()
    assert row is not None and row.verified is False  # 不丢弃，带标入场


def test_distill_without_evidence_is_unverified(db):
    submit_episode(db, U, source="test", title="部署服务", outcome="success",
                   steps=[{"action": "直接启动", "observation": "成功"}], cost={})
    cand = InsightCandidate(kind="strategy", subject="部署流程",
                            content="部署前先探测端口", applies_when="部署时")
    run_episode_once(db, FakeDistiller([cand]))
    row = db.scalars(select(m.MemoryInsight)).first()
    assert row is not None and row.verified is False


def test_unverified_discounted_in_brief_ranking(db):
    good = _insight(db, subject="部署端口甲", verified=True)
    bad = _insight(db, subject="部署端口乙", verified=False)
    out = brief_for_task(db, U, "部署端口怎么处理", 5)
    assert [o.insight_id for o in out] == [good.id, bad.id]
    assert out[0].score > out[1].score
    assert out[0].verified is True and out[1].verified is False


# ---------- 矛盾裁决 ----------

def test_adjudication_closes_loser_and_link(db):
    win = _insight(db, subject="端口策略甲", content="优先使用 8080",
                   confidence=0.9, feedback_pos=3)
    lose = _insight(db, subject="端口策略乙", content="绝不要用 8080",
                    confidence=0.4)
    link = create_link(db, U.app_id, U.user_id, ("insight", win.id),
                       ("insight", lose.id), "contradicts", 0.6, "rule")
    db.commit()
    stats = consolidate_insights(db, now=NOW)
    assert stats["adjudicated"] == 1
    db.expire_all()
    assert db.get(m.MemoryInsight, lose.id).valid_to is not None
    assert db.get(m.MemoryInsight, win.id).valid_to is None
    assert db.get(m.MemoryLink, link.id).valid_to is not None


def test_adjudication_needs_gap_and_evidence(db):
    a = _insight(db, subject="策略甲", content="这样做", confidence=0.55, feedback_pos=3)
    b = _insight(db, subject="策略乙", content="不要这样做", confidence=0.45)
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", b.id),
                "contradicts", 0.6, "rule")
    db.commit()
    stats = consolidate_insights(db, now=NOW)  # 差距 0.1 < 0.30：证据再足也不裁
    assert stats["adjudicated"] == 0
    assert db.get(m.MemoryInsight, b.id).valid_to is None


def test_adjudication_spares_pinned_loser(db):
    win = _insight(db, subject="策略甲", content="这样做", confidence=0.9, feedback_pos=3)
    lose = _insight(db, subject="策略乙", content="不要这样做", confidence=0.3, pinned=True)
    create_link(db, U.app_id, U.user_id, ("insight", win.id), ("insight", lose.id),
                "contradicts", 0.6, "rule")
    db.commit()
    stats = consolidate_insights(db, now=NOW)  # 置顶是用户亲手策展的，永不自动裁
    assert stats["adjudicated"] == 0
    assert db.get(m.MemoryInsight, lose.id).valid_to is None


# ---------- 真遗忘 ----------

def test_retire_stale_never_applied(db):
    # 四条内容互不相同：防的是被合并环节先关行，测不到遗忘环节
    stale = _insight(db, subject="陈年经验", content="陈年做法甲：早就没人这么干了",
                     valid_from=NOW - timedelta(days=200))
    applied = _insight(db, subject="用过的经验", content="用过的做法乙：最近还在被应用",
                        valid_from=NOW - timedelta(days=200), applied_count=1)
    endorsed = _insight(db, subject="被夸过的经验", content="被夸的做法丙：有人给过好评",
                         valid_from=NOW - timedelta(days=200), feedback_pos=1)
    fresh = _insight(db, subject="新经验", content="新做法丁：刚提炼出来")
    stats = consolidate_insights(db, now=NOW)
    assert stats["retired"] == 1
    db.expire_all()
    assert db.get(m.MemoryInsight, stale.id).valid_to is not None
    for alive in (applied, endorsed, fresh):
        assert db.get(m.MemoryInsight, alive.id).valid_to is None
