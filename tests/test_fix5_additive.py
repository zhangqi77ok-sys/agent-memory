"""修复第 5 轮：分化即综合的自毁（审计 Top 5，A-F01/A-F01b/A-F10）。

旧行为：综合/weave 簇抽象/睡眠簇抽象起草原则时走 _apply_insight 的版本
滚动语义——起草主题与胜家/输家（或簇成员）same_subject 但内容不同时，
UPDATE 分支关掉双方，刚建的 related 边经两次 migrate_links 成自环被关，
随后挂的 generalizes 边指向死节点，巡检报告却仍显示 synthesized=1。

新行为：三处起草点走 additive 加法模式——原则是新增节点，不关行、不迁
边、不继承计数；NOOP 时不挂边、不计数（与综合分支原有的正确写法统一）。
"""

from datetime import datetime, timezone

from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.episodes import _apply_insight, _Cand, consolidate_insights
from app.links import create_link, weave_links
from app.sleep import sleep_consolidate

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def _insight(db, subject, content, applies="", confidence=0.5, feedback_pos=0, project=None, vec=None):
    row = m.MemoryInsight(
        app_id=APP,
        user_id="alice",
        kind="strategy",
        subject=subject,
        content=content,
        applies_when=applies,
        confidence=confidence,
        feedback_pos=feedback_pos,
        project=project,
        embedding=list(vec) if vec else None,
        embed_model="fake-embedder" if vec else None,
    )
    db.add(row)
    db.flush()
    return row


def _live_principles(db):
    return db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.kind == "principle", m.MemoryInsight.valid_to.is_(None))).all()


def _live_generalizes(db):
    return db.scalars(select(m.MemoryLink).where(m.MemoryLink.relation == "generalizes", m.MemoryLink.valid_to.is_(None))).all()


def _split_pair(db):
    """同主题的胜家/输家对——第 24 轮测试没撞上 bug，正是因为它用了不同
    主题；真实判定器起草的原则主题常与原主题相同，这才是高频路径。"""
    win = _insight(db, "端口策略", "优先使用 8080", applies="生产部署时", confidence=0.9, feedback_pos=3)
    lose = _insight(db, "端口策略", "绝不要用 8080", applies="仅测试环境", confidence=0.4, feedback_pos=1)
    link = create_link(db, APP, "alice", ("insight", win.id), ("insight", lose.id), "contradicts", 0.6, "rule")
    db.commit()
    return win, lose, link


class _SynthLinker:
    def __init__(self, drafted):
        self.drafted = drafted

    def synthesize_condition(self, winner, loser, winner_evidence=0, loser_evidence=0):
        return self.drafted


class _GeneralizeLinker:
    def __init__(self, drafted):
        self.drafted = drafted

    def judge(self, a, b):
        return None

    def generalize(self, members):
        return self.drafted


def _related_edge(db, win_id, lose_id):
    rows = db.scalars(select(m.MemoryLink).where(m.MemoryLink.relation == "related", m.MemoryLink.valid_to.is_(None))).all()
    return [r for r in rows if {r.src_id, r.dst_id} == {win_id, lose_id}]


def test_synthesis_same_subject_keeps_both_alive(db):
    win, lose, link = _split_pair(db)
    fake = _SynthLinker(drafted=("端口策略", "生产部署时用 8080，测试环境避开 8080", "部署服务时"))
    stats = consolidate_insights(db, now=NOW, linker=fake)
    assert stats["differentiated"] == 1 and stats["synthesized"] == 1
    db.expire_all()
    # 双方存活：分化判定“输家不是错的”，综合不得推翻分化自己的结论
    assert db.get(m.MemoryInsight, win.id).valid_to is None
    assert db.get(m.MemoryInsight, lose.id).valid_to is None
    # 双方计数不动（加法模式不继承计数，旧行还活着就不能双重记账）
    assert db.get(m.MemoryInsight, win.id).feedback_pos == 3
    assert db.get(m.MemoryInsight, lose.id).feedback_pos == 1
    assert db.get(m.MemoryLink, link.id).valid_to is not None  # 矛盾边已关
    assert len(_related_edge(db, win.id, lose.id)) == 1  # related 边不再被迁成自环
    principles = _live_principles(db)
    assert len(principles) == 1
    principle = principles[0]
    assert principle.subject == "端口策略" and principle.project is None
    # 新原则从默认值起步，不是双方的新版本
    assert principle.version == 1
    assert principle.confidence == 0.5 and principle.feedback_pos == 0
    edges = _live_generalizes(db)
    assert len(edges) == 2 and all(e.src_id == principle.id for e in edges)
    assert {e.dst_id for e in edges} == {win.id, lose.id}
    assert all(e.strength == 0.8 and e.origin == "llm" for e in edges)
    # 如实性硬证据：再跑一轮 weave，死边清理不动这两条边（旧行为下它们
    # 指向死节点，会在这里被 _close_dead_links 清掉）
    weave_links(db)
    db.expire_all()
    assert all(db.get(m.MemoryLink, e.id).valid_to is None for e in edges)


def test_synthesis_noop_hangs_no_edges(db):
    win, lose, _ = _split_pair(db)
    # 起草主题+内容与胜家行完全一致 → NOOP：不建原则、不挂边、不计数
    fake = _SynthLinker(drafted=("端口策略", "优先使用 8080", "生产部署时"))
    stats = consolidate_insights(db, now=NOW, linker=fake)
    assert stats["differentiated"] == 1 and stats["synthesized"] == 0
    assert _live_principles(db) == []
    assert _live_generalizes(db) == []
    db.expire_all()
    assert db.get(m.MemoryInsight, win.id).valid_to is None
    assert db.get(m.MemoryInsight, lose.id).valid_to is None


def _cluster_members(db):
    ids = []
    for proj in ("p1", "p2", "p3"):
        ids.append(_insight(db, "部署端口", "部署前先探测端口与依赖再启动", vec=[1.0, 0.0], project=proj).id)
    db.commit()
    return ids


def test_weave_abstraction_same_subject_keeps_members_alive(db):
    ids = _cluster_members(db)
    fake = _GeneralizeLinker(drafted=("部署端口", "部署前先探测目标环境再动手", "部署或上线时"))
    stats = weave_links(db, linker=fake)
    assert stats["generalized"] == 1
    db.expire_all()
    assert all(db.get(m.MemoryInsight, i).valid_to is None for i in ids)
    principles = _live_principles(db)
    assert len(principles) == 1
    edges = _live_generalizes(db)
    assert len(edges) == 3 and all(e.src_id == principles[0].id for e in edges)
    assert {e.dst_id for e in edges} == set(ids)


def test_weave_abstraction_noop_hangs_no_edges(db):
    ids = _cluster_members(db)
    # 起草内容与成员完全一致 → NOOP：此前 weave 只查 gid，仍会挂边计数
    fake = _GeneralizeLinker(drafted=("部署端口", "部署前先探测端口与依赖再启动", "部署或上线时"))
    stats = weave_links(db, linker=fake)
    assert stats["generalized"] == 0
    assert _live_generalizes(db) == []
    db.expire_all()
    assert all(db.get(m.MemoryInsight, i).valid_to is None for i in ids)


def _deliver(db, ids):
    row = m.BriefDelivery(app_id=APP, user_id="alice", insight_ids=list(ids))
    db.add(row)
    db.flush()
    return row


def _sleep_cluster(db):
    ids = []
    for proj in ("p1", "p2", "p3"):
        ids.append(_insight(db, "部署端口", "部署前先探测端口与依赖再启动", project=proj).id)
    _deliver(db, ids)
    _deliver(db, ids)
    db.commit()
    return ids


def test_sleep_abstraction_same_subject_keeps_members_alive(db):
    ids = _sleep_cluster(db)
    fake = _GeneralizeLinker(drafted=("部署端口", "部署前先探测目标环境再动手", "部署或上线时"))
    stats = sleep_consolidate(db, linker=fake)
    assert stats["generalized"] == 1
    db.expire_all()
    assert all(db.get(m.MemoryInsight, i).valid_to is None for i in ids)
    principles = _live_principles(db)
    assert len(principles) == 1
    edges = _live_generalizes(db)
    assert len(edges) == 3 and all(e.src_id == principles[0].id for e in edges)
    assert {e.dst_id for e in edges} == set(ids)


def test_sleep_abstraction_noop_hangs_no_edges(db):
    ids = _sleep_cluster(db)
    # 此前 sleep 只以 NOOP 决定计数、不决定挂边，仍会把非原则行当原则挂上
    fake = _GeneralizeLinker(drafted=("部署端口", "部署前先探测端口与依赖再启动", "部署或上线时"))
    stats = sleep_consolidate(db, linker=fake)
    assert stats["generalized"] == 0
    assert _live_generalizes(db) == []
    db.expire_all()
    assert all(db.get(m.MemoryInsight, i).valid_to is None for i in ids)


def test_historical_dead_generalizes_edge_pruned_by_weave(db):
    # 历史残边不需要修复脚本：下一轮 weave 的死边清理本就清掉它
    principle = _insight(db, "通用原则", "先探测再动手")
    dead = _insight(db, "旧经验", "旧做法")
    edge = create_link(db, APP, "alice", ("insight", principle.id), ("insight", dead.id), "generalizes", 0.8, "llm")
    db.commit()
    dead.valid_to = m._now()
    db.commit()
    stats = weave_links(db)
    assert stats["pruned"] >= 1
    db.expire_all()
    assert db.get(m.MemoryLink, edge.id).valid_to is not None
    assert db.get(m.MemoryInsight, principle.id).valid_to is None


def test_non_additive_update_still_closes(db):
    # 回归护栏：默认路径仍是版本滚动语义，提炼写入的基本盘不受影响
    old = _insight(db, "部署端口", "部署前先探测端口占用再启动")
    db.commit()
    decision, nid = _apply_insight(db, U, _Cand("strategy", "部署端口", "部署时不要做端口检查", ""), None, None)
    db.commit()
    assert decision == "update"
    db.expire_all()
    assert db.get(m.MemoryInsight, old.id).valid_to is not None
    new = db.get(m.MemoryInsight, nid)
    assert new is not None and new.valid_to is None and new.version == 2
