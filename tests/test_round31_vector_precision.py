"""第三十一轮：提醒向量路的判定规则（FakeEmbedder，零网络）。

真实向量路的精度数字由 scripts/reminder_vector_precision.py 以真实
gemini-embedding-001 向量跑出（见 docs/eval.md 第 31 轮一节）；本文件只钉
判定规则本身，不依赖网络、不依赖真实向量值：

- 向量命中：余弦 ≥ COS_MIN（0.66）触发，低于则不触发；
- 模型不一致：存量向量模型与本次 embedder 模型不同 → 向量路不参与；
- 维度不一致：存量向量与查询向量长度不同 → 向量路不参与；
- 短条件 guard 不挡向量路：关键词不中 + 向量中 → 触发（第 27 轮 guard
  只管关键词分支，代码已确认）；
- check_triggers 端到端：fake 向量武装 → 向量命中 → 触发。

假向量统一 2 维：PG 套件下 EMBEDDING_DIM=2，pgvector 列维度必须一致；
SQLite 下 JSON 列不限维，2 维同样通过。
"""

import math

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.recall import COS_MIN
from app.reminders import (
    SHORT_COND_TOKENS,
    _trigger_score,
    check_triggers,
    create_reminder,
)
from app.tokenize import tokens_of

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")


class DictEmbedder:
    """按文本查表的假嵌入器：embed 返回预设向量，与 task_type 无关。"""

    def __init__(self, mapping, model="fake-embed-1"):
        self.mapping = mapping
        self.model = model

    def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        return [list(self.mapping[t]) for t in texts]


def _row(condition, embedding=None, embed_model=None):
    row = m.MemoryReminder(app_id=APP, user_id="alice", condition_text=condition, action_text="x")
    if embedding is not None:
        row.embedding = list(embedding)
    if embed_model is not None:
        row.embed_model = embed_model
    return row


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb)


def test_vector_hit_and_miss_by_cosine():
    # [1, 0] 与 [1, 0] 余弦 1.0 命中；与 [0, 1] 余弦 0 不命中；任务无 token
    # 交集，排除关键词路干扰（条件“上线前”与任务“月度报表”无共享 bigram）。
    row = _row("上线前", embedding=[1.0, 0.0], embed_model="fake-embed-1")
    task_tokens = tokens_of("月度报表")
    assert _trigger_score(row, task_tokens, [1.0, 0.0], "fake-embed-1") == 1.0
    assert _trigger_score(row, task_tokens, [0.0, 1.0], "fake-embed-1") is None
    # 边界：余弦恰好等于 COS_MIN 命中（构造 cos = 0.66 的向量对）。
    q = [0.66, math.sqrt(1 - 0.66**2)]
    assert abs(_cos([1.0, 0.0], q) - COS_MIN) < 1e-9
    assert _trigger_score(row, task_tokens, q, "fake-embed-1") is not None


def test_vector_ignored_on_model_mismatch():
    row = _row("上线前", embedding=[1.0, 0.0], embed_model="other-model")
    task_tokens = tokens_of("月度报表")
    assert _trigger_score(row, task_tokens, [1.0, 0.0], "fake-embed-1") is None


def test_vector_ignored_on_dim_mismatch():
    row = _row("上线前", embedding=[1.0, 0.0, 0.0], embed_model="fake-embed-1")
    task_tokens = tokens_of("月度报表")
    assert _trigger_score(row, task_tokens, [1.0, 0.0], "fake-embed-1") is None


def test_short_condition_guard_does_not_block_vector():
    # “上线前”2 token，任务“今天上线，前端有问题”关键词覆盖 0.5 < guard
    # 要求的 1.0，关键词路不中；向量余弦 0.9 ≥ COS_MIN，照常触发。
    assert len(tokens_of("上线前")) <= SHORT_COND_TOKENS
    row = _row("上线前", embedding=[1.0, 0.0], embed_model="fake-embed-1")
    task_tokens = tokens_of("今天上线，前端有问题")
    assert _trigger_score(row, task_tokens, None, None) is None
    assert _trigger_score(row, task_tokens, [0.9, math.sqrt(1 - 0.9**2)], "fake-embed-1") is not None


def test_check_triggers_end_to_end_with_fake_embedder(db):
    mapping = {
        "部署服务时": [1.0, 0.0],
        "把新版本推上机器": [0.9, math.sqrt(1 - 0.9**2)],
        "打印月度报表给财务": [0.0, 1.0],
    }
    emb = DictEmbedder(mapping)
    create_reminder(db, U, condition="部署服务时", action="先探测端口占用", once=False, embedder=emb)
    # 向量命中且归因干净：任务“把新版本推上机器”与条件“部署服务时”无
    # 共享 bigram（关键词路不中），余弦 0.9 ≥ COS_MIN，纯向量触发。
    assert not (tokens_of("部署服务时") & tokens_of("把新版本推上机器"))
    fired = check_triggers(db, U, "把新版本推上机器", embedder=emb)
    assert any(r.condition_text == "部署服务时" for r in fired)
    # 向量不中：任务向量与条件向量余弦 0，且关键词无交集。
    fired = check_triggers(db, U, "打印月度报表给财务", embedder=emb)
    assert not any(r.condition_text == "部署服务时" for r in fired)
