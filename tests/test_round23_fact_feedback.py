"""第二十三轮：事实级反馈 API——事实/偏好置信度重算。

缺口：经验有反馈闭环（第十轮），事实与偏好没有——直接 memory_write
的用户写下的事实，之后无法回报准不准，负信号无处可去。

公式（第二十三轮，worker.fact_confidence）：(S+P)/(S+P+N+1)，S = 佐证
计数、P = 好评、N = 差评。无反馈时恒等于旧佐证公式 S/(S+1)，旧行数值
不动；好评如一次新佐证，差评拉低；佐证路径（_bump_support）同步改用
此式，否则反馈后一次新佐证就把负反馈洗掉。
"""

import pytest

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.episodes import record_fact_feedback, record_preference_feedback
from app.worker import _bump_support, fact_confidence, support_confidence
from tests.conftest import consent

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")


def _fact(db, subject="咖啡", content="喜欢拿铁", support=1, confidence=0.5):
    row = m.MemoryFact(app_id=APP, user_id="alice", subject=subject, content=content,
                       support_count=support, confidence=confidence)
    db.add(row)
    db.flush()
    return row


def _pref(db, key="咖啡", value="拿铁", support=1, confidence=0.5):
    row = m.MemoryPreference(app_id=APP, user_id="alice", key=key, value=value,
                             support_count=support, confidence=confidence)
    db.add(row)
    db.flush()
    return row


def _reload(db, row):
    db.expire_all()
    return db.get(type(row), row.id)


# ---------- 公式 ----------

def test_fact_confidence_identity_with_support_confidence():
    # 无反馈时恒等于旧公式：旧行数值不动，第十七轮口径不受影响
    for s in (1, 2, 3, 5, 10):
        assert fact_confidence(s, 0, 0) == support_confidence(s)


# ---------- 服务层：事实 ----------

def test_fact_helpful_is_like_one_more_corroboration(db):
    row = _fact(db)
    db.commit()
    out = record_fact_feedback(db, U, row.id, helpful=True)
    assert out.feedback_pos == 1 and out.feedback_neg == 0
    assert out.confidence == pytest.approx(2 / 3)  # 0.5 → 0.667，与再被佐证一次同值
    db.commit()
    assert _reload(db, row).confidence == pytest.approx(2 / 3)


def test_fact_unhelpful_lowers_confidence(db):
    row = _fact(db)
    db.commit()
    out = record_fact_feedback(db, U, row.id, helpful=False)
    assert out.feedback_pos == 0 and out.feedback_neg == 1
    assert out.confidence == pytest.approx(1 / 3)  # 0.5 → 0.333


def test_fact_high_support_helpful(db):
    row = _fact(db, support=3, confidence=0.75)
    db.commit()
    out = record_fact_feedback(db, U, row.id, helpful=True)
    assert out.confidence == pytest.approx(4 / 5)  # (3+1)/(3+1+0+1) = 0.8


def test_fact_balanced_feedback_returns_to_neutral(db):
    row = _fact(db)
    db.commit()
    record_fact_feedback(db, U, row.id, helpful=True)
    out = record_fact_feedback(db, U, row.id, helpful=False)
    assert (out.feedback_pos, out.feedback_neg) == (1, 1)
    assert out.confidence == pytest.approx(0.5)


def test_fact_feedback_wrong_user_or_missing_raises(db):
    row = _fact(db)
    db.commit()
    other = UserContext(app_id=APP, user_id="mallory")
    with pytest.raises(ValueError):
        record_fact_feedback(db, other, row.id, helpful=True)
    with pytest.raises(ValueError):
        record_fact_feedback(db, U, "no-such-id", helpful=True)


def test_fact_feedback_audited(db):
    row = _fact(db)
    db.commit()
    record_fact_feedback(db, U, row.id, helpful=True)
    db.commit()
    logs = db.query(m.AuditLog).filter(
        m.AuditLog.user_id == "alice", m.AuditLog.action == "feedback").all()
    assert any(row.id in (log.fact_ids or []) for log in logs)


# ---------- 服务层：偏好 ----------

def test_preference_feedback(db):
    row = _pref(db)
    db.commit()
    out = record_preference_feedback(db, U, row.id, helpful=False)
    assert out.feedback_neg == 1 and out.confidence == pytest.approx(1 / 3)
    out = record_preference_feedback(db, U, row.id, helpful=True)
    assert out.confidence == pytest.approx(0.5)


# ---------- 佐证路径保留反馈 ----------

def test_bump_support_preserves_feedback(db):
    # 负反馈后又被佐证：旧算法会给 0.667（洗掉差评），新算法给 0.5
    row = _fact(db)
    db.commit()
    record_fact_feedback(db, U, row.id, helpful=False)
    db.commit()
    row = _reload(db, row)
    _bump_support([row], "喜欢拿铁", "content")
    assert row.support_count == 2
    assert row.confidence == pytest.approx(2 / (2 + 0 + 1 + 1))  # 0.5，不是 0.667


def test_bump_support_unchanged_without_feedback(db):
    row = _fact(db)
    db.commit()
    _bump_support([row], "喜欢拿铁", "content")
    assert row.support_count == 2
    assert row.confidence == pytest.approx(2 / 3)


# ---------- REST ----------

def test_rest_fact_feedback_and_list_counts(client, db):
    consent(client, "alice")
    row = _fact(db)
    db.commit()
    r = client.post("/v1/facts/feedback",
                    json={"user_id": "alice", "fact_id": row.id, "helpful": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["confidence"] == pytest.approx(2 / 3, abs=1e-4)
    assert (body["support_count"], body["feedback_pos"], body["feedback_neg"]) == (1, 1, 0)
    listed = client.get("/v1/me/memories", params={"user_id": "alice", "kind": "fact"})
    item = [i for i in listed.json()["items"] if i["id"] == row.id][0]
    assert (item["feedback_pos"], item["feedback_neg"]) == (1, 0)
    assert item["confidence"] == pytest.approx(2 / 3, abs=1e-4)


def test_rest_fact_feedback_404s(client, db):
    consent(client, "alice")
    row = _fact(db)
    db.commit()
    # 跨用户与不存在同形 404，不泄露存在性
    r = client.post("/v1/facts/feedback",
                    json={"user_id": "mallory", "fact_id": row.id, "helpful": True})
    assert r.status_code == 404
    r = client.post("/v1/facts/feedback",
                    json={"user_id": "alice", "fact_id": "no-such-id", "helpful": True})
    assert r.status_code == 404
    # 未同意用户同样 404（与经验反馈同形）
    r = client.post("/v1/facts/feedback",
                    json={"user_id": "bob", "fact_id": row.id, "helpful": True})
    assert r.status_code == 404


def test_rest_preference_feedback(client, db):
    consent(client, "alice")
    row = _pref(db)
    db.commit()
    r = client.post("/v1/preferences/feedback",
                    json={"user_id": "alice", "preference_id": row.id, "helpful": False})
    assert r.status_code == 200, r.text
    assert r.json()["confidence"] == pytest.approx(1 / 3, abs=1e-4)
    r = client.post("/v1/preferences/feedback",
                    json={"user_id": "alice", "preference_id": "no-such-id", "helpful": True})
    assert r.status_code == 404


# ---------- MCP ----------

def test_mcp_fact_feedback(client, db, monkeypatch):
    pytest.importorskip("mcp")
    import app.mcp_server as ms

    monkeypatch.setenv("MEMORY_USER_ID", "alice")
    consent(client, "alice")
    row = _fact(db)
    db.commit()
    out = ms.memory_fact_feedback(row.id, True)
    assert out["ok"] is True and out["confidence"] == pytest.approx(2 / 3, abs=1e-4)
    assert out["feedback_pos"] == 1
    assert ms.memory_fact_feedback("no-such-id", True)["error"] == "not_found"
    monkeypatch.setenv("MEMORY_USER_ID", "bob")  # bob 从未同意
    assert ms.memory_fact_feedback(row.id, True)["error"] == "memory_disabled"
