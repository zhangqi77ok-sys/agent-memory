"""第十五轮「织网」：记忆之间的连接层。

用户定调：信息是点，智慧是线与网。此前经验在库里一行行互不认识，召回只能
按与查询的相似度捞散行。本轮考：写入即连边、巡检织密与抽象、版本滚动迁边、
召回沿边扩散（类比召回）、矛盾在简报里被标记、跨用户不串网、删记忆连边删。
"""
from sqlalchemy import select

from app import models as m
from app.api.memories import forget
from app.auth import UserContext
from app.config import settings
from app.episodes import _apply_insight, _Cand, brief_for_task
from app.links import (
    NodeView,
    classify_pair,
    create_link,
    links_touching,
    neighbors_of,
    weave_links,
    weave_on_write,
)
from app.recall import recall_facts
from tests.conftest import consent, uid

U = UserContext(app_id=settings.app_id, user_id="alice")
BOB = UserContext(app_id=settings.app_id, user_id="bob")


def _insight(db, subject, content, applies="", vec=None, project=None, user=U):
    row = m.MemoryInsight(app_id=user.app_id, user_id=user.user_id, kind="strategy",
                          subject=subject, content=content, applies_when=applies,
                          project=project,
                          embedding=list(vec) if vec else None,
                          embed_model="fake-embedder" if vec else None)
    db.add(row)
    db.commit()
    return row


def _fact(db, subject, content, user=U):
    row = m.MemoryFact(app_id=user.app_id, user_id=user.user_id,
                       subject=subject, content=content)
    db.add(row)
    db.commit()
    return row


def _active_links(db, user=U):
    return list(db.scalars(select(m.MemoryLink).where(
        m.MemoryLink.user_id == user.user_id, m.MemoryLink.valid_to.is_(None))))


# ---------- 写入时织网 ----------

def test_weave_on_write_same_subject_related(db):
    a = _insight(db, "部署端口", "部署前先探测端口占用再启动服务")
    b = _insight(db, "部署端口", "本地调试时优先选 3000 段端口避开常用占用")
    made = weave_on_write(db, U.app_id, U.user_id, b)
    db.commit()
    assert made == 1
    links = _active_links(db)
    assert len(links) == 1
    assert links[0].relation == "related" and links[0].origin == "rule"
    assert {links[0].src_id, links[0].dst_id} == {a.id, b.id}


def test_weave_on_write_supports_with_vector(db):
    _insight(db, "重试策略", "接口超时后指数退避重试", vec=[1.0, 0.0])
    b = _insight(db, "重试策略", "调用失败时按指数退避重试并加抖动", vec=[1.0, 0.0])
    assert weave_on_write(db, U.app_id, U.user_id, b) == 1
    db.commit()
    link = _active_links(db)[0]
    assert link.relation == "supports" and link.strength > 0.99


def test_weave_dedup_no_duplicate_links(db):
    _insight(db, "部署端口", "部署前先探测端口占用再启动服务")
    b = _insight(db, "部署端口", "本地调试时优先选 3000 段端口避开常用占用")
    weave_on_write(db, U.app_id, U.user_id, b)
    weave_on_write(db, U.app_id, U.user_id, b)
    db.commit()
    assert len(_active_links(db)) == 1


def test_classify_judge_overrides_rules(db):
    a = NodeView("insight", "1", "部署端口", "部署前先探测端口占用")
    b = NodeView("insight", "2", "部署端口", "本地调试优先选 3000 段端口")
    # 无判定器：同主题不同内容 → related（规则）
    assert classify_pair(a, b, None)[0] == "related"
    # 判定器说矛盾 → 听判定器的（语义判断优先于规则猜测），origin 标 llm
    rel, strength, origin = classify_pair(a, b, None, judge=lambda x, y: ("contradicts", 0.9))
    assert (rel, origin) == ("contradicts", "llm") and strength == 0.9
    # 判定器说无关系 → 规则也不许硬连
    assert classify_pair(a, b, None, judge=lambda x, y: ("none", 0.0)) is None


# ---------- 巡检织网 ----------

def test_patrol_links_contradiction_by_markers(db):
    _insight(db, "部署端口", "部署前必须先检查端口占用再启动")
    _insight(db, "部署端口", "部署时不要检查端口，直接启动即可")
    stats = weave_links(db)
    assert stats["contradicts"] >= 1
    link = _active_links(db)[0]
    assert link.relation == "contradicts" and link.origin == "rule"


def test_patrol_cross_subject_related_by_vector(db):
    _insight(db, "数据库迁移", "迁移前先全量备份数据库", vec=[1.0, 0.0])
    _insight(db, "模式变更", "变更表结构前先备份数据", vec=[0.99, 0.14])
    stats = weave_links(db)
    assert stats["linked"] >= 1
    assert _active_links(db)[0].relation in ("related", "supports", "generalizes")


def test_patrol_prunes_dead_links(db):
    a = _insight(db, "部署端口", "部署前先探测端口占用")
    b = _insight(db, "日志轮转", "日志按日轮转保留七天")
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", b.id),
                "related", 0.9, "manual")
    db.commit()
    b.valid_to = m._now()
    db.commit()
    stats = weave_links(db)
    assert stats["pruned"] >= 1
    assert _active_links(db) == []


class _FakeLinker:
    """巡检抽象测试用：判定一律弃权（落规则），抽象起草给固定原则。"""

    def judge(self, a, b):
        return None

    def generalize(self, members):
        return ("通用部署原则", "部署前先探测目标环境再动手", "部署或上线时")


def test_patrol_generalizes_cross_project_cluster(db):
    for proj, subj in (("p1", "甲项目部署"), ("p2", "乙项目部署"), ("p3", "丙项目部署")):
        _insight(db, subj, "部署前先探测端口与依赖再启动", vec=[1.0, 0.0], project=proj)
    stats = weave_links(db, linker=_FakeLinker())
    assert stats["generalized"] == 1
    general = db.scalar(select(m.MemoryInsight).where(
        m.MemoryInsight.subject == "通用部署原则", m.MemoryInsight.valid_to.is_(None)))
    assert general is not None and general.project is None
    edges = [lk for lk in _active_links(db) if lk.relation == "generalizes"]
    assert len(edges) == 3 and all(e.src_id == general.id for e in edges)


# ---------- 版本迁移 ----------

def test_links_migrate_on_version_update(db):
    a = _insight(db, "部署端口", "部署前先探测端口占用再启动")
    b = _insight(db, "日志轮转", "日志按日轮转保留七天")
    c = _insight(db, "部署端口检查", "部署时不要做端口检查")
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", b.id),
                "supports", 0.8, "manual")
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", c.id),
                "contradicts", 0.7, "manual")
    db.commit()
    decision, new_id = _apply_insight(
        db, U, _Cand("strategy", "部署端口", "部署前先探测端口占用，优先用 3000 段", ""),
        None, None)
    db.commit()
    assert decision == "update" and new_id != a.id
    # supports 边迁到新版本；contradicts 边关掉（针对旧文本，不自动继承）
    sup = [lk for lk in _active_links(db) if lk.relation == "supports"]
    assert len(sup) == 1 and new_id in (sup[0].src_id, sup[0].dst_id)
    assert [lk for lk in _active_links(db) if lk.relation == "contradicts"] == []
    closed = db.scalars(select(m.MemoryLink).where(m.MemoryLink.relation == "contradicts")).all()
    assert closed and all(lk.valid_to is not None for lk in closed)


# ---------- 召回扩散：类比召回与矛盾标记 ----------

def test_brief_spreading_activation(db):
    a = _insight(db, "部署端口选择", "部署前先探测端口占用再启动服务", applies="部署服务时")
    b = _insight(db, "日志保留策略", "日志按日轮转保留七天避免磁盘写满")
    # 无边时：B 与查询零重叠，进不了简报
    before = brief_for_task(db, U, "部署服务要绑定端口", 5)
    assert [o.insight_id for o in before] == [a.id]
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", b.id),
                "related", 0.9, "manual")
    db.commit()
    after = brief_for_task(db, U, "部署服务要绑定端口", 5)
    by_id = {o.insight_id: o for o in after}
    assert b.id in by_id
    assert by_id[b.id].reason == "linked" and by_id[b.id].linked_from == a.id
    assert by_id[a.id].reason != "linked"


def test_brief_marks_contradictions(db):
    a = _insight(db, "部署端口", "部署前必须先探测端口占用再启动")
    b = _insight(db, "部署端口", "部署时不要探测端口直接启动服务")
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", b.id),
                "contradicts", 0.7, "manual")
    db.commit()
    out = brief_for_task(db, U, "部署端口怎么处理", 5)
    by_id = {o.insight_id: o for o in out}
    assert a.id in by_id and b.id in by_id
    assert by_id[a.id].conflicts_with == [b.id]
    assert by_id[b.id].conflicts_with == [a.id]


def test_fact_recall_spreading(db):
    f1 = _fact(db, "咖啡偏好", "用户喜欢手冲咖啡")
    f2 = _fact(db, "作息习惯", "用户习惯十一点前睡觉")
    create_link(db, U.app_id, U.user_id, ("fact", f1.id), ("fact", f2.id),
                "related", 0.9, "manual")
    db.commit()
    out = recall_facts(db, U, "手冲咖啡", k=5, token_budget=1500)
    by_id = {o.fact_id: o for o in out}
    assert f1.id in by_id and f2.id in by_id
    assert by_id[f2.id].reason == "linked" and by_id[f2.id].linked_from == f1.id


# ---------- 隔离与删除 ----------

def test_links_do_not_cross_users(db):
    a = _insight(db, "部署端口", "部署前先探测端口占用")
    bob_row = _insight(db, "部署端口", "部署前先探测端口占用", user=BOB)
    # alice 织网只看自己的节点：不会连到 bob 的经验
    weave_on_write(db, BOB.app_id, BOB.user_id, bob_row)
    db.commit()
    assert _active_links(db, user=BOB) == []
    assert neighbors_of(db, U.app_id, U.user_id, "insight", a.id) == []


def test_rest_links_endpoint_and_isolation(client, db):
    consent(client, "alice", "bob")
    a = _insight(db, "部署端口", "部署前先探测端口占用")
    b = _insight(db, "日志轮转", "日志按日轮转保留七天")
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", b.id),
                "related", 0.9, "manual")
    db.commit()
    body = client.get(f"/v1/me/insights/{a.id}/links", params=uid("alice")).json()
    assert len(body["links"]) == 1
    assert body["links"][0]["relation"] == "related"
    assert body["links"][0]["node"]["subject"] == "日志轮转"
    # 别人的经验查邻里 → 404，不泄露存在性
    r = client.get(f"/v1/me/insights/{a.id}/links", params=uid("bob"))
    assert r.status_code == 404


def test_forget_removes_links(db):
    a = _insight(db, "部署端口", "部署前先探测端口占用")
    b = _insight(db, "日志轮转", "日志按日轮转保留七天")
    create_link(db, U.app_id, U.user_id, ("insight", a.id), ("insight", b.id),
                "related", 0.9, "manual")
    db.commit()
    assert links_touching(db, U.app_id, U.user_id, "insight", b.id)
    forget(a.id, U, db)
    assert list(db.scalars(select(m.MemoryLink))) == []
