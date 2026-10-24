"""第二十一轮：睡眠巩固——共用强化/建边、死边剪除、事实合并、共用簇抽象。

每项任务的门限都在设计文档里（docs/sleep-round21-design.md）：
强化只看水位后的窗口、建边要全量同现 ≥ 2 次、剪边要同时满足下限+
闲置 3 月+从未被证实、事实合并只认同主题、簇抽象要有现成原则覆盖
检查。测试逐项断言门限与反例。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.links import create_link
from app.sleep import sleep_consolidate

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")
NOW = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)


def _insight(db, subject, content, project=None):
    row = m.MemoryInsight(app_id=APP, user_id="alice", kind="strategy",
                          subject=subject, content=content, project=project)
    db.add(row)
    db.flush()
    return row


def _fact(db, subject, content, support=1, vec=None):
    row = m.MemoryFact(app_id=APP, user_id="alice", subject=subject,
                       content=content, support_count=support)
    if vec is not None:
        row.embedding = vec
        row.embed_model = "test"
    db.add(row)
    db.flush()
    return row


def _deliver(db, ids):
    row = m.BriefDelivery(app_id=APP, user_id="alice", insight_ids=list(ids))
    db.add(row)
    db.flush()
    return row


def _active_links(db):
    return list(db.scalars(select(m.MemoryLink).where(
        m.MemoryLink.valid_to.is_(None))))


def _reports(db):
    return list(db.scalars(select(m.ConsolidationReport).where(
        m.ConsolidationReport.user_id == "alice")))


# ---------- 共用强化 ----------

def test_couse_strengthens_edge_once_per_run(db):
    a = _insight(db, "部署端口", "部署前先探测端口占用再启动")
    b = _insight(db, "日志保留", "日志按日轮转保留七天")
    link = create_link(db, APP, "alice", ("insight", a.id), ("insight", b.id),
                       "related", 0.5, "manual")
    _deliver(db, [a.id, b.id])
    _deliver(db, [a.id, b.id])
    db.commit()
    stats = sleep_consolidate(db)
    assert stats["strengthened"] == 1
    assert abs(db.get(m.MemoryLink, link.id).strength - 0.55) < 1e-9
    assert len(_reports(db)) == 1
    # 第二轮：水位已过，无新交付 → 不再重复加权
    stats2 = sleep_consolidate(db)
    assert stats2["strengthened"] == 0
    assert abs(db.get(m.MemoryLink, link.id).strength - 0.55) < 1e-9
    assert len(_reports(db)) == 2


# ---------- 共用建边 ----------

def test_couse_creates_edge_after_two_codeliveries(db):
    a = _insight(db, "部署端口", "部署前先探测端口占用再启动")
    b = _insight(db, "日志保留", "日志按日轮转保留七天")
    _deliver(db, [a.id, b.id])
    _deliver(db, [a.id, b.id])
    db.commit()
    stats = sleep_consolidate(db)
    assert stats["linked"] == 1
    link = _active_links(db)[0]
    assert link.relation == "related" and link.origin == "rule"
    assert abs(link.strength - 0.5) < 1e-9


def test_couse_single_delivery_creates_no_edge(db):
    a = _insight(db, "部署端口", "部署前先探测端口占用再启动")
    b = _insight(db, "日志保留", "日志按日轮转保留七天")
    _deliver(db, [a.id, b.id])
    db.commit()
    stats = sleep_consolidate(db)
    assert stats["linked"] == 0
    assert _active_links(db) == []


# ---------- 死边剪除 ----------

def test_prune_dead_edge_at_floor_and_idle(db):
    a = _insight(db, "部署端口", "部署前先探测端口占用再启动")
    b = _insight(db, "日志保留", "日志按日轮转保留七天")
    link = create_link(db, APP, "alice", ("insight", a.id), ("insight", b.id),
                       "related", 0.10, "rule")
    link.created_at = NOW - timedelta(days=100)
    db.commit()
    stats = sleep_consolidate(db, now=NOW)
    assert stats["pruned"] == 1
    assert db.get(m.MemoryLink, link.id).valid_to is not None


def test_prune_skips_recent_or_applied_edges(db):
    a = _insight(db, "部署端口", "部署前先探测端口占用再启动")
    b = _insight(db, "日志保留", "日志按日轮转保留七天")
    c = _insight(db, "缓存策略", "缓存穿透时加空值占位")
    recent = create_link(db, APP, "alice", ("insight", a.id), ("insight", b.id),
                         "related", 0.10, "rule")
    recent.created_at = NOW - timedelta(days=10)
    applied = create_link(db, APP, "alice", ("insight", a.id), ("insight", c.id),
                          "related", 0.10, "rule")
    applied.created_at = NOW - timedelta(days=100)
    db.add(m.BriefDeliveryEdge(app_id=APP, user_id="alice", delivery_id="d1",
                               link_id=applied.id, insight_id=c.id, applied=True,
                               created_at=NOW - timedelta(days=100)))
    db.commit()
    stats = sleep_consolidate(db, now=NOW)
    assert stats["pruned"] == 0
    assert db.get(m.MemoryLink, recent.id).valid_to is None
    assert db.get(m.MemoryLink, applied.id).valid_to is None


# ---------- 事实合并 ----------

def test_fact_merge_norm_equal_migrates_links(db):
    keeper = _fact(db, "部署端口", "部署前先检查端口占用", support=2)
    loser = _fact(db, "部署端口", "  部署前先检查端口占用  ", support=1)
    other = _fact(db, "日志保留", "日志按日轮转保留七天")
    create_link(db, APP, "alice", ("fact", loser.id), ("fact", other.id),
                "related", 0.8, "manual")
    db.commit()
    stats = sleep_consolidate(db)
    assert stats["merged"] == 1
    assert db.get(m.MemoryFact, loser.id).valid_to is not None
    kept = db.get(m.MemoryFact, keeper.id)
    assert kept.support_count == 3
    assert abs(kept.confidence - 0.75) < 1e-9
    links = _active_links(db)
    assert len(links) == 1
    assert {links[0].src_id, links[0].dst_id} == {keeper.id, other.id}
    pair = _reports(db)[0].merged_pairs[0]
    assert pair["keeper_subject"] == "部署端口" and pair["loser_id"] == loser.id


def test_fact_merge_skips_different_subject(db):
    _fact(db, "部署端口", "部署前先检查端口占用")
    _fact(db, "缓存策略", "部署前先检查端口占用")
    db.commit()
    stats = sleep_consolidate(db)
    assert stats["merged"] == 0


def test_fact_merge_by_vector(db):
    _fact(db, "数据库迁移", "迁移前先全量备份数据库", vec=[1.0, 0.0])
    _fact(db, "数据库迁移", "变更表结构前先备份数据", vec=[0.99, 0.14])
    db.commit()
    stats = sleep_consolidate(db)
    assert stats["merged"] == 1


# ---------- 共用簇抽象 ----------

class _FakeLinker:
    """判定弃权（睡眠不做对子判定），抽象起草给固定原则。"""

    def judge(self, a, b):
        return None

    def generalize(self, members):
        return ("通用部署原则", "部署前先探测目标环境再动手", "部署或上线时")


def test_couse_cluster_generalizes_once(db):
    ids = []
    for proj, subj in (("p1", "甲项目部署"), ("p2", "乙项目部署"), ("p3", "丙项目部署")):
        ids.append(_insight(db, subj, "部署前先探测端口与依赖再启动", project=proj).id)
    _deliver(db, ids)
    _deliver(db, ids)
    db.commit()
    stats = sleep_consolidate(db, linker=_FakeLinker())
    assert stats["generalized"] == 1
    general = db.scalar(select(m.MemoryInsight).where(
        m.MemoryInsight.subject == "通用部署原则",
        m.MemoryInsight.valid_to.is_(None)))
    assert general is not None and general.project is None
    edges = [lk for lk in _active_links(db) if lk.relation == "generalizes"]
    assert len(edges) == 3 and all(e.src_id == general.id for e in edges)
    # 第二轮：已有原则覆盖簇 ≥2 名成员 → 不再重复起草
    stats2 = sleep_consolidate(db, linker=_FakeLinker())
    assert stats2["generalized"] == 0


def test_couse_cluster_without_linker_generalizes_nothing(db):
    ids = []
    for proj, subj in (("p1", "甲项目部署"), ("p2", "乙项目部署"), ("p3", "丙项目部署")):
        ids.append(_insight(db, subj, "部署前先探测端口与依赖再启动", project=proj).id)
    _deliver(db, ids)
    _deliver(db, ids)
    db.commit()
    stats = sleep_consolidate(db)
    assert stats["generalized"] == 0


# ---------- 报告接口 ----------

def test_consolidation_latest_endpoint(db, client):
    from tests.conftest import consent, uid

    consent(client, "alice")
    r = client.get("/v1/me/consolidation/latest", params=uid("alice"))
    assert r.status_code == 200
    assert r.json()["report"] is None
    a = _insight(db, "部署端口", "部署前先探测端口占用再启动")
    b = _insight(db, "日志保留", "日志按日轮转保留七天")
    _deliver(db, [a.id, b.id])
    _deliver(db, [a.id, b.id])
    db.commit()
    sleep_consolidate(db)
    r = client.get("/v1/me/consolidation/latest", params=uid("alice"))
    assert r.status_code == 200
    report = r.json()["report"]
    assert report["linked"] == 1 and report["run_at"] is not None
