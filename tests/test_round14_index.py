"""第十四轮补完（关键词倒排索引）：索引维护的同事务性、探针计数与逐行打分的严格等价。

第一版的 LIKE 计数门地板 ~60ms/5000 行，补完改倒排：token 集合落表、
探针按索引数命中数。这批测试守三件事：索引跟着父行同生共死（增/改/删、
非文本更新不扰动）；探针带回的命中数与 kw_score 分子逐分相等（不是超集
近似）；探针路径与全量路径在同一份数据上给出完全一致的召回结果。
"""
import random

from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.episodes import brief_for_task
from app.recall import _QueryPlan, recall_facts
from app.tokenize import MAX_TOKEN_LEN, tokens_of

U = UserContext(app_id=settings.app_id, user_id="alice")


class FakeEmbedder:
    model = "fake-embed-1"

    def embed(self, texts, task_type="RETRIEVAL_QUERY"):
        return [[1.0, 0.0] for _ in texts]


def _fact(db, subject, content, **kw):
    row = m.MemoryFact(app_id=settings.app_id, user_id="alice",
                       subject=subject, content=content, **kw)
    db.add(row)
    db.commit()
    return row


def _insight(db, subject, content, applies="", **kw):
    row = m.MemoryInsight(app_id=settings.app_id, user_id="alice", kind="lesson",
                          subject=subject, content=content, applies_when=applies, **kw)
    db.add(row)
    db.commit()
    return row


def _fact_tokens(db, fact_id):
    return set(db.scalars(select(m.MemoryFactToken.token)
                          .where(m.MemoryFactToken.fact_id == fact_id)))


def _insight_tokens(db, insight_id):
    return set(db.scalars(select(m.MemoryInsightToken.token)
                          .where(m.MemoryInsightToken.insight_id == insight_id)))


# ---------- 索引维护：与父行同事务 ----------

def test_postings_written_on_insert(db):
    f = _fact(db, "饮品偏好", "用户喜欢喝咖啡，不加糖")
    assert _fact_tokens(db, f.id) == tokens_of("饮品偏好 用户喜欢喝咖啡，不加糖")
    i = _insight(db, "部署经验", "上线前先查端口占用", applies="发布服务时")
    assert _insight_tokens(db, i.id) == tokens_of("部署经验 上线前先查端口占用 发布服务时")


def test_postings_follow_content_update(db):
    f = _fact(db, "饮品偏好", "用户喜欢喝咖啡")
    f.content = "用户改喝红茶了"
    db.commit()
    assert _fact_tokens(db, f.id) == tokens_of("饮品偏好 用户改喝红茶了")


def test_postings_untouched_by_nontext_update(db):
    i = _insight(db, "部署经验", "上线前先查端口占用")
    before = _insight_tokens(db, i.id)
    i.use_count = 7
    i.confidence = 0.9
    db.commit()
    assert _insight_tokens(db, i.id) == before


def test_postings_removed_on_orm_delete(db):
    f = _fact(db, "饮品偏好", "用户喜欢喝咖啡")
    db.delete(f)
    db.commit()
    assert _fact_tokens(db, f.id) == set()


def test_overlong_token_not_indexed(db):
    long_word = "a" * (MAX_TOKEN_LEN + 10)
    f = _fact(db, "长串", f"值是 {long_word} 别存索引")
    assert long_word not in _fact_tokens(db, f.id)
    assert "长串" in _fact_tokens(db, f.id)


def test_overlong_query_token_falls_back_and_still_hits(db):
    """查询带超长 token 时探针不成立，必须退全量精确打分而不是漏召回。"""
    long_word = "b" * (MAX_TOKEN_LEN + 10)
    _fact(db, "长串事实", f"配置值 {long_word} 已生效")
    out = recall_facts(db, U, long_word, k=5, token_budget=1500)
    assert [o.subject for o in out] == ["长串事实"]


# ---------- 探针计数 == 逐行打分：逐分等价 ----------

def test_probe_score_equals_kw_score(db):
    _fact(db, "饮品偏好", "用户喜欢喝咖啡，不加糖，早上来一杯")
    q = "咖啡加糖吗"
    out = recall_facts(db, U, q, k=5, token_budget=1500)
    assert len(out) == 1
    expect = _QueryPlan(q).kw_score("饮品偏好 用户喜欢喝咖啡，不加糖，早上来一杯")
    assert out[0].score == round(expect, 4)


def test_probe_path_matches_full_load_path(db):
    """同一份数据：探针路径（无嵌入器）与全量路径（SQLite 带向量、行无向量）
    必须给出完全一致的结果——索引只许加速，不许改语义。"""
    rng = random.Random(20261024)
    frags = ["部署", "端口", "咖啡", "咖喱", "茶", "水", "api", "port", "ports", "8080",
             "限流", "重试", " log ", "，", "。", " ", "：", "A", "b", "3", "迁移", "数据库"]
    for i in range(60):
        _fact(db, f"主题{i}", "".join(rng.choice(frags) for _ in range(rng.randint(2, 8))))
    for q in ["部署端口", "api 限流重试", "茶", "port 8080", "完全不搭界的查询词", "咖啡和茶"]:
        probe_out = recall_facts(db, U, q, k=5, token_budget=1500)
        full_out = recall_facts(db, U, q, k=5, token_budget=1500, embedder=FakeEmbedder())
        assert [(o.fact_id, o.score, o.reason) for o in probe_out] == \
               [(o.fact_id, o.score, o.reason) for o in full_out], q


def test_brief_probe_path_matches_full_load_path(db):
    _insight(db, "部署端口", "上线前先用 ss 查监听占用，冲突就换端口", applies="发布服务时")
    _insight(db, "缓存策略", "热点键设置随机过期抖动防雪崩", applies="设计缓存时")
    _insight(db, "日志排障", "先按 trace_id 串链路，再看错误码分布", applies="线上报错时")
    q = "部署服务时端口被占用怎么处理"
    probe_out = brief_for_task(db, U, q, 5)
    full_out = brief_for_task(db, U, q, 5, embedder=FakeEmbedder())
    assert [(o.insight_id, o.score, o.reason) for o in probe_out] == \
           [(o.insight_id, o.score, o.reason) for o in full_out]


def test_brief_probe_keeps_pinned_and_drops_muted(db):
    _insight(db, "置顶经验", "与查询毫无字面关系的置顶条目", pinned=True)
    _insight(db, "禁用经验", "部署端口相关的禁用条目", muted=True)
    _insight(db, "部署端口", "上线前先查端口占用", applies="发布时")
    out = brief_for_task(db, U, "部署端口冲突", 5)
    subjects = {o.subject for o in out}
    assert "置顶经验" in subjects and "部署端口" in subjects
    assert "禁用经验" not in subjects
    pinned = next(o for o in out if o.subject == "置顶经验")
    assert pinned.reason == "pinned"
