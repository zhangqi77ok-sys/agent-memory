"""第二轮修复：subject 相似合并（#3）、候选预过滤+咨询锁（#2/#4）、余弦门槛与 token 估算（#5/#6）。"""
from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.extract import same_subject
from app.recall import _est_tokens, recall_facts
from app.worker import _apply_fact, _apply_preference, _lock_user


def test_same_subject():
    assert same_subject("咖啡", "咖啡偏好")  # 包含
    assert same_subject("居住城市", "城市")
    assert same_subject("喝咖啡的习惯", "咖啡")
    assert same_subject("Lang", "language")  # casefold + 包含
    assert not same_subject("咖啡", "咖喱")  # 只共享一个字
    assert not same_subject("工作", "工资")
    assert not same_subject("饮食偏好", "饮食习惯")  # jaccard 0.2，宁可漏合并
    assert not same_subject("", "咖啡")


def _job(db, key="k1"):
    job = m.MemoryJob(user_id="alice", idempotency_key=key, session_id="s1")
    db.add(job)
    db.commit()
    return job


def _active_facts(db, user="alice"):
    return list(db.scalars(select(m.MemoryFact).where(
        m.MemoryFact.user_id == user, m.MemoryFact.valid_to.is_(None))))


def test_worker_merges_near_duplicate_subject(db):
    job = _job(db)
    assert _apply_fact(db, job, "咖啡", "喜欢拿铁", "ADD", None)[0] == "add"
    # 模型这次换了个 subject 说法：应合并到同一件事上，而不是新旧并存
    assert _apply_fact(db, job, "咖啡偏好", "喜欢燕麦拿铁", "UPDATE", None)[0] == "update"
    db.commit()
    active = _active_facts(db)
    assert len(active) == 1
    assert active[0].subject == "咖啡偏好" and active[0].content == "喜欢燕麦拿铁"
    all_rows = list(db.scalars(select(m.MemoryFact).where(m.MemoryFact.user_id == "alice")))
    assert len(all_rows) == 2 and all_rows[0].valid_to is not None  # 旧行被失效，不是删除


def test_worker_merge_noop_when_value_same(db):
    job = _job(db)
    _apply_fact(db, job, "咖啡", "喜欢拿铁", "ADD", None)
    decision, fid = _apply_fact(db, job, "咖啡偏好", "喜欢拿铁", "ADD", None)
    db.commit()
    assert decision == "noop" and fid is None
    assert len(_active_facts(db)) == 1


def test_worker_merge_leaves_unrelated_subject_alone(db):
    job = _job(db)
    _apply_fact(db, job, "咖啡", "喜欢拿铁", "ADD", None)
    _apply_fact(db, job, "天气", "喜欢晴天", "ADD", None)
    _apply_fact(db, job, "咖啡偏好", "喜欢燕麦拿铁", "UPDATE", None)
    db.commit()
    subjects = {f.subject for f in _active_facts(db)}
    assert subjects == {"咖啡偏好", "天气"}


def test_worker_merges_near_duplicate_preference_key(db):
    job = _job(db)
    assert _apply_preference(db, job, "语言", "中文", "ADD")[0] == "add"
    assert _apply_preference(db, job, "语言偏好", "英文", "UPDATE")[0] == "update"
    db.commit()
    active = list(db.scalars(select(m.MemoryPreference).where(
        m.MemoryPreference.user_id == "alice", m.MemoryPreference.valid_to.is_(None))))
    assert len(active) == 1 and active[0].value == "英文"


def test_lock_user_is_noop_on_sqlite(db):
    _lock_user(db, _job(db))  # 不抛错即可；Postgres 路径靠咨询锁，SQLite 天然串行


class QueryEmbedder:
    model = "fake-q"

    def embed(self, texts, task_type="RETRIEVAL_QUERY"):
        return [[1.0, 0.0] for _ in texts]


def _fact(db, subject, content, embedding=None):
    f = m.MemoryFact(user_id="alice", subject=subject, content=content, embedding=embedding)
    db.add(f)
    db.commit()
    return f


def test_recall_cosine_gate_blocks_middling_similarity(db):
    hit = _fact(db, "咖啡", "喜欢拿铁", embedding=[1.0, 0.0])       # cos = 1.0
    _fact(db, "天气", "今天晴天", embedding=[0.6, 0.8])              # cos = 0.6 < 0.66
    kw_hit = _fact(db, "饮品", "提神饮料有咖啡因", embedding=None)   # 无向量，靠关键词
    u = UserContext(app_id=settings.app_id, user_id="alice")
    out = recall_facts(db, u, "提神饮料", k=5, token_budget=1500, embedder=QueryEmbedder())
    ids = {f.fact_id for f in out}
    assert hit.id in ids and kw_hit.id in ids
    # 关键回归：cos=0.6 的融合分 0.42 远超旧的 0.10，若只看融合分会被误召回
    assert len(out) == 2


def test_est_tokens():
    assert _est_tokens("咖啡") == 2
    assert _est_tokens("a" * 40) == 10
    assert _est_tokens("") == 1
    assert _est_tokens("咖啡coffee") == 2 + 6 // 4
