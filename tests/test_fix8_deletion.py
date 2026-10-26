"""修复第 8 轮：删除清单漂移——清单单点收口与补漏。

覆盖审计 F-01（clear_all 漏交付边与巩固报告）、F-02（forget(insight)
不清理交付账、睡眠给已删节点建边、事实来源消息 TODO）与附录 B-F6
（合并附录里的败者正文随保留行复活）。Fake + 临时库，零联网。
"""

from collections import Counter

from sqlalchemy import select

from app import models as m
from app.config import settings
from app.deletion import CLEAR_ALL_KEEP, CLEAR_ALL_MODELS
from app.models import UserScoped
from app.sleep import _link_coused, sleep_consolidate
from tests.conftest import consent, uid

APP = settings.app_id


def _fact(db, subject, content, session=None):
    row = m.MemoryFact(app_id=APP, user_id="alice", subject=subject, content=content, source_session_id=session)
    db.add(row)
    db.flush()
    return row


def _insight(db, subject, content, vec=None):
    row = m.MemoryInsight(app_id=APP, user_id="alice", kind="strategy", subject=subject, content=content)
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


def _count(db, model):
    return db.query(model).filter_by(user_id="alice").count()


# ---------- 机制：注册表 ----------


def test_registry_user_scoped_models_covered():
    """新增一张用户域表而不更新清单时测试即失败——漂移从审计时发现
    变成合入时失败。"""
    subclasses = set(UserScoped.__subclasses__())
    assert subclasses == set(CLEAR_ALL_MODELS) | set(CLEAR_ALL_KEEP)


# ---------- clear_all 全量 ----------


def test_clear_all_drops_edges_reports_and_everything(client, db):
    consent(client, "alice")
    f = _fact(db, "主题A", "事实正文")
    _insight(db, "经验A", "经验正文")
    db.add_all(
        [
            m.MemoryPreference(app_id=APP, user_id="alice", key="k", value="v"),
            m.SessionSummary(app_id=APP, user_id="alice", session_id="s1", summary="sum"),
            m.SessionMessage(app_id=APP, user_id="alice", session_id="s1", msg_id="m1", role="user", content="hi"),
            m.TaskEpisode(app_id=APP, user_id="alice", title="任务", steps=[]),
            m.MemoryReminder(app_id=APP, user_id="alice", condition_text="到时", action_text="提醒"),
            m.MemoryJob(app_id=APP, user_id="alice", idempotency_key="jk", session_id="s1"),
            m.MemoryFactToken(fact_id=f.id, token="tok", app_id=APP, user_id="alice"),
        ]
    )
    ins = db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.user_id == "alice")).first()
    link = m.MemoryLink(
        app_id=APP,
        user_id="alice",
        src_kind="fact",
        src_id=f.id,
        dst_kind="insight",
        dst_id=ins.id,
        relation="related",
        strength=0.5,
        origin="manual",
    )
    db.add(link)
    db.flush()
    d = _deliver(db, [ins.id])
    db.add(m.BriefDeliveryEdge(app_id=APP, user_id="alice", delivery_id=d.id, link_id=link.id, insight_id=ins.id))
    db.add(
        m.ConsolidationReport(
            app_id=APP, user_id="alice", merged_pairs=[{"keeper_id": "k", "loser_id": "l", "keeper_subject": "留", "loser_subject": "删"}]
        )
    )
    # 别用户行不动
    db.add(m.MemoryFact(app_id=APP, user_id="bob", subject="b", content="2"))
    db.add(m.BriefDeliveryEdge(app_id=APP, user_id="bob", delivery_id="d", link_id="l", insight_id="i"))
    db.add(m.ConsolidationReport(app_id=APP, user_id="bob"))
    db.commit()

    assert client.delete("/v1/me/memories", params=uid("alice")).status_code == 204
    db.expire_all()
    for model in (
        m.MemoryFact,
        m.MemoryPreference,
        m.SessionSummary,
        m.SessionMessage,
        m.MemoryInsight,
        m.TaskEpisode,
        m.MemoryReminder,
        m.MemoryJob,
        m.BriefDelivery,
        m.BriefDeliveryEdge,
        m.ConsolidationReport,
        m.MemoryLink,
        m.MemoryFactToken,
        m.MemoryInsightToken,
    ):
        assert _count(db, model) == 0, model.__name__
    assert db.query(m.MemoryFact).filter_by(user_id="bob").count() == 1
    assert db.query(m.BriefDeliveryEdge).filter_by(user_id="bob").count() == 1
    assert db.query(m.ConsolidationReport).filter_by(user_id="bob").count() == 1
    # 刻意保留：同意状态、删除审计、墓碑
    assert db.query(m.UserSetting).filter_by(user_id="alice").count() == 1
    assert db.query(m.AuditLog).filter_by(user_id="alice").count() >= 1
    assert db.query(m.ForgottenId).count() >= 1


# ---------- forget(insight)：交付清账 ----------


def test_forget_insight_strips_deliveries_and_edges(client, db):
    a = _insight(db, "忘掉的", "死经验")
    b = _insight(db, "活着的", "活经验")
    c = _insight(db, "第三个", "另一经验")
    d1 = _deliver(db, [a.id, b.id])
    d2 = _deliver(db, [a.id])
    d3 = _deliver(db, [b.id, c.id])
    db.add_all(
        [
            m.BriefDeliveryEdge(app_id=APP, user_id="alice", delivery_id=d1.id, link_id="lk1", insight_id=a.id),
            m.BriefDeliveryEdge(app_id=APP, user_id="alice", delivery_id=d1.id, link_id="lk1", insight_id=b.id),
        ]
    )
    db.commit()
    a_id, b_id = a.id, b.id  # 删后 ORM 对象过期，先取纯 id

    assert client.delete(f"/v1/me/memories/{a_id}", params=uid("alice")).status_code == 204
    db.expire_all()
    assert db.get(m.BriefDelivery, d1.id).insight_ids == [b_id]
    assert db.get(m.BriefDelivery, d2.id).insight_ids == []  # 剥空行保留，不抹历史
    assert db.get(m.BriefDelivery, d3.id).insight_ids == [b_id, c.id]
    assert db.query(m.BriefDeliveryEdge).filter_by(insight_id=a_id).count() == 0
    assert db.query(m.BriefDeliveryEdge).filter_by(insight_id=b_id).count() == 1


# ---------- forget：报告 scrub 与附录剥离 ----------


def test_forget_insight_scrubs_reports_and_appendices(client, db):
    loser = _insight(db, "败者主题", "败者正文")
    other = _insight(db, "另一主题", "另一正文")
    seg_loser = f"\n\n[合并自同类经验：败者主题 <!-- merged-from:{loser.id} -->] 败者正文"
    seg_other = f"\n\n[合并自同类经验：另一主题 <!-- merged-from:{other.id} -->] 另一正文"
    keeper = _insight(db, "保留主题", "保留正文" + seg_loser + seg_other, vec=[0.1, 0.2])
    closed = _insight(db, "保留主题旧版", "旧版正文" + seg_loser)
    closed.valid_to = closed.valid_from
    db.add(
        m.ConsolidationReport(
            app_id=APP,
            user_id="alice",
            merged_pairs=[{"keeper_id": "k", "loser_id": "l", "keeper_subject": "留", "loser_subject": "删"}],
            generalized_ids=[{"id": loser.id, "subject": "败者主题"}, {"id": other.id, "subject": "另一主题"}],
        )
    )
    db.commit()

    assert client.delete(f"/v1/me/memories/{loser.id}", params=uid("alice")).status_code == 204
    db.expire_all()
    rep = db.query(m.ConsolidationReport).filter_by(user_id="alice").one()
    assert rep.generalized_ids == [{"id": other.id, "subject": "另一主题"}]
    assert rep.merged_pairs == [{"keeper_id": "k", "loser_id": "l", "keeper_subject": "留", "loser_subject": "删"}]
    k = db.get(m.MemoryInsight, keeper.id)
    assert k.content == "保留正文" + seg_other
    assert k.embedding is None and k.embed_model is None  # 内容变了，向量作废
    assert "败者正文" not in db.get(m.MemoryInsight, closed.id).content


def test_forget_fact_scrubs_report_pairs_and_orphan_sources(client, db):
    f1 = _fact(db, "主题一", "事实一", session="s1")
    _fact(db, "主题二", "事实二", session="s1")  # 同会话存活事实
    f3 = _fact(db, "主题三", "事实三", session="s2")
    db.add_all(
        [
            m.SessionMessage(app_id=APP, user_id="alice", session_id="s1", msg_id="m1", role="user", content="会话一原文"),
            m.SessionMessage(app_id=APP, user_id="alice", session_id="s2", msg_id="m2", role="user", content="会话二原文"),
            m.SessionSummary(app_id=APP, user_id="alice", session_id="s2", summary="会话二摘要"),
            m.ConsolidationReport(
                app_id=APP,
                user_id="alice",
                merged_pairs=[
                    {"keeper_id": f1.id, "loser_id": "x", "keeper_subject": "主题一", "loser_subject": "旧"},
                    {"keeper_id": "y", "loser_id": f3.id, "keeper_subject": "留", "loser_subject": "主题三"},
                    {"keeper_id": "y", "loser_id": "z", "keeper_subject": "留", "loser_subject": "无关"},
                ],
            ),
        ]
    )
    db.commit()
    f1_id, f3_id = f1.id, f3.id  # 删后 ORM 对象过期，先取纯 id

    assert client.delete(f"/v1/me/memories/{f1_id}", params=uid("alice")).status_code == 204
    db.expire_all()
    rep = db.query(m.ConsolidationReport).filter_by(user_id="alice").one()
    assert all(p["keeper_id"] != f1_id for p in rep.merged_pairs)
    assert len(rep.merged_pairs) == 2
    # s1 仍有存活事实引用，来源保留；s2 的事实尚未删，同样保留
    assert db.query(m.SessionMessage).filter_by(session_id="s1").count() == 1
    assert db.query(m.SessionMessage).filter_by(session_id="s2").count() == 1

    assert client.delete(f"/v1/me/memories/{f3_id}", params=uid("alice")).status_code == 204
    db.expire_all()
    rep = db.query(m.ConsolidationReport).filter_by(user_id="alice").one()
    assert all(p["loser_id"] != f3_id for p in rep.merged_pairs)
    assert len(rep.merged_pairs) == 1
    # s2 再无事实引用，消息与摘要一并清；s1 仍有存活事实，不动
    assert db.query(m.SessionMessage).filter_by(session_id="s2").count() == 0
    assert db.query(m.SessionSummary).filter_by(session_id="s2").count() == 0
    assert db.query(m.SessionMessage).filter_by(session_id="s1").count() == 1


# ---------- 睡眠不给死节点建边 ----------


def test_sleep_skips_dead_endpoints(client, db):
    a = _insight(db, "活一", "活经验一")
    b = _insight(db, "活二", "活经验二")
    dead = "deadbeefdeadbeefdeadbeefdeadbeef"
    _deliver(db, [dead, a.id])
    _deliver(db, [dead, a.id])
    _deliver(db, [a.id, b.id])
    _deliver(db, [a.id, b.id])
    db.commit()

    stats = sleep_consolidate(db)
    db.expire_all()
    links = db.query(m.MemoryLink).filter_by(user_id="alice").all()
    assert not any(lk.src_id == dead or lk.dst_id == dead for lk in links)
    assert any({lk.src_id, lk.dst_id} == {a.id, b.id} for lk in links)
    assert stats["linked"] >= 1

    # _link_coused 函数级关口：直接喂死端点对也被跳过
    before = db.query(m.MemoryLink).count()
    assert _link_coused(db, APP, "alice", Counter({(dead, a.id): 5})) == []
    assert db.query(m.MemoryLink).count() == before


def test_forget_then_sleep_no_resurrection(client, db):
    a = _insight(db, "将忘", "将忘经验")
    b = _insight(db, "长存", "长存经验")
    _deliver(db, [a.id, b.id])
    _deliver(db, [a.id, b.id])
    db.commit()

    assert client.delete(f"/v1/me/memories/{a.id}", params=uid("alice")).status_code == 204
    db.expire_all()
    # 交付已被剥 id：睡眠连计数的源头都没有，更建不出悬边
    assert db.query(m.BriefDelivery).filter_by(user_id="alice").all()[0].insight_ids == [b.id]
    sleep_consolidate(db)
    db.expire_all()
    links = db.query(m.MemoryLink).filter_by(user_id="alice").all()
    assert not any(lk.src_id == a.id or lk.dst_id == a.id for lk in links)
