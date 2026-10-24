"""第二十轮规模：PG 锚点探针测试。

- ordered_tokens：与 tokens_of 同集合，按查询词序返回（去重）。
- 锚点快路径（仅 PG）：采样 + LATERAL 验数，命中数精确，找够 CAP 即停。
- 回落（仅 PG）：找不够 CAP 时 _anchor_probe 返回 None，简报走精确探针，
  结果与 SQLite 精确路径一致。
- 简报级测试不分方言：SQLite 走精确探针，PG 走锚点→回落→精确探针，
  胜出集合相同。
"""

import pytest
from sqlalchemy import or_

import app.recall as recall
from app import models as m
from app.auth import UserContext
from app.config import settings
from app.episodes import brief_for_task
from app.tokenize import ordered_tokens, tokens_of

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")
QUERY = "部署服务要绑定端口"


def _insight(db, subject, content, applies=""):
    row = m.MemoryInsight(app_id=APP, user_id="alice", kind="strategy",
                          subject=subject, content=content, applies_when=applies)
    db.add(row)
    db.flush()
    return row


def _fixture(db):
    """4 条命中（部署/服务/端口各 3 个查询 token）+ 2 条无关。"""
    matching = [
        _insight(db, "部署端口选择", "部署前先探测端口占用再启动服务", "部署服务时"),
        _insight(db, "部署端口排查", "服务启动失败先查端口是否被占用", "部署服务时"),
        _insight(db, "端口冲突处理", "部署多个服务时端口不能重复"),
        _insight(db, "服务部署清单", "部署服务前确认端口与依赖"),
    ]
    others = [
        _insight(db, "日志保留策略", "日志按日轮转保留七天避免磁盘写满"),
        _insight(db, "缓存过期策略", "缓存键设 TTL 到期自动失效"),
    ]
    db.commit()
    return matching, others


def _cols_scope():
    cols = [m.MemoryInsight.id, m.MemoryInsight.kind, m.MemoryInsight.subject,
            m.MemoryInsight.content, m.MemoryInsight.applies_when,
            m.MemoryInsight.confidence, m.MemoryInsight.project,
            m.MemoryInsight.pinned, m.MemoryInsight.valid_from,
            m.MemoryInsight.last_used_at, m.MemoryInsight.source_episode_id,
            m.MemoryInsight.verified, m.MemoryInsight.agent,
            m.MemoryInsight.evidence_chained]
    scope = (m.MemoryInsight.app_id == APP, m.MemoryInsight.user_id == "alice",
             m.MemoryInsight.valid_to.is_(None),
             or_(m.MemoryInsight.muted.is_(False), m.MemoryInsight.muted.is_(None)))
    return cols, scope


def _pg_only(db):
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("anchor probe is PG-only")


def test_ordered_tokens_same_set_query_order():
    assert ordered_tokens(QUERY) == [
        "部署", "署服", "服务", "务要", "要绑", "绑定", "定端", "端口"]
    assert set(ordered_tokens(QUERY)) == tokens_of(QUERY)
    assert ordered_tokens("cache 缓存 cache") == ["cache", "缓存"]
    assert ordered_tokens("a b a") == ["a", "b"]


def test_anchor_probe_fast_path_pg(db, monkeypatch):
    _pg_only(db)
    matching, _ = _fixture(db)
    monkeypatch.setattr(recall, "_ANCHOR_CAP", 3)
    cols, scope = _cols_scope()
    plan = recall._QueryPlan(QUERY)
    rows = recall._anchor_probe(db, m.MemoryInsight, m.MemoryInsightToken,
                                "insight_id", cols, scope, plan)
    assert rows is not None and len(rows) == 3
    matching_ids = {r.id for r in matching}
    for r in rows:
        assert r.id in matching_ids
        # 命中数精确：行 token 集 ∩ 查询 token 集
        scoring = f"{r.subject} {r.content} {r.applies_when}"
        assert r.kw_hits == len(tokens_of(scoring) & plan.tokens)


def test_anchor_probe_fallback_pg(db, monkeypatch):
    _pg_only(db)
    matching, _ = _fixture(db)
    monkeypatch.setattr(recall, "_ANCHOR_CAP", 100)
    cols, scope = _cols_scope()
    plan = recall._QueryPlan(QUERY)
    assert recall._anchor_probe(db, m.MemoryInsight, m.MemoryInsightToken,
                                "insight_id", cols, scope, plan) is None
    # 回落精确探针后简报仍正确：4 条命中全胜出，无关条目不入选
    out = brief_for_task(db, U, QUERY, 5)
    assert {o.subject for o in out} == {r.subject for r in matching}


def test_brief_small_fixture_both_dialects(db):
    matching, _ = _fixture(db)
    out = brief_for_task(db, U, QUERY, 5)
    assert {o.subject for o in out} == {r.subject for r in matching}
