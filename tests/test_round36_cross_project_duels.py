"""第 36 轮「跨项目矛盾对决」（审计 F-11）：裁决循环同桶收口 +
对决记录 + 跨项目简报降权 + 分化分界 + 删除/开关/跨用户边界。
"""

import pytest
from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.duels import duel_cross_project
from app.episodes import brief_for_task, consolidate_insights
from app.links import create_link
from app.schemas import CONSENT_VERSION
from tests.conftest import uid

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")
BOB = UserContext(app_id=APP, user_id="bob")

SUBJ = "部署前备份"


def _insight(db, subject, content, project, user=U, applies_when="", **kw):
    row = m.MemoryInsight(app_id=user.app_id, user_id=user.user_id, kind="strategy",
                          subject=subject, content=content, applies_when=applies_when,
                          project=project, **kw)
    db.add(row)
    db.commit()
    return row


def _pair(db, user=U, conf_a=0.9, conf_b=0.5):
    """正反对撞对：A 指令（必须）、B 禁止（不要），同主题同情境。"""
    a = _insight(db, SUBJ, "部署前必须备份数据库", "proj-a", user=user,
                 confidence=conf_a)
    b = _insight(db, SUBJ, "部署前不要备份数据库", "proj-b", user=user,
                 confidence=conf_b)
    return a, b


def _duels(db, user=U):
    return list(db.scalars(select(m.MemoryDuel).where(
        m.MemoryDuel.app_id == user.app_id, m.MemoryDuel.user_id == user.user_id,
        m.MemoryDuel.valid_to.is_(None))))


def _edge(db, a_id, b_id, relation="contradicts", user=U):
    return db.scalar(select(m.MemoryLink).where(
        m.MemoryLink.app_id == user.app_id, m.MemoryLink.user_id == user.user_id,
        m.MemoryLink.relation == relation, m.MemoryLink.valid_to.is_(None),
        m.MemoryLink.src_kind == "insight", m.MemoryLink.dst_kind == "insight",
        ((m.MemoryLink.src_id == a_id) & (m.MemoryLink.dst_id == b_id))
        | ((m.MemoryLink.src_id == b_id) & (m.MemoryLink.dst_id == a_id))))


def _fresh(db, row_id):
    db.expire_all()
    return db.get(m.MemoryInsight, row_id)


def test_cross_project_duel_recorded_with_winner(db):
    """跨项目对决：胜负记记录，胜方为高置信度行，边在。"""
    a, b = _pair(db)
    stats = duel_cross_project(db)
    assert stats["duels"] == 1
    recs = _duels(db)
    assert len(recs) == 1
    r = recs[0]
    assert r.verdict == "win" and r.winner_id == a.id and r.loser_id == b.id
    assert r.a_project != r.b_project and {r.a_project, r.b_project} == {"proj-a", "proj-b"}
    assert _edge(db, a.id, b.id) is not None


def test_reward_breaks_confidence_tie(db):
    """置信度打平时奖励分定胜负（A 成功 5 次胜过 B 零奖励）。"""
    a, b = _pair(db, conf_a=0.5, conf_b=0.5)
    db.execute(m.MemoryInsight.__table__.update().where(
        m.MemoryInsight.id == a.id).values(success_count=5))
    db.commit()
    duel_cross_project(db)
    r = _duels(db)[0]
    assert r.winner_id == a.id and r.loser_id == b.id


def test_exact_tie_records_tie_no_demotion(db):
    """全信号相等 → tie 记录，无败方，简报不降权。"""
    a, b = _pair(db, conf_a=0.5, conf_b=0.5)
    stats = duel_cross_project(db)
    assert stats["ties"] == 1
    r = _duels(db)[0]
    assert r.verdict == "tie" and r.winner_id is None and r.loser_id is None
    out = brief_for_task(db, U, "部署前备份要注意什么", 5)
    assert {i.insight_id for i in out} == {a.id, b.id}
    scores = {i.insight_id: i.score for i in out}
    assert scores[a.id] == scores[b.id]


def test_cross_project_loser_not_retired_by_consolidate(db):
    """收口核心：裁决循环不再越桶退役——差距 + 证据齐备时两行都活着，
    且对决记录已建（裁决循环内直接委托对决步）。"""
    a, b = _pair(db)
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", b.id),
                "contradicts", 0.6, "rule")
    db.execute(m.MemoryInsight.__table__.update().where(
        m.MemoryInsight.id == a.id).values(feedback_pos=2))
    db.commit()
    consolidate_insights(db)
    assert _fresh(db, a.id).valid_to is None and _fresh(db, b.id).valid_to is None
    recs = _duels(db)
    assert len(recs) == 1 and recs[0].loser_id == b.id


def test_same_bucket_adjudication_unchanged(db):
    """同桶裁决原样不动：同项目对 + 差距 + 证据 → 输家退役，无对决记录。"""
    a = _insight(db, SUBJ, "部署前必须备份数据库", "proj-a", confidence=0.9,
                 feedback_pos=2)
    b = _insight(db, SUBJ, "部署前不要备份数据库", "proj-a", confidence=0.5)
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", b.id),
                "contradicts", 0.6, "rule")
    db.commit()
    consolidate_insights(db)
    assert _fresh(db, b.id).valid_to is not None
    assert _fresh(db, a.id).valid_to is None
    assert _duels(db) == []


def test_generic_row_neither_retired_nor_dueled(db):
    """通用行不参战：通用行对项目行，裁决不退役、对决无记录。"""
    a = _insight(db, SUBJ, "部署前必须备份数据库", None, confidence=0.9,
                 feedback_pos=2)
    b = _insight(db, SUBJ, "部署前不要备份数据库", "proj-b", confidence=0.5)
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", b.id),
                "contradicts", 0.6, "rule")
    db.commit()
    consolidate_insights(db)
    assert _fresh(db, a.id).valid_to is None and _fresh(db, b.id).valid_to is None
    assert _duels(db) == []


def test_different_conditions_differentiate_not_duel(db):
    """分化分界：情境不同 + 输家有证据 → 关规则边、改连 related 边、
    两行都活着、无对决记录。"""
    a = _insight(db, SUBJ, "部署前必须备份数据库", "proj-a", confidence=0.9,
                 applies_when="生产部署")
    b = _insight(db, SUBJ, "部署前不要备份数据库", "proj-b", confidence=0.5,
                 applies_when="测试部署", feedback_pos=1)
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", b.id),
                "contradicts", 0.6, "rule")
    db.commit()
    stats = duel_cross_project(db)
    assert stats["differentiated"] == 1
    assert _edge(db, a.id, b.id, "contradicts") is None
    assert _edge(db, a.id, b.id, "related") is not None
    assert _fresh(db, a.id).valid_to is None and _fresh(db, b.id).valid_to is None
    assert _duels(db) == []


def test_judge_edge_with_different_conditions_still_duels(db):
    """判定器边高于字符串分界：判定器源边在场时情境不同也进对决，边不动。"""
    a = _insight(db, SUBJ, "部署前必须备份数据库", "proj-a", confidence=0.9,
                 applies_when="生产部署")
    b = _insight(db, SUBJ, "部署前不要备份数据库", "proj-b", confidence=0.5,
                 applies_when="测试部署", feedback_pos=1)
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", b.id),
                "contradicts", 0.8, "llm")
    db.commit()
    duel_cross_project(db)
    recs = _duels(db)
    assert len(recs) == 1 and recs[0].winner_id == a.id
    assert _edge(db, a.id, b.id, "contradicts") is not None


def test_brief_demotion_only_cross_project(db):
    """简报降权：胜方项目简报里败方乘 0.7（叠加跨项目 0.85）；败方
    本方项目简报里不降权；置顶败方不降权。"""
    a, b = _pair(db)
    duel_cross_project(db)
    out_a = brief_for_task(db, U, "部署前备份要注意什么", 5, project="proj-a")
    out_b = brief_for_task(db, U, "部署前备份要注意什么", 5, project="proj-b")
    sa = {i.insight_id: i.score for i in out_a}
    sb = {i.insight_id: i.score for i in out_b}
    assert sa[b.id] / sb[b.id] == pytest.approx(0.595, abs=0.005)  # (0.85 * 0.7) / 1.0
    # 置顶败方：降权失效（本方简报与跨项目简报同分，只差项目乘子）
    db.execute(m.MemoryInsight.__table__.update().where(
        m.MemoryInsight.id == b.id).values(pinned=True))
    db.commit()
    out_a2 = brief_for_task(db, U, "部署前备份要注意什么", 5, project="proj-a")
    sa2 = {i.insight_id: i.score for i in out_a2}
    assert sa2[b.id] / sb[b.id] == pytest.approx(0.85, abs=0.005)


def test_pinned_loser_skips_duel(db):
    """输家被置顶 → 不裁决、不建记录（用户策展高于自动裁决）。"""
    a, b = _pair(db)
    db.execute(m.MemoryInsight.__table__.update().where(
        m.MemoryInsight.id == b.id).values(pinned=True))
    db.commit()
    stats = duel_cross_project(db)
    assert stats["skipped"] == 1 and _duels(db) == []


def test_forget_purges_duel_records_and_edges(db, client):
    """删除穿透：forget 一行后，涉及该行的对决记录与边清零。"""
    a, b = _pair(db)
    duel_cross_project(db)
    assert _duels(db) and _edge(db, a.id, b.id) is not None
    r = client.delete(f"/v1/me/memories/{a.id}", params=uid("alice"))
    assert r.status_code == 204
    assert _duels(db) == []
    assert _edge(db, a.id, b.id) is None


def test_switched_off_user_has_no_duels(db):
    """开关关闭的用户整轮跳过：无记录、无边。"""
    a, b = _pair(db)
    db.add(m.UserSetting(app_id=APP, user_id="alice", memory_enabled=False,
                         consent_version=CONSENT_VERSION))
    db.commit()
    stats = duel_cross_project(db)
    assert stats["duels"] == 0 and _duels(db) == []
    assert _edge(db, a.id, b.id) is None


def test_cross_user_isolation(db):
    """跨用户不越界：两用户的对各裁各的，记录不混。"""
    _pair(db, user=U)
    _pair(db, user=BOB)
    duel_cross_project(db)
    ra = _duels(db, U)
    rb = _duels(db, BOB)
    assert len(ra) == 1 and len(rb) == 1
    assert ra[0].loser_id != rb[0].loser_id


def test_duels_endpoint_lists_current_only(db, client):
    """对决记录可查：REST 列现行记录；跨用户查不到对方的。"""
    a, b = _pair(db)
    duel_cross_project(db)
    body = client.get("/v1/me/duels", params=uid("alice")).json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["winner_id"] == a.id and item["loser_id"] == b.id
    assert item["a_subject"] == SUBJ and item["verdict"] == "win"
    assert client.get("/v1/me/duels", params=uid("bob")).json()["total"] == 0


def test_candidate_cost_bounded(db):
    """候选成本有界：2000 互不共享锚点的填充行 + 1 对决对，对决步只产
    1 条记录（填充行之间不成对，锚点桶互不相交）。"""
    rows = [m.MemoryInsight(app_id=APP, user_id="alice", kind="strategy",
                            subject=chr(0x4E00 + i), content=chr(0x4E00 + i),
                            project="proj-a") for i in range(2000)]
    db.add_all(rows)
    db.commit()
    _pair(db)
    duel_cross_project(db)
    assert len(_duels(db)) == 1
