"""第十四轮（索引化召回）：打分等价性、通道完备性、PG 向量索引与通道召回。

核心纪律：这一轮只许变快，不许变语义——关键词打分从「全文分词求交集」换成
「按查询计划做存在性测试」，必须逐分等价；SQL 通道只负责压候选，凡通道压不
完备的情形（SQLite 带向量等）必须保全量，纯语义命中一条都不能丢。
"""
import os
import random

import pytest
from sqlalchemy import text

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.episodes import brief_for_task
from app.recall import _QueryPlan, _score, recall_facts

U = UserContext(app_id=settings.app_id, user_id="alice")


class FakeEmbedder:
    model = "fake-embed-1"

    def __init__(self, vec=(1.0, 0.0)):
        self.vec = list(vec)

    def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        return [list(self.vec) for _ in texts]


# ---------- 关键词打分等价性：计划式存在性测试 == 旧分词集合交集 ----------

ADVERSARIAL = [
    ("茶", "茶水早餐"),          # 孤字在别人 bigram 里，不算命中
    ("茶", "想喝 茶"),           # 孤段，才算命中
    ("茶", "茶"),                # 文本本身就是孤段
    ("port", "ports are busy"),  # 长词的一部分不算命中
    ("port", "the port is 8080"),
    ("部署端口", "部署，端口被占用"),   # 标点断段：部署/端口在，署端不在
    ("部署端口", "部署端口冲突"),
    ("咖啡", "咖喱饭"),          # 共享单字不命中（bigram 纪律）
    ("api限流", "API 限流重试"),
    ("8080", "端口 8080 被占用"),
    ("a", "a b c"),
    ("a", "abc"),
    ("重试3次", "重试 3 次后成功"),
    ("", "任意文本"),
    ("部署", ""),
]


@pytest.mark.parametrize("q,t", ADVERSARIAL)
def test_kw_score_adversarial_equivalence(q, t):
    assert _QueryPlan(q).kw_score(t) == _score(q, t)


def test_kw_score_random_equivalence():
    rng = random.Random(20261024)
    frags = ["部署", "端口", "咖啡", "咖喱", "茶", "水", "api", "port", "ports", "8080",
             "限流", "重试", " log ", "，", "。", " ", "：", "A", "b", "3", "迁移", "数据库"]
    for _ in range(3000):
        q = "".join(rng.choice(frags) for _ in range(rng.randint(1, 4)))
        t = "".join(rng.choice(frags) for _ in range(rng.randint(1, 10)))
        assert _QueryPlan(q).kw_score(t) == _score(q, t), (q, t)


# ---------- SQLite：通道不得改变召回语义 ----------

def _fact(db, subject, content, **kw):
    row = m.MemoryFact(app_id=settings.app_id, user_id="alice",
                       subject=subject, content=content, **kw)
    db.add(row)
    db.commit()
    return row


def test_recall_keyword_channel_still_hits(db):
    _fact(db, "饮品偏好", "用户喜欢喝咖啡，不加糖")
    _fact(db, "工作", "用户在互联网公司做后端开发")
    out = recall_facts(db, U, "咖啡", k=5, token_budget=1500)
    assert [o.subject for o in out] == ["饮品偏好"]
    assert out[0].reason == "keyword"


def test_recall_tokenless_query_returns_empty(db):
    _fact(db, "饮品偏好", "用户喜欢喝咖啡")
    assert recall_facts(db, U, "！！！", k=5, token_budget=1500) == []


def test_recall_sqlite_semantic_only_hit_not_dropped(db):
    """SQLite 带向量时通道压不了候选，必须保全量：零字面重叠的语义命中不能丢。"""
    _fact(db, "饮品偏好", "用户喜欢喝咖啡", embedding=[1.0, 0.0], embed_model="fake-embed-1")
    out = recall_facts(db, U, "提神饮料来一杯", k=5, token_budget=1500,
                       embedder=FakeEmbedder((1.0, 0.0)))
    assert [o.subject for o in out] == ["饮品偏好"]
    assert out[0].reason == "hybrid"


def _insight(db, subject, content, applies="", **kw):
    row = m.MemoryInsight(app_id=settings.app_id, user_id="alice", kind="lesson",
                          subject=subject, content=content, applies_when=applies, **kw)
    db.add(row)
    db.commit()
    return row


def test_brief_pinned_survives_channel_filter(db):
    """置顶经验零字面重叠也要必达：置顶通道保证它进候选集。"""
    _insight(db, "无关主题", "跟任务毫不相干的经验内容", pinned=True)
    out = brief_for_task(db, U, "部署服务时端口被占用怎么处理", 5)
    assert len(out) == 1 and out[0].reason == "pinned"


def test_brief_sqlite_semantic_only_hit_not_dropped(db):
    _insight(db, "饮品经验", "提神饮品相关的经验总结", embedding=[1.0, 0.0],
             embed_model="fake-embed-1")
    out = brief_for_task(db, U, "完全不搭界的任务描述", 5, embedder=FakeEmbedder((1.0, 0.0)))
    assert [o.subject for o in out] == ["饮品经验"]


def test_brief_bookkeeping_still_applies(db):
    row = _insight(db, "部署端口", "部署时先查端口占用再启动服务", "处理部署端口任务时")
    out = brief_for_task(db, U, "部署服务端口被占用", 5)
    assert [o.insight_id for o in out] == [row.id]
    db.expire_all()
    again = db.get(m.MemoryInsight, row.id)
    assert again.use_count == 1 and again.last_used_at is not None


# ---------- Postgres 专属：向量索引 + 向量通道 ----------

pytestmark_pg = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"),
                                   reason="needs Postgres")


@pytestmark_pg
def test_insight_hnsw_index_exists(db):
    idx = {r[0] for r in db.execute(text(
        "SELECT indexname FROM pg_indexes WHERE tablename='memory_insight'"))}
    assert "ix_insight_hnsw" in idx


@pytestmark_pg
def test_recall_pg_vector_channel_hit(db):
    """PG 上零字面重叠的事实只能靠向量通道进候选：通道不在，这条就召不回来。"""
    _fact(db, "饮品偏好", "用户喜欢喝咖啡", embedding=[1.0, 0.0], embed_model="fake-embed-1")
    out = recall_facts(db, U, "提神饮料来一杯", k=5, token_budget=1500,
                       embedder=FakeEmbedder((1.0, 0.0)))
    assert [o.subject for o in out] == ["饮品偏好"]
    assert out[0].reason == "hybrid"


@pytestmark_pg
def test_brief_pg_vector_channel_hit(db):
    _insight(db, "饮品经验", "提神饮品相关的经验总结", embedding=[1.0, 0.0],
             embed_model="fake-embed-1")
    out = brief_for_task(db, U, "完全不搭界的任务描述", 5, embedder=FakeEmbedder((1.0, 0.0)))
    assert [o.subject for o in out] == ["饮品经验"]
