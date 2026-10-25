"""修复第 7 轮：同意门收口——纠正、Markdown 导回、经验 PATCH 过门，
MEMORY_AUTO_CONSENT 绝不覆盖已有行。Fake + 临时库，零联网。"""

from datetime import UTC, datetime

import pytest

pytest.importorskip("mcp")

import app.mcp_server as ms
from app import models as m
from app.config import settings
from app.schemas import CONSENT_VERSION
from tests.conftest import consent, uid

U_APP = settings.app_id


def _fact(db, subject="城市", content="住在北京"):
    row = m.MemoryFact(app_id=U_APP, user_id="alice", subject=subject, content=content)
    db.add(row)
    db.commit()
    return row


def _insight(db, subject="部署端口", content="先探测端口再部署"):
    row = m.MemoryInsight(app_id=U_APP, user_id="alice", kind="pitfall", subject=subject, content=content, applies_when="部署时")
    db.add(row)
    db.commit()
    return row


def _setting(db, enabled, version):
    row = m.UserSetting(app_id=U_APP, user_id="alice", memory_enabled=enabled, consent_version=version, consent_at=datetime.now(UTC))
    db.add(row)
    db.commit()
    return row


def _disable(client):
    client.put("/v1/me/settings", json={"user_id": "alice", "memory_enabled": False})


# ---------- correct ----------


def test_correct_blocked_when_disabled(client, db):
    consent(client, "alice")
    f = _fact(db)
    _disable(client)
    r = client.patch(f"/v1/me/memories/{f.id}", json={"user_id": "alice", "content": "住在成都"})
    assert r.status_code == 404
    db.expire_all()
    assert db.get(m.MemoryFact, f.id).valid_to is None
    assert db.query(m.MemoryFact).count() == 1  # 无新版本行


def test_correct_blocked_without_settings_row(client, db):
    f = _fact(db)  # alice 从未同意，无设置行
    r = client.patch(f"/v1/me/memories/{f.id}", json={"user_id": "alice", "content": "住在成都"})
    assert r.status_code == 404
    assert db.query(m.MemoryFact).count() == 1


def test_correct_blocked_when_consent_expired(client, db):
    _setting(db, True, "old-version")
    f = _fact(db)
    r = client.patch(f"/v1/me/memories/{f.id}", json={"user_id": "alice", "content": "住在成都"})
    assert r.status_code == 404
    assert db.query(m.MemoryFact).count() == 1


def test_correct_works_when_enabled(client, db):
    consent(client, "alice")
    f = _fact(db)
    r = client.patch(f"/v1/me/memories/{f.id}", json={"user_id": "alice", "content": "住在成都"})
    assert r.status_code == 200 and r.json()["version"] == 2


def test_correct_gate_precedes_tombstone_409(client, db):
    # 门在墓碑查之前：关闭用户即使新内容命中墓碑，也只看到 404，不泄露行存在性
    consent(client, "alice")
    f = _fact(db, "咖啡", "喜欢咖啡")
    client.delete(f"/v1/me/memories/{f.id}", params=uid("alice"))
    other = _fact(db, "咖啡", "讨厌咖啡")
    _disable(client)
    r = client.patch(f"/v1/me/memories/{other.id}", params={"user_id": "alice"}, json={"user_id": "alice", "content": "喜欢咖啡"})
    assert r.status_code == 404


# ---------- patch_insight ----------


def test_patch_insight_blocked_when_disabled(client, db):
    consent(client, "alice")
    row = _insight(db)
    _disable(client)
    r = client.patch(f"/v1/me/insights/{row.id}", params=uid("alice"), json={"content": "改完的新指导", "pinned": True})
    assert r.status_code == 404
    db.expire_all()
    row = db.get(m.MemoryInsight, row.id)
    assert row.content == "先探测端口再部署" and row.pinned is False


def test_patch_insight_blocked_without_settings_row(client, db):
    row = _insight(db)
    r = client.patch(f"/v1/me/insights/{row.id}", params=uid("alice"), json={"muted": True})
    assert r.status_code == 404
    db.expire_all()
    assert db.get(m.MemoryInsight, row.id).muted is False


def test_patch_insight_blocked_when_consent_expired(client, db):
    _setting(db, True, "old-version")
    row = _insight(db)
    r = client.patch(f"/v1/me/insights/{row.id}", params=uid("alice"), json={"muted": True})
    assert r.status_code == 404


def test_patch_insight_works_when_enabled(client, db):
    consent(client, "alice")
    row = _insight(db)
    r = client.patch(f"/v1/me/insights/{row.id}", params=uid("alice"), json={"content": "改完的新指导"})
    assert r.status_code == 200 and r.json()["content"] == "改完的新指导"


# ---------- markdown import ----------

_NEW_FACT_FILE = {"facts/new.md": "---\ntype: facts\nsubject: 新主题\n---\n\n# 事实 · 新主题\n\n- 新内容\n"}


def test_markdown_import_blocked_when_disabled(client, db):
    consent(client, "alice")
    _disable(client)
    r = client.post("/v1/me/markdown/import", params={"user_id": "alice"}, json={"files": _NEW_FACT_FILE})
    assert r.status_code == 404
    assert db.query(m.MemoryFact).count() == 0  # 零新建行


def test_markdown_import_blocked_without_settings_row(client, db):
    r = client.post("/v1/me/markdown/import", params={"user_id": "alice"}, json={"files": _NEW_FACT_FILE})
    assert r.status_code == 404
    assert db.query(m.MemoryFact).count() == 0


def test_markdown_import_blocked_when_consent_expired(client, db):
    _setting(db, True, "old-version")
    r = client.post("/v1/me/markdown/import", params={"user_id": "alice"}, json={"files": _NEW_FACT_FILE})
    assert r.status_code == 404
    assert db.query(m.MemoryFact).count() == 0


def test_markdown_import_works_when_enabled(client, db):
    consent(client, "alice")
    r = client.post("/v1/me/markdown/import", params={"user_id": "alice"}, json={"files": _NEW_FACT_FILE})
    assert r.status_code == 200 and r.json()["created"] >= 1


# ---------- AUTO_CONSENT 绝不覆盖已有行 ----------


def test_auto_consent_never_reenables_opted_out_user(client, db, monkeypatch):
    consent(client, "alice")
    _disable(client)
    monkeypatch.setenv("MEMORY_USER_ID", "alice")
    monkeypatch.setenv("MEMORY_AUTO_CONSENT", "1")
    out = ms.memory_write("关闭后 MCP 也不应重开")
    assert out["skipped"] is True and out["job_id"] is None
    db.expire_all()
    from sqlalchemy import select

    row = db.scalar(select(m.UserSetting).where(m.UserSetting.user_id == "alice"))
    assert row is not None and row.memory_enabled is False


def test_auto_consent_never_refreshes_expired_consent(client, db, monkeypatch):
    before = _setting(db, True, "old-version")
    old_at = before.consent_at
    monkeypatch.setenv("MEMORY_USER_ID", "alice")
    monkeypatch.setenv("MEMORY_AUTO_CONSENT", "1")
    out = ms.memory_write("版本过期也不应被静默刷掉")
    assert out["skipped"] is True and out["job_id"] is None
    db.expire_all()
    from sqlalchemy import select

    row = db.scalar(select(m.UserSetting).where(m.UserSetting.user_id == "alice"))
    # SQLite 存取会丢 tzinfo，只比墙钟值
    assert row.consent_version == "old-version"
    assert row.consent_at.replace(tzinfo=None) == old_at.replace(tzinfo=None)


def test_auto_consent_still_creates_row_for_new_user(client, db, monkeypatch):
    monkeypatch.setenv("MEMORY_USER_ID", "carol")
    monkeypatch.setenv("MEMORY_AUTO_CONSENT", "1")
    out = ms.memory_write("第一次使用就自动开启")
    assert out["skipped"] is False and out["job_id"]
    db.expire_all()
    from sqlalchemy import select

    row = db.scalar(select(m.UserSetting).where(m.UserSetting.user_id == "carol"))
    assert row is not None and row.memory_enabled is True
    assert row.consent_version == CONSENT_VERSION


# ---------- 删除与开关本身不受门影响 ----------


def test_delete_and_settings_still_work_when_disabled(client, db):
    consent(client, "alice")
    f = _fact(db)
    _disable(client)
    assert client.delete(f"/v1/me/memories/{f.id}", params=uid("alice")).status_code == 204
    r = client.put("/v1/me/settings", json={"user_id": "alice", "memory_enabled": True, "consent_version": CONSENT_VERSION})
    assert r.status_code == 200 and r.json()["memory_enabled"] is True
    # 重新开启后三入口恢复
    f2 = _fact(db, "城市2", "住在上海")
    assert client.patch(f"/v1/me/memories/{f2.id}", json={"user_id": "alice", "content": "住在杭州"}).status_code == 200
