"""第二十四轮：分化即综合——矛盾分化时把条件分流落成一条原则。

接线点：
- links.LlmLinker.synthesize_condition（胜家/输家视图 + 证据计数 → 原则三元组，
  空 subject/空 content/调用失败 → None）；
- episodes.consolidate_insights(linker=...)：分化分支内起草成功才建原则行与两条
  generalizes 边（0.8，"llm"，与第二十一轮簇抽象同口径），审计 decision="synthesize"
  并带原则 id；无 linker/起草失败/返回空/抛异常时分化照常，绝不回滚。
- worker 小时节拍传入与 weave/sleep 同一个 linker，日志门补 stats["synthesized"]。
"""

from datetime import datetime, timezone

from sqlalchemy import select

from app import models as m
from app.config import settings
from app.episodes import consolidate_insights
from app.links import create_link

APP = settings.app_id
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def _insight(db, subject, content, applies="", confidence=0.5, feedback_pos=0):
    row = m.MemoryInsight(app_id=APP, user_id="alice", kind="strategy", subject=subject,
                          content=content, applies_when=applies,
                          confidence=confidence, feedback_pos=feedback_pos)
    db.add(row)
    db.flush()
    return row


def _split_pair(db):
    """胜家（生产部署用 8080，证据 3 条）与输家（仅测试环境避开 8080，证据 1 条）
    被 contradicts 边连着——满足第二十轮条件分化的全部门槛。"""
    win = _insight(db, "端口策略甲", "优先使用 8080", applies="生产部署时",
                   confidence=0.9, feedback_pos=3)
    lose = _insight(db, "端口策略乙", "绝不要用 8080", applies="仅测试环境",
                    confidence=0.4, feedback_pos=1)
    link = create_link(db, APP, "alice", ("insight", win.id), ("insight", lose.id),
                       "contradicts", 0.6, "rule")
    db.commit()
    return win, lose, link


class _FakeLinker:
    """起草成功返回三元组；drafted=None 表综合不成；exc 非空表判定器抛异常。"""

    def __init__(self, drafted=None, exc=None):
        self.drafted = drafted
        self.exc = exc
        self.calls = []

    def synthesize_condition(self, winner, loser, winner_evidence=0, loser_evidence=0):
        self.calls.append((winner.subject, loser.subject, winner_evidence, loser_evidence))
        if self.exc is not None:
            raise self.exc
        return self.drafted


def _related_edge(db, win_id, lose_id):
    rows = db.scalars(select(m.MemoryLink).where(
        m.MemoryLink.relation == "related", m.MemoryLink.valid_to.is_(None))).all()
    return [r for r in rows if {r.src_id, r.dst_id} == {win_id, lose_id}]


def test_synthesis_on_differentiation(db):
    win, lose, link = _split_pair(db)
    fake = _FakeLinker(drafted=("端口分流原则", "生产部署时用 8080，测试环境避开 8080",
                                "部署服务时"))
    stats = consolidate_insights(db, now=NOW, linker=fake)
    assert stats["differentiated"] == 1 and stats["synthesized"] == 1
    assert fake.calls == [("端口策略甲", "端口策略乙", 3, 1)]
    db.expire_all()
    assert db.get(m.MemoryInsight, lose.id).valid_to is None
    assert db.get(m.MemoryLink, link.id).valid_to is not None
    assert len(_related_edge(db, win.id, lose.id)) == 1  # 原 related 边仍在
    principles = db.scalars(select(m.MemoryInsight).where(
        m.MemoryInsight.kind == "principle", m.MemoryInsight.valid_to.is_(None))).all()
    assert len(principles) == 1
    principle = principles[0]
    assert principle.subject == "端口分流原则" and principle.project is None
    edges = db.scalars(select(m.MemoryLink).where(
        m.MemoryLink.relation == "generalizes", m.MemoryLink.src_id == principle.id,
        m.MemoryLink.valid_to.is_(None))).all()
    assert {e.dst_id for e in edges} == {win.id, lose.id}
    assert all(e.strength == 0.8 and e.origin == "llm" for e in edges)
    audits = db.scalars(select(m.AuditLog).where(
        m.AuditLog.decision == "synthesize")).all()
    assert len(audits) == 1 and principle.id in audits[0].fact_ids


def test_synthesis_none_keeps_differentiation(db):
    win, lose, _ = _split_pair(db)
    stats = consolidate_insights(db, now=NOW, linker=_FakeLinker(drafted=None))
    assert stats["differentiated"] == 1 and stats["synthesized"] == 0
    db.expire_all()
    assert db.get(m.MemoryInsight, lose.id).valid_to is None
    assert len(_related_edge(db, win.id, lose.id)) == 1
    principles = db.scalars(select(m.MemoryInsight).where(
        m.MemoryInsight.kind == "principle", m.MemoryInsight.valid_to.is_(None))).all()
    assert principles == []


def test_no_linker_regression(db):
    win, lose, _ = _split_pair(db)
    stats = consolidate_insights(db, now=NOW)
    assert stats["differentiated"] == 1 and stats["synthesized"] == 0
    db.expire_all()
    assert db.get(m.MemoryInsight, lose.id).valid_to is None
    assert len(_related_edge(db, win.id, lose.id)) == 1


def test_synthesis_exception_does_not_rollback(db):
    win, lose, link = _split_pair(db)
    stats = consolidate_insights(db, now=NOW,
                                 linker=_FakeLinker(exc=RuntimeError("llm down")))
    assert stats["differentiated"] == 1 and stats["synthesized"] == 0
    db.expire_all()
    assert db.get(m.MemoryInsight, lose.id).valid_to is None  # 分化不回滚
    assert db.get(m.MemoryLink, link.id).valid_to is not None  # 矛盾边已关
    assert len(_related_edge(db, win.id, lose.id)) == 1
