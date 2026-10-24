"""第十八轮：类人记忆审查 P2 收尾——语义去重、合并留痕、预算跳过、
元记忆降级信号、提交完整性校验、COS_MIN 再校准脚本。"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.episodes import brief_for_task, consolidate_insights, submit_episode, validate_episode
from app.recall import recall_facts
from app.worker import _apply_fact, _apply_preference
from scripts.recalibrate_cos import suggest_threshold

U = UserContext(app_id=settings.app_id, user_id="alice")
NOW = datetime.now(timezone.utc)
APP = settings.app_id


class MapEmbedder:
    """按文本查向量的假嵌入器：同一模型名，供语义去重测试精确控制余弦。"""
    model = "fake-embed-1"

    def __init__(self, mapping):
        self.mapping = mapping

    def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        return [list(self.mapping.get(t, [0.0, 1.0])) for t in texts]


def _job(db, user="alice"):
    job = m.MemoryJob(app_id=APP, user_id=user, session_id="s1",
                      idempotency_key=f"k-{uuid.uuid4().hex[:12]}")
    db.add(job)
    db.commit()
    return job


def _active_facts(db):
    return list(db.scalars(select(m.MemoryFact).where(m.MemoryFact.valid_to.is_(None))))


# ---------- 1. 语义去重（跨措辞） ----------

def test_semantic_dup_fact_noop_and_support(db):
    job = _job(db)
    _apply_fact(db, job, "咖啡", "喜欢拿铁", "ADD", None,
                embedder=MapEmbedder({"咖啡 喜欢拿铁": [1.0, 0.0]}))
    db.commit()
    # 主题措辞不同（咖啡饮品 vs 咖啡），内容相同，向量 0.99——同一件事
    d, _ = _apply_fact(db, job, "咖啡饮品", "喜欢拿铁", "ADD", None,
                       embedder=MapEmbedder({"咖啡饮品 喜欢拿铁": [0.99, 0.14]}))
    db.commit()
    assert d == "noop"
    facts = _active_facts(db)
    assert len(facts) == 1
    assert facts[0].support_count == 2  # 跨措辞命中同样算一次佐证


def test_semantic_dup_fact_update(db):
    job = _job(db)
    _apply_fact(db, job, "咖啡", "喜欢拿铁", "ADD", None,
                embedder=MapEmbedder({"咖啡 喜欢拿铁": [1.0, 0.0]}))
    db.commit()
    d, _ = _apply_fact(db, job, "咖啡饮品", "改喝美式", "ADD", None,
                       embedder=MapEmbedder({"咖啡饮品 改喝美式": [0.99, 0.14]}))
    db.commit()
    assert d == "update"
    facts = _active_facts(db)
    assert len(facts) == 1 and facts[0].content == "改喝美式" and facts[0].version == 2


def test_semantic_far_vector_adds(db):
    job = _job(db)
    _apply_fact(db, job, "咖啡", "喜欢拿铁", "ADD", None,
                embedder=MapEmbedder({"咖啡 喜欢拿铁": [1.0, 0.0]}))
    db.commit()
    d, _ = _apply_fact(db, job, "茶", "喜欢绿茶", "ADD", None,
                       embedder=MapEmbedder({"茶 喜欢绿茶": [0.0, 1.0]}))
    db.commit()
    assert d == "add"
    assert len(_active_facts(db)) == 2


def test_semantic_dup_preference(db):
    job = _job(db)
    _apply_preference(db, job, "咖啡偏好", "拿铁", "ADD",
                      embedder=MapEmbedder({"咖啡偏好 拿铁": [1.0, 0.0]}))
    db.commit()
    d, _ = _apply_preference(db, job, "咖啡口味", "拿铁", "ADD",
                             embedder=MapEmbedder({"咖啡口味 拿铁": [0.99, 0.14]}))
    db.commit()
    assert d == "noop"
    prefs = list(db.scalars(select(m.MemoryPreference).where(
        m.MemoryPreference.valid_to.is_(None))))
    assert len(prefs) == 1 and prefs[0].support_count == 2


# ---------- 2. 合并保留败者内容 ----------

def test_merge_keeps_loser_content(db):
    a = m.MemoryInsight(app_id=APP, user_id="alice", kind="pitfall",
                        subject="端口探测", content="先探测端口再绑定",
                        applies_when="部署绑定端口时", confidence=0.8,
                        feedback_pos=3, valid_from=NOW)
    b = m.MemoryInsight(app_id=APP, user_id="alice", kind="pitfall",
                        subject="端口探测", content="先探测端口再绑定",
                        applies_when="换机器部署时也要探测", confidence=0.5,
                        feedback_pos=1, valid_from=NOW)
    db.add_all([a, b])
    db.commit()
    stats = consolidate_insights(db, now=NOW)
    assert stats["merged"] == 1
    alive = list(db.scalars(select(m.MemoryInsight).where(
        m.MemoryInsight.valid_to.is_(None))))
    assert len(alive) == 1
    assert "合并自同类经验" in alive[0].content  # 败者表述留痕，不再静默丢弃
    assert "换机器部署时也要探测" in (alive[0].applies_when or "")


# ---------- 3. 预算装包：跳过而非截断 ----------

def test_fact_budget_skips_big_item(db):
    # 两条主题不同（同主题会被写入侧 UPDATE 合并），但都命中查询 token
    job = _job(db)
    _apply_fact(db, job, "部署", "大" * 2000, "ADD", None)
    _apply_fact(db, job, "端口", "小结", "ADD", None)
    db.commit()
    # 大事实置信高排前面（0.9 vs 默认 0.5），预算 1500 装不下它时旧行为截断为空
    big = [f for f in _active_facts(db) if len(f.content) > 100][0]
    big.confidence = 0.9
    db.commit()
    out = recall_facts(db, U, "部署端口", k=5, token_budget=1500)
    assert [o.content for o in out] == ["小结"]


def test_brief_budget_skips_big_item(db):
    big = m.MemoryInsight(app_id=APP, user_id="alice", kind="pitfall",
                          subject="部署端口", content="大" * 2000,
                          applies_when="部署绑定端口时", confidence=0.9,
                          valid_from=NOW)
    small = m.MemoryInsight(app_id=APP, user_id="alice", kind="pitfall",
                            subject="部署端口", content="小结",
                            applies_when="部署绑定端口时", confidence=0.5,
                            valid_from=NOW)
    db.add_all([big, small])
    db.commit()
    out = brief_for_task(db, U, "部署端口", 5, token_budget=1500)
    assert [i.content for i in out] == ["小结"]


# ---------- 4. 元记忆：降级信号 ----------

def test_brief_meta_degraded_signals(db):
    meta: dict = {}
    brief_for_task(db, U, "部署端口", 5, meta=meta)
    assert meta == {"degraded": True, "degraded_reason": "no_embedder"}

    class Boom:
        def embed(self, *a, **k):
            raise RuntimeError("boom")

    meta = {}
    brief_for_task(db, U, "部署端口", 5, embedder=Boom(), meta=meta)
    assert meta == {"degraded": True, "degraded_reason": "embed_failed"}


# ---------- 5. 提交完整性校验 ----------

@pytest.mark.parametrize("kw", [
    {"title": "  ", "outcome": "success", "steps": [{"action": "做"}]},
    {"title": "t", "outcome": "done", "steps": [{"action": "做"}]},
    {"title": "t", "outcome": "success", "steps": []},
    {"title": "t", "outcome": "success", "steps": [{"observation": "无动作"}]},
    {"title": "t", "outcome": "success", "steps": [{"action": "做"}],
     "cost": {"tokens": -5}},
])
def test_submit_validation_rejects(kw):
    with pytest.raises(ValueError):
        validate_episode(kw["title"], kw["outcome"], kw["steps"], kw.get("cost"))


def test_submit_episode_validation_enforced(db):
    with pytest.raises(ValueError):
        submit_episode(db, U, source="t", title="t", outcome="success",
                       steps=[], cost={})
    assert db.query(m.TaskEpisode).count() == 0


# ---------- 6. COS_MIN 再校准脚本（只建议，不改阈值） ----------

def test_recalibrate_separable_midpoint():
    pairs = [(0.70 + 0.01 * i, True) for i in range(9)] + \
            [(0.50 + 0.02 * i, False) for i in range(8)]
    r = suggest_threshold(pairs)
    assert r["separable"] is True
    assert r["suggested"] == pytest.approx((0.64 + 0.70) / 2, abs=1e-3)
    assert r["current_eval"] == {"false_positive": 0, "false_negative": 0}


def test_recalibrate_overlap_youden():
    pairs = [(0.70, True), (0.60, True), (0.65, False), (0.55, False)]
    r = suggest_threshold(pairs)
    assert r["separable"] is False and r["suggested"] is not None
