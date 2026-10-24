"""第二十二轮：遗忘曲线——锚点结清、复述依赖因子、边基线对齐、前后召回变化。

核心是修一个旧 bug：consolidate 的衰减没有结清锚点，每小时巡检都会把
同一段闲置再乘一次（宽限期后实际按小时复利）。锚点算法的总额与旧单次
口径一致（day 100 合计 3 块），只修重复计费；复述（任务回流证实应用 +
人工好评）让月因子越近 1；边衰减的基线从“最近交付”改为“最近应用”，
曝光不再重置边的遗忘时钟。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.edge_learning import decay_idle_edges
from app.episodes import brief_for_task, consolidate_insights
from app.links import create_link

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")
NOW = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)


def _insight(db, subject, content, confidence=0.5, last_used=None,
             applied=0, feedback_pos=0):
    row = m.MemoryInsight(app_id=APP, user_id="alice", kind="strategy",
                          subject=subject, content=content, confidence=confidence,
                          last_used_at=last_used, applied_count=applied,
                          feedback_pos=feedback_pos)
    db.add(row)
    db.flush()
    return row


def _conf(db, row_id):
    db.expire_all()
    return db.get(m.MemoryInsight, row_id).confidence


# ---------- 锚点结清 ----------

def test_decay_anchor_no_double_charge(db):
    row = _insight(db, "部署端口", "部署前先探测端口占用再启动",
                   last_used=NOW - timedelta(days=100))
    db.commit()
    stats = consolidate_insights(db, now=NOW)
    assert stats["decayed"] == 1
    # 首扣：(100-1)//30 = 3 块，0.5 × 0.95³
    assert _conf(db, row.id) == pytest.approx(0.5 * 0.95 ** 3, abs=1e-4)
    # 同一时刻再跑：锚点已到 NOW，不足整月，不再扣
    stats = consolidate_insights(db, now=NOW)
    assert stats["decayed"] == 0
    assert _conf(db, row.id) == pytest.approx(0.5 * 0.95 ** 3, abs=1e-4)
    # 40 天后：续扣 exactly 1 块；总额 4 块 = 旧单次口径 day 140 的 (140-1)//30
    stats = consolidate_insights(db, now=NOW + timedelta(days=40))
    assert stats["decayed"] == 1
    assert _conf(db, row.id) == pytest.approx(0.5 * 0.95 ** 4, abs=1e-4)


def test_rehearsal_refresh_restarts_grace(db):
    row = _insight(db, "部署端口", "部署前先探测端口占用再启动",
                   last_used=NOW - timedelta(days=100))
    db.commit()
    consolidate_insights(db, now=NOW)
    assert _conf(db, row.id) == pytest.approx(0.5 * 0.95 ** 3, abs=1e-4)
    # 被用上（回流/反馈刷新 last_used_at）：锚点被新的最后使用盖过，宽限重算
    row.last_used_at = NOW + timedelta(days=10)
    db.commit()
    stats = consolidate_insights(db, now=NOW + timedelta(days=35))
    assert stats["decayed"] == 0  # 距最后使用仅 25 天，宽限内不扣
    stats = consolidate_insights(db, now=NOW + timedelta(days=41))
    assert stats["decayed"] == 1  # 距最后使用 31 天，宽限公式扣 1 块
    assert _conf(db, row.id) == pytest.approx(0.5 * 0.95 ** 4, abs=1e-4)


# ---------- 复述依赖因子 ----------

def test_rehearsal_slows_decay(db):
    plain = _insight(db, "部署端口", "部署前先探测端口占用再启动",
                     last_used=NOW - timedelta(days=60))
    rehearsed = _insight(db, "日志保留", "日志按日轮转保留七天",
                         last_used=NOW - timedelta(days=60),
                         applied=3, feedback_pos=1)
    db.commit()
    consolidate_insights(db, now=NOW)
    # 0 次复述：因子 0.95；4 次复述：因子 1 - 0.05/5 = 0.99
    assert _conf(db, plain.id) == pytest.approx(0.5 * 0.95, abs=1e-4)
    assert _conf(db, rehearsed.id) == pytest.approx(0.5 * 0.99, abs=1e-4)


# ---------- 边基线对齐 ----------

def test_edge_decay_ignores_unapplied_delivery(db):
    a = _insight(db, "陈年甲", "陈年做法甲")
    b = _insight(db, "陈年乙", "陈年做法乙")
    link = create_link(db, APP, "alice", ("insight", a.id), ("insight", b.id),
                       "related", 0.9, "rule")
    link.created_at = NOW - timedelta(days=60)
    db.add(m.BriefDeliveryEdge(app_id=APP, user_id="alice", delivery_id="d1",
                               link_id=link.id, insight_id=b.id, applied=False,
                               created_at=NOW))
    db.commit()
    # 近期交付但未应用：基线仍是创建（60 天前），衰减 2 个月
    assert decay_idle_edges(db, now=NOW) == 1
    db.commit()
    db.expire_all()
    assert db.get(m.MemoryLink, link.id).strength == pytest.approx(0.9 * 0.95 ** 2)


def test_edge_decay_skips_recently_applied(db):
    a = _insight(db, "陈年甲", "陈年做法甲")
    b = _insight(db, "陈年乙", "陈年做法乙")
    link = create_link(db, APP, "alice", ("insight", a.id), ("insight", b.id),
                       "related", 0.9, "rule")
    link.created_at = NOW - timedelta(days=60)
    db.add(m.BriefDeliveryEdge(app_id=APP, user_id="alice", delivery_id="d1",
                               link_id=link.id, insight_id=b.id, applied=True,
                               created_at=NOW))
    db.commit()
    assert decay_idle_edges(db, now=NOW) == 0
    db.commit()


# ---------- 前后召回变化 ----------

def _brief_score(db, insight_id):
    out = brief_for_task(db, U, "部署服务要绑定端口", 5)
    by_id = {o.insight_id: o for o in out}
    assert insight_id in by_id, [o.insight_id for o in out]
    return by_id[insight_id].score


def test_recall_change_before_after_decay(db):
    aged = _insight(db, "部署端口选择", "部署前先探测端口占用再启动服务",
                    last_used=NOW - timedelta(days=100))
    fresh = _insight(db, "服务端口", "启动服务前先探测端口占用",
                     last_used=NOW, applied=2)
    db.commit()
    aged_before = _brief_score(db, aged.id)
    fresh_before = _brief_score(db, fresh.id)
    consolidate_insights(db, now=NOW)
    aged_after = _brief_score(db, aged.id)
    fresh_after = _brief_score(db, fresh.id)
    # 闲置组：置信度被衰减，简报分数下降；复述组：未衰减，分数不变
    assert aged_after < aged_before
    assert fresh_after == fresh_before
