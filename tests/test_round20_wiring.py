"""第二十轮第二阶段：接线测试——边从使用中学习、网结构接口、织网简报、
矛盾条件分化、抽象阶梯。

接线点：
- 简报扩散记边（brief_delivery_edge）→ 回流/人工反馈按边改权；
- 闲置衰减结清锚点（decayed_until）保证同一段闲置只扣一次；
- GET /v1/me/graph/stats 与简报 graph 段；
- 裁决：有 distinct 适用情境且有证据的输家分化保留，不退役；
- 原则胜出时带上泛化支撑经验（reason=principle_support）。
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.edge_learning import decay_idle_edges
from app.episodes import (
    brief_for_task,
    consolidate_insights,
    record_feedback,
    submit_episode,
)
from app.links import create_link

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def _insight(db, subject, content, kind="strategy", project=None, applies="",
             confidence=0.5, feedback_pos=0, pinned=False):
    row = m.MemoryInsight(app_id=APP, user_id="alice", kind=kind, subject=subject,
                          content=content, project=project, applies_when=applies,
                          confidence=confidence, feedback_pos=feedback_pos,
                          pinned=pinned)
    db.add(row)
    db.flush()
    return row


def _spread_pair(db, strength=0.9):
    """A 直达命中 + B 经 related 边扩散带出（复用第十五轮的召回口径）。"""
    a = _insight(db, "部署端口选择", "部署前先探测端口占用再启动服务", applies="部署服务时")
    b = _insight(db, "日志保留策略", "日志按日轮转保留七天避免磁盘写满")
    link = create_link(db, APP, "alice", ("insight", a.id), ("insight", b.id),
                       "related", strength, "manual")
    db.commit()
    out = brief_for_task(db, U, "部署服务要绑定端口", 5)
    by_id = {o.insight_id: o for o in out}
    assert by_id[b.id].reason == "linked" and by_id[b.id].linked_from == a.id
    return a, b, link, out


def _delivery_edge(db, insight_id):
    rows = db.scalars(select(m.BriefDeliveryEdge).where(
        m.BriefDeliveryEdge.insight_id == insight_id)).all()
    assert len(rows) == 1
    return rows[0]


# ---------- 简报扩散记边 ----------

def test_brief_records_delivery_edge(db):
    a, b, link, _ = _spread_pair(db)
    erow = _delivery_edge(db, b.id)
    assert erow.link_id == link.id
    assert erow.applied is False
    # 直达的 A 没有交付边
    assert db.scalars(select(m.BriefDeliveryEdge).where(
        m.BriefDeliveryEdge.insight_id == a.id)).all() == []


# ---------- 回流按边改权 ----------

def _submit(db, outcome):
    return submit_episode(db, U, source="api", title="部署服务", outcome=outcome,
                          steps=[{"action": "部署"}], cost={"tokens": 10, "retries": 0})


def test_backflow_strengthens_edge_on_success(db):
    _, b, link, _ = _spread_pair(db, strength=0.9)
    _submit(db, "success")
    db.expire_all()
    assert db.get(m.MemoryLink, link.id).strength == 0.91
    assert _delivery_edge(db, b.id).applied is True


def test_backflow_weakens_edge_on_failure(db):
    _, b, link, _ = _spread_pair(db, strength=0.9)
    _submit(db, "failure")
    db.expire_all()
    assert db.get(m.MemoryLink, link.id).strength == 0.81
    assert _delivery_edge(db, b.id).applied is True


def test_backflow_partial_marks_applied_without_strength_change(db):
    _, b, link, _ = _spread_pair(db, strength=0.9)
    _submit(db, "partial")
    db.expire_all()
    assert db.get(m.MemoryLink, link.id).strength == 0.9
    assert _delivery_edge(db, b.id).applied is True


# ---------- 人工反馈按边改权 ----------

def test_manual_feedback_moves_edge(db):
    _, _, link, _ = _spread_pair(db, strength=0.9)
    record_feedback(db, U, _spread_b_id(db), helpful=True)
    db.expire_all()
    assert db.get(m.MemoryLink, link.id).strength == 0.91


def _spread_b_id(db):
    rows = db.scalars(select(m.BriefDeliveryEdge)).all()
    assert len(rows) == 1
    return rows[0].insight_id


def test_manual_feedback_negative_weakens_edge(db):
    _, _, link, _ = _spread_pair(db, strength=0.9)
    record_feedback(db, U, _spread_b_id(db), helpful=False)
    db.expire_all()
    assert db.get(m.MemoryLink, link.id).strength == 0.81


def test_manual_feedback_without_delivery_edge_leaves_edges(db):
    a = _insight(db, "单条经验", "部署前先探测端口占用")
    db.commit()
    record_feedback(db, U, a.id, helpful=True)
    db.commit()
    assert db.scalars(select(m.BriefDeliveryEdge)).all() == []


# ---------- 闲置衰减结清锚点 ----------

def test_decay_idle_edges_anchor_prevents_double_decay(db):
    a = _insight(db, "陈年甲", "陈年做法甲")
    b = _insight(db, "陈年乙", "陈年做法乙")
    link = create_link(db, APP, "alice", ("insight", a.id), ("insight", b.id),
                       "related", 0.9, "rule")
    db.flush()
    link.created_at = NOW - timedelta(days=60)
    db.commit()
    assert decay_idle_edges(db, now=NOW) == 1
    db.commit()  # decay 只改不提交，调用方负责 commit
    db.expire_all()
    assert db.get(m.MemoryLink, link.id).strength == pytest.approx(0.9 * 0.95 ** 2)
    # 同一时刻再结清：锚点已到 NOW，不足整月，不再扣
    assert decay_idle_edges(db, now=NOW) == 0
    db.commit()
    db.expire_all()
    assert db.get(m.MemoryLink, link.id).strength == pytest.approx(0.9 * 0.95 ** 2)


def test_decay_idle_edges_skips_recently_delivered(db):
    _, _, link, _ = _spread_pair(db, strength=0.9)
    db.flush()
    link.created_at = NOW - timedelta(days=60)
    db.commit()
    # 交付边是刚建的（简报时）：基准取最近交付，不足整月，不衰减
    assert decay_idle_edges(db, now=NOW) == 0
    db.commit()


# ---------- 网结构接口 ----------

def test_graph_stats_endpoint(db, client):
    from tests.conftest import consent

    consent(client, "alice")
    a = _insight(db, "部署端口选择", "部署前先探测端口占用")
    b = _insight(db, "日志保留策略", "日志按日轮转保留七天")
    c = _insight(db, "回滚流程", "回滚先停流量再切版本")
    create_link(db, APP, "alice", ("insight", a.id), ("insight", b.id),
                "supports", 0.9, "rule")
    create_link(db, APP, "alice", ("insight", b.id), ("insight", c.id),
                "related", 0.8, "rule")
    db.commit()
    r = client.get("/v1/me/graph/stats", params={"user_id": "alice"})
    assert r.status_code == 200
    st = r.json()
    assert st["node_count"] == 3 and st["edge_count"] == 2
    assert st["orphan_count"] == 0
    assert st["hubs"][0]["subject"] == "日志保留策略"


# ---------- 织网简报 graph 段 ----------

def test_brief_graph_section(db, client):
    from tests.conftest import consent

    consent(client, "alice")
    a, b, link, _ = _spread_pair(db)
    r = client.post("/v1/brief", json={"user_id": "alice", "task": "部署服务要绑定端口"})
    assert r.status_code == 200
    graph = r.json()["graph"]
    assert {n["id"] for n in graph["nodes"]} == {a.id, b.id}
    assert len(graph["edges"]) == 1
    edge = graph["edges"][0]
    assert edge["link_id"] == link.id and edge["relation"] == "related"
    assert {edge["src"], edge["dst"]} == {a.id, b.id}


# ---------- 矛盾条件分化 ----------

def test_adjudication_differentiates_condition_split_loser(db):
    win = _insight(db, subject="端口策略甲", content="优先使用 8080",
                   confidence=0.9, feedback_pos=3, applies="生产部署时")
    lose = _insight(db, subject="端口策略乙", content="绝不要用 8080",
                    confidence=0.4, feedback_pos=1, applies="仅测试环境")
    link = create_link(db, APP, "alice", ("insight", win.id), ("insight", lose.id),
                       "contradicts", 0.6, "rule")
    db.commit()
    stats = consolidate_insights(db, now=NOW)
    assert stats["differentiated"] == 1 and stats["adjudicated"] == 0
    db.expire_all()
    assert db.get(m.MemoryInsight, lose.id).valid_to is None
    assert db.get(m.MemoryLink, link.id).valid_to is not None
    related = [lk for lk in db.scalars(select(m.MemoryLink).where(
        m.MemoryLink.relation == "related", m.MemoryLink.valid_to.is_(None))).all()]
    assert len(related) == 1
    assert {related[0].src_id, related[0].dst_id} == {win.id, lose.id}


def test_adjudication_still_retires_same_condition_loser(db):
    win = _insight(db, subject="端口策略甲", content="优先使用 8080",
                   confidence=0.9, feedback_pos=3, applies="部署服务时")
    lose = _insight(db, subject="端口策略乙", content="绝不要用 8080",
                    confidence=0.4, feedback_pos=1, applies="部署服务时")
    link = create_link(db, APP, "alice", ("insight", win.id), ("insight", lose.id),
                       "contradicts", 0.6, "rule")
    db.commit()
    stats = consolidate_insights(db, now=NOW)
    assert stats["adjudicated"] == 1 and stats["differentiated"] == 0
    db.expire_all()
    assert db.get(m.MemoryInsight, lose.id).valid_to is not None
    assert db.get(m.MemoryLink, link.id).valid_to is not None


# ---------- 抽象阶梯 ----------

def test_principle_brief_carries_supporting_insights(db):
    principle = _insight(db, "部署端口原则", "部署前先探测端口占用再启动服务",
                         kind="principle", project=None)
    concrete = _insight(db, "日志保留策略", "日志按日轮转保留七天避免磁盘写满",
                        project="p1")
    create_link(db, APP, "alice", ("insight", principle.id),
                ("insight", concrete.id), "generalizes", 0.8, "llm")
    db.commit()
    # k=1：原则独占胜出名额，扩散来不及带支撑——阶梯路径才真正被测到
    out = brief_for_task(db, U, "部署服务要绑定端口", 1, project="p1")
    by_id = {o.insight_id: o for o in out}
    assert principle.id in by_id
    assert by_id[concrete.id].reason == "principle_support"
    assert by_id[concrete.id].linked_from == principle.id
    # 支撑是脚注，不进交付记录（回流只给胜出的原则结账）
    delivery = db.scalars(select(m.BriefDelivery)).all()
    assert len(delivery) == 1
    assert concrete.id not in delivery[0].insight_ids
    assert principle.id in delivery[0].insight_ids
