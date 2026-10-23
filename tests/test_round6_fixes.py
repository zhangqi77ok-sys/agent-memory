"""第六轮（保数据）回归：细节评审 P1 四条——claim 同构、提交幂等、候选限长、same_subject 收紧。

每条都对应评审里实测可复现的问题：
- claim_episode 旧版只 attempts+1 不改状态，同一条 episode 能被连续领两次；
- episode 提交（REST/MCP）没有任何幂等，客户端重试一次 = 重复落库 + 重复提炼；
- parse 层不限 subject 长度，超长主题在 SQLite 测不出、到 PG 的 String(128) 列整批爆；
- same_subject 包含合并误伤 Java/JavaScript 等不同的话题，UPDATE 会误杀旧值。
"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.distill import parse_insights
from app.episodes import claim_episode, submit_episode
from app.extract import parse_items, same_subject
from app.models import _now
from tests.conftest import consent

U = UserContext(app_id=settings.app_id, user_id="alice")


def _episode(db, title="部署服务", **kw):
    return submit_episode(db, U, source="test", title=title, outcome="success",
                          steps=[{"action": "启动"}], cost={}, **kw)


# ---------- #4 same_subject：误合并要拦住，真合并要保留 ----------

@pytest.mark.parametrize("a,b", [
    ("Java", "JavaScript"),   # 驼峰拼接的新专名
    ("Go", "Google"),          # 短侧只有 2 个 ASCII 字符
    ("C", "C++"),              # 1 个字符
    ("咖啡", "咖啡因过敏"),     # 多出的「因过敏」不是通用修饰
    ("AI", "AI工具"),          # 短侧太短，且「工具」不在通用修饰里
])
def test_same_subject_no_longer_overmerges(a, b):
    assert not same_subject(a, b)
    assert not same_subject(b, a)


@pytest.mark.parametrize("a,b", [
    ("咖啡", "咖啡偏好"),
    ("居住城市", "城市"),
    ("喝咖啡的习惯", "咖啡"),
    ("Lang", "language"),
    ("端口探测", "端口探测方法"),
])
def test_same_subject_still_merges_variants(a, b):
    assert same_subject(a, b)
    assert same_subject(b, a)


# ---------- #6 限长：超长 subject 单条丢弃，不连累整批 ----------

def test_parse_items_drops_overlong_subject_only():
    raw = ('{"items":['
           '{"kind":"fact","subject":"' + "主" * 200 + '","content":"x","action":"ADD"},'
           '{"kind":"fact","subject":"咖啡","content":"喜欢拿铁","action":"ADD"}]}')
    out = parse_items(raw)
    assert [c.subject for c in out] == ["咖啡"]


def test_parse_insights_drops_overlong_subject_only():
    out = parse_insights({"items": [
        {"kind": "lesson", "subject": "主" * 200, "content": "x", "applies_when": "y"},
        {"kind": "lesson", "subject": "备份", "content": "先备份再升级", "applies_when": "升级时"},
    ]})
    assert [c.subject for c in out] == ["备份"]


# ---------- #3 claim：真领取、不可重复领、过期可重领、上限不领 ----------

def test_claim_marks_running_and_cannot_be_claimed_twice(db):
    ep = _episode(db)
    first = claim_episode(db)
    assert first is not None and first.id == ep.id
    assert first.distill_status == "running" and first.locked_until is not None
    assert claim_episode(db) is None  # 旧版在这里会把同一条再领一次


def test_claim_reclaims_after_lock_expires(db):
    ep = _episode(db)
    claim_episode(db)
    row = db.get(m.TaskEpisode, ep.id)
    row.locked_until = _now() - timedelta(seconds=1)  # 模拟领取者崩溃、锁已过期
    db.commit()
    again = claim_episode(db)
    assert again is not None and again.id == ep.id and again.attempts == 2


def test_claim_skips_episode_at_attempt_cap(db):
    ep = _episode(db)
    row = db.get(m.TaskEpisode, ep.id)
    row.attempts = 3
    db.commit()
    assert claim_episode(db) is None


# ---------- #5 幂等：同键只落一次 ----------

def test_submit_episode_idempotent_by_key(db):
    a = _episode(db, idempotency_key="retry-1")
    b = _episode(db, idempotency_key="retry-1")
    assert a.id == b.id
    assert len(db.scalars(select(m.TaskEpisode)).all()) == 1
    c = _episode(db, idempotency_key="retry-2")
    assert c.id != a.id
    d = _episode(db)  # 不带键：不防重，照常落库
    e = _episode(db)
    assert d.id != e.id


def test_rest_episode_submit_idempotency_header(client, db):
    consent(client, "alice")
    body = {"user_id": "alice", "title": "部署", "outcome": "success", "steps": [], "cost": {}}
    r1 = client.post("/v1/episodes", json=body, headers={"Idempotency-Key": "rest-1"})
    r2 = client.post("/v1/episodes", json=body, headers={"Idempotency-Key": "rest-1"})
    assert r1.status_code == 202 and r1.json()["duplicate"] is False
    assert r2.status_code == 202 and r2.json()["duplicate"] is True
    assert r1.json()["episode_id"] == r2.json()["episode_id"]
    assert len(db.scalars(select(m.TaskEpisode)).all()) == 1


def test_mcp_episode_submit_fingerprint_dedup(client, db, monkeypatch):
    pytest.importorskip("mcp")
    import app.mcp_server as ms

    monkeypatch.setenv("MEMORY_USER_ID", "alice")
    monkeypatch.setenv("MEMORY_AUTO_CONSENT", "1")
    kw = dict(title="部署服务", outcome="partial",
              steps=[{"action": "在 8080 启动", "error": "EADDRINUSE"}], tokens=400)
    first = ms.memory_episode_submit(**kw)
    second = ms.memory_episode_submit(**kw)  # 同内容重试：指纹相同
    assert first["duplicate"] is False and second["duplicate"] is True
    assert first["episode_id"] == second["episode_id"]
    third = ms.memory_episode_submit(**{**kw, "title": "另一个任务"})
    assert third["duplicate"] is False and third["episode_id"] != first["episode_id"]
