"""第十一轮回归：经验生命周期（衰减/合并/矛盾计数）、可见可编辑（列表/单条策展）、
简报三要素（新近性）与置顶必达/禁用不召回。"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.episodes import brief_for_task, consolidate_insights
from tests.conftest import consent, uid

U = UserContext(app_id=settings.app_id, user_id="alice")
NOW = datetime.now(timezone.utc)
APP = settings.app_id


def _insight(db, subject="部署端口选择", content="部署前先探测端口占用，避开 8080",
             days_ago=0, **kw):
    fields = {"applies_when": "部署服务绑定端口时"}
    fields.update(kw)
    row = m.MemoryInsight(
        app_id=APP, user_id="alice", kind="pitfall", subject=subject, content=content,
        valid_from=NOW - timedelta(days=days_ago), **fields)
    db.add(row)
    db.commit()
    return row


def _active(db):
    return list(db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.valid_to.is_(None))))


def test_decay_after_grace_period(db):
    old = _insight(db, days_ago=100, confidence=0.8, feedback_pos=3, subject="老经验")
    _insight(db, days_ago=10, confidence=0.8, subject="新经验")
    stats = consolidate_insights(db, now=NOW)
    assert stats["decayed"] == 1
    # 闲置 100 天：宽限 30 天后按 (100-1)//30=3 段衰减；第二十二轮起月因子
    # 随复述变慢：3 次好评 → 因子 1 - 0.05/4 = 0.9875，不再是旧的 0.95
    assert old.confidence == pytest.approx(0.8 * 0.9875 ** 3, abs=1e-3)
    assert old.feedback_pos == 3  # 衰减只压置信度，不动证据计数


def test_merge_duplicates_keeps_higher_confidence(db):
    a = _insight(db, subject="端口探测", content="先探测端口再绑定", confidence=0.8,
                 feedback_pos=3, use_count=2)
    b = _insight(db, subject="端口探测", content="先探测端口再绑定", confidence=0.5,
                 feedback_pos=1, use_count=5)
    stats = consolidate_insights(db, now=NOW)
    assert stats["merged"] == 1
    alive = _active(db)
    assert len(alive) == 1 and alive[0].id == a.id
    assert alive[0].feedback_pos == 4 and alive[0].use_count == 7  # 计数并入保留行
    assert db.scalar(select(m.AuditLog).where(m.AuditLog.action == "consolidate")) is not None
    assert b.valid_to is not None


def test_conflict_counted_not_auto_resolved(db):
    _insight(db, subject="部署端口选择", content="优先用 8080，团队约定")
    _insight(db, subject="部署端口", content="绝不要用 8080，经常被占")
    stats = consolidate_insights(db, now=NOW)
    assert stats["conflicts"] == 1 and stats["merged"] == 0
    assert len(_active(db)) == 2  # 矛盾只上报，不自动裁决


def test_list_and_patch_insights(client, db):
    consent(client, "alice", "bob")
    row = _insight(db)
    row.embedding = [0.1, 0.2]
    row.embed_model = "fake"
    db.commit()
    body = client.get("/v1/me/insights", params=uid("alice")).json()
    assert len(body) == 1 and body[0]["subject"] == "部署端口选择"
    assert body[0]["confidence"] == 0.5 and body[0]["pinned"] is False
    # 改内容 → 旧向量作废（匹配面变了）
    r = client.patch(f"/v1/me/insights/{row.id}", params=uid("alice"),
                     json={"content": "改完的新指导", "pinned": True})
    assert r.status_code == 200 and r.json()["content"] == "改完的新指导"
    assert r.json()["pinned"] is True
    db.refresh(row)
    assert row.embedding is None and row.embed_model is None
    # 别人的：404
    r = client.patch(f"/v1/me/insights/{row.id}", params=uid("bob"), json={"muted": True})
    assert r.status_code == 404
    assert client.get("/v1/me/insights", params=uid("bob")).json() == []


def test_brief_muted_excluded_pinned_forced(db):
    _insight(db, subject="部署端口选择", content="部署前先探测端口占用，避开 8080",
             muted=True)
    _insight(db, subject="完全无关的主题", content="这条与任务毫无关系但被置顶",
             applies_when="任何时候", pinned=True)
    _insight(db, subject="端口绑定策略", content="部署绑定端口前先探测占用情况",
             applies_when="部署服务时")
    out = brief_for_task(db, U, "部署服务需要绑定端口", 5)
    subjects = [o.subject for o in out]
    assert "部署端口选择" not in subjects          # 禁用：哪怕最相关也不出
    assert "完全无关的主题" in subjects            # 置顶：不相关也必达
    pinned_out = next(o for o in out if o.subject == "完全无关的主题")
    assert pinned_out.reason == "pinned"
    assert subjects[0] == "完全无关的主题"          # 置顶排在相关性命中之前


def test_brief_recency_orders_fresh_first(db):
    _insight(db, subject="端口经验旧", content="部署前先探测端口占用情况", days_ago=400)
    _insight(db, subject="端口经验新", content="部署前先探测端口占用情况", days_ago=0)
    out = brief_for_task(db, U, "部署服务需要绑定端口", 5)
    assert [o.subject for o in out] == ["端口经验新", "端口经验旧"]
    assert out[0].score > out[1].score
