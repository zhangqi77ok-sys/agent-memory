"""第 35 轮「奖励加权巩固」：outcome 维度的巩固优先级。

奖励分是派生分（app/reward.py），不存表、不写置信度：睡眠的两条
带容量上限的队列（共用建边 50、共用簇抽象 5）按奖励优先排，衰减
复述计数补上旧公式漏掉的任务成功；负奖励降权不删除，超容顺延。
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import models as m
from app import sleep as sleep_mod
from app.auth import UserContext
from app.config import settings
from app.episodes import consolidate_insights
from app.reward import REWARD_CAP, reward_from_counts, reward_score
from app.schemas import CONSENT_VERSION
from app.sleep import sleep_consolidate

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")
NOW = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)


def _insight(db, subject, content, user="alice", project=None, **kw):
    row = m.MemoryInsight(app_id=APP, user_id=user, kind="strategy",
                          subject=subject, content=content, project=project, **kw)
    db.add(row)
    db.flush()
    return row


def _deliver(db, ids, user="alice"):
    row = m.BriefDelivery(app_id=APP, user_id=user, insight_ids=list(ids))
    db.add(row)
    db.flush()
    return row


def _conf(db, row_id):
    db.expire_all()
    return db.get(m.MemoryInsight, row_id).confidence


def _links(db, user="alice"):
    return list(db.scalars(select(m.MemoryLink).where(
        m.MemoryLink.user_id == user, m.MemoryLink.valid_to.is_(None))))


def _set_switch(db, user, on):
    s = db.scalar(select(m.UserSetting).where(m.UserSetting.user_id == user))
    if s is None:
        s = m.UserSetting(app_id=APP, user_id=user, memory_enabled=on,
                          consent_version=CONSENT_VERSION)
        db.add(s)
    else:
        s.memory_enabled = on
    db.commit()


# ---------- 分值单元 ----------

def test_reward_weights_and_cap():
    assert reward_from_counts(pos=1) == 1.0
    assert reward_from_counts(succ=1) == 1.0
    assert reward_from_counts(auto_pos=3) == 1.5  # 自动信号半权
    assert reward_from_counts(auto_neg=2) == -1.0
    assert reward_from_counts(pos=2, neg=1, succ=1, auto_pos=2, auto_neg=2) == 2.0
    assert reward_from_counts(pos=50) == REWARD_CAP  # 上限钳制
    assert reward_from_counts(neg=50) == -REWARD_CAP  # 负向对称
    assert reward_from_counts() == 0.0  # None 安全（全默认 0）
    assert reward_from_counts(pos=None, neg=None, succ=None,
                              auto_pos=None, auto_neg=None) == 0.0


def test_reward_score_reads_row():
    row = SimpleNamespace(feedback_pos=2, feedback_neg=0, success_count=1,
                          auto_pos=0, auto_neg=0)
    assert reward_score(row) == 3.0


# ---------- 建边队列：奖励优先 ----------

def test_link_queue_reward_beats_exposure(db, monkeypatch):
    """奖励对（同现 2 次）胜过高曝光对（同现 5 次），cap 压到 1 时只建奖励对。"""
    a = _insight(db, "奖励经验", "已被好评的经验", feedback_pos=2)
    b = _insight(db, "搭档经验", "与奖励经验同现")
    c = _insight(db, "高曝光甲", "常一起出现甲")
    d = _insight(db, "高曝光乙", "常一起出现乙")
    _deliver(db, [a.id, b.id])
    _deliver(db, [a.id, b.id])
    for _ in range(5):
        _deliver(db, [c.id, d.id])
    db.commit()
    monkeypatch.setattr(sleep_mod, "MAX_NEW_COUSE_EDGES", 1)
    stats = sleep_consolidate(db)
    assert stats["linked"] == 1
    link = _links(db)[0]
    assert {link.src_id, link.dst_id} == {a.id, b.id}


def test_link_queue_falls_back_to_count_when_no_reward(db, monkeypatch):
    """全零奖励时退化为旧行为：次数降序。"""
    a = _insight(db, "低频甲", "同现 2 次甲")
    b = _insight(db, "低频乙", "同现 2 次乙")
    c = _insight(db, "高频甲", "同现 5 次甲")
    d = _insight(db, "高频乙", "同现 5 次乙")
    _deliver(db, [a.id, b.id])
    _deliver(db, [a.id, b.id])
    for _ in range(5):
        _deliver(db, [c.id, d.id])
    db.commit()
    monkeypatch.setattr(sleep_mod, "MAX_NEW_COUSE_EDGES", 1)
    sleep_consolidate(db)
    link = _links(db)[0]
    assert {link.src_id, link.dst_id} == {c.id, d.id}


def test_negative_reward_demoted_not_excluded(db, monkeypatch):
    """负奖励对排队尾（cap=1 时输给中性对），但容量有余时仍建边——降权≠删除。"""
    a = _insight(db, "差评经验", "被差评的经验", feedback_neg=3)
    b = _insight(db, "差评搭档", "与差评经验同现")
    c = _insight(db, "中性甲", "中性对甲")
    d = _insight(db, "中性乙", "中性对乙")
    for _ in range(5):
        _deliver(db, [a.id, b.id])
    _deliver(db, [c.id, d.id])
    _deliver(db, [c.id, d.id])
    db.commit()
    monkeypatch.setattr(sleep_mod, "MAX_NEW_COUSE_EDGES", 1)
    sleep_consolidate(db)
    link = _links(db)[0]
    assert {link.src_id, link.dst_id} == {c.id, d.id}
    # 第二轮（新水位后无新交付不影响全量对）：放开容量，负奖励对仍建边
    monkeypatch.setattr(sleep_mod, "MAX_NEW_COUSE_EDGES", 50)
    stats = sleep_consolidate(db)
    assert stats["linked"] == 1
    pairs = [{li.src_id, li.dst_id} for li in _links(db)]
    assert {a.id, b.id} in pairs


# ---------- 簇抽象队列：簇奖励优先 ----------

class _FakeLinker:
    def generalize(self, views):
        return ("抽象原则", "从同现簇抽象出的原则内容", "适用情境")


def _cluster(db, members):
    """members: [(subject, project, feedback_pos)]，链式同现 ×2 连成簇。"""
    rows = [_insight(db, s, f"内容{s}", project=p, feedback_pos=f)
            for s, p, f in members]
    for x, y in zip(rows, rows[1:], strict=False):
        _deliver(db, [x.id, y.id])
        _deliver(db, [x.id, y.id])
    return rows


def test_abstraction_queue_cluster_reward_first(db, monkeypatch):
    """3 成员高奖励簇胜过 5 成员零奖励簇（cap 压到 1）。"""
    big = _cluster(db, [(f"大簇{i}", "pa" if i % 2 == 0 else "pb", 0)
                        for i in range(5)])
    small = _cluster(db, [("小簇甲", "pa", 3), ("小簇乙", "pb", 0),
                          ("小簇丙", "pa", 0)])
    db.commit()
    monkeypatch.setattr(sleep_mod, "MAX_NEW_PRINCIPLES", 1)
    stats = sleep_consolidate(db, linker=_FakeLinker())
    assert stats["generalized"] == 1
    report = db.scalar(select(m.ConsolidationReport).where(
        m.ConsolidationReport.user_id == "alice"))
    gid = report.generalized_ids[0]["id"]
    touched = {li.src_id for li in _links(db) if li.dst_id == gid}
    touched |= {li.dst_id for li in _links(db) if li.src_id == gid}
    small_ids = {r.id for r in small}
    big_ids = {r.id for r in big}
    assert touched & small_ids and not (touched & big_ids)


def test_abstraction_thresholds_still_block(db):
    """门槛不动：单项目簇、规模不足簇仍不抽象（奖励再高也不行）。"""
    _cluster(db, [("单项目甲", "pa", 5), ("单项目乙", "pa", 5),
                  ("单项目丙", "pa", 5)])
    db.commit()
    stats = sleep_consolidate(db, linker=_FakeLinker())
    assert stats["generalized"] == 0


# ---------- 衰减：补上任务成功 ----------

def test_decay_counts_task_success(db):
    plain = _insight(db, "普通经验", "闲置 60 天", last_used_at=NOW - timedelta(days=60))
    succ = _insight(db, "成功经验", "闲置 60 天且任务成功过",
                    last_used_at=NOW - timedelta(days=60), success_count=1)
    db.commit()
    stats = consolidate_insights(db, now=NOW)
    assert stats["decayed"] == 2
    # 0 次复述：0.5 × 0.95；成功 1 次：因子 1 - 0.05/2 = 0.975
    assert _conf(db, plain.id) == pytest.approx(0.5 * 0.95, abs=1e-4)
    assert _conf(db, succ.id) == pytest.approx(0.5 * 0.975, abs=1e-4)


# ---------- 置信度不动 / 自生成不自奖励 ----------

def test_sleep_does_not_touch_confidence(db):
    row = _insight(db, "高奖励经验", "已被多次好评", feedback_pos=5,
                   confidence=0.7)
    db.commit()
    sleep_consolidate(db)
    assert _conf(db, row.id) == pytest.approx(0.7, abs=1e-9)


def test_synthesized_principle_starts_at_zero_reward(db):
    _cluster(db, [("簇甲", "pa", 2), ("簇乙", "pb", 1), ("簇丙", "pa", 0)])
    db.commit()
    stats = sleep_consolidate(db, linker=_FakeLinker())
    assert stats["generalized"] == 1
    report = db.scalar(select(m.ConsolidationReport).where(
        m.ConsolidationReport.user_id == "alice"))
    gid = report.generalized_ids[0]["id"]
    db.expire_all()
    principle = db.get(m.MemoryInsight, gid)
    assert (principle.feedback_pos or 0) == 0
    assert (principle.success_count or 0) == 0
    assert (principle.auto_pos or 0) == 0
    assert (principle.auto_neg or 0) == 0
    assert reward_score(principle) == 0.0


# ---------- 隔离 / 墓碑 / 开关 ----------

def test_cross_user_reward_isolation(db):
    a = _insight(db, "alice 奖励", "alice 的好评经验", feedback_pos=5)
    b = _insight(db, "alice 搭档", "alice 搭档")
    c = _insight(db, "bob 甲", "bob 的经验", user="bob")
    d = _insight(db, "bob 乙", "bob 的搭档", user="bob")
    _deliver(db, [a.id, b.id])
    _deliver(db, [a.id, b.id])
    _deliver(db, [c.id, d.id], user="bob")
    _deliver(db, [c.id, d.id], user="bob")
    db.commit()
    sleep_consolidate(db)
    alice_links = _links(db, "alice")
    assert len(alice_links) == 1
    assert {alice_links[0].src_id, alice_links[0].dst_id} == {a.id, b.id}


def test_closed_high_reward_row_does_not_link(db):
    a = _insight(db, "已关高奖励", "关闭前被好评", feedback_pos=5)
    b = _insight(db, "搭档", "与已关行同现")
    _deliver(db, [a.id, b.id])
    _deliver(db, [a.id, b.id])
    a.valid_to = NOW
    db.commit()
    stats = sleep_consolidate(db)
    assert stats["linked"] == 0


def test_switched_off_user_skipped(db):
    _set_switch(db, "bob", False)
    _insight(db, "alice 经验", "alice 的经验")
    _insight(db, "bob 经验", "bob 的经验", user="bob")
    db.commit()
    sleep_consolidate(db)
    users = {r.user_id for r in db.scalars(select(m.ConsolidationReport))}
    assert "alice" in users and "bob" not in users


# ---------- 成本有界：工作量由交付驱动，不由总行数驱动 ----------

def test_link_cost_driven_by_deliveries_not_rows(db):
    for i in range(2000):
        _insight(db, f"填充{i}", f"填充内容{i}")
    a = _insight(db, "合格甲", "合格对甲")
    b = _insight(db, "合格乙", "合格对乙")
    c = _insight(db, "合格丙", "合格对丙")
    d = _insight(db, "合格丁", "合格对丁")
    _deliver(db, [a.id, b.id])
    _deliver(db, [a.id, b.id])
    _deliver(db, [c.id, d.id])
    _deliver(db, [c.id, d.id])
    db.commit()
    stats = sleep_consolidate(db)
    assert stats["linked"] == 2  # 2000 填充行没有交付，永不进入对计算
