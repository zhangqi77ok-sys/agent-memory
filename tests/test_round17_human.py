"""第十七轮：类人记忆补全 + 前端同步 + 管理端。

审查剩余四块：前瞻记忆（记得将来要做的事）、编码显著性（重要的先复盘）、
事实/偏好置信度（被佐证的程度要算数）、轨迹骨架化（细节让位给要点）。
外加用户两条新规：每轮更新同步前端（本轮落在 Vue 面板与星图，构建与前端单测
另行验证）；要有管理端页面能看所有人的记忆（后端 /v1/admin 只读接口在此验）。
"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.episodes import (
    claim_episode,
    episode_salience,
    skeletonize_episodes,
    submit_episode,
)
from app.recall import recall_facts
from app.reminders import check_triggers, create_reminder, dismiss_reminder, list_reminders
from app.worker import _apply_fact, _apply_preference, support_confidence
from tests.conftest import consent

U = UserContext(app_id=settings.app_id, user_id="alice")
BOB = UserContext(app_id=settings.app_id, user_id="bob")
NOW = m._now()


def _job(db, user="alice"):
    import uuid

    job = m.MemoryJob(app_id=settings.app_id, user_id=user, session_id="s1",
                      idempotency_key=f"k-{uuid.uuid4().hex[:12]}")
    db.add(job)
    db.commit()
    return job


# ---------- 编码显著性 ----------

def test_salience_ordering_and_cost_weight():
    assert episode_salience("failure", {}) > episode_salience("partial", {})
    assert episode_salience("partial", {}) > episode_salience("success", {})
    assert episode_salience("success", {"tokens": 20000}) > episode_salience("success", {})
    assert episode_salience("success", {"retries": 2}) > episode_salience("success", {})


def test_claim_episode_prefers_high_salience(db):
    submit_episode(db, U, source="t", title="顺利的小任务", outcome="success",
                   steps=[{"action": "做完"}], cost={})
    submit_episode(db, U, source="t", title="翻车的大任务", outcome="failure",
                   steps=[{"action": "翻车"}], cost={"tokens": 30000})
    first = claim_episode(db)
    assert first is not None and first.title == "翻车的大任务"
    assert first.salience > 3.0


# ---------- 事实/偏好置信度 ----------

def test_support_confidence_curve():
    assert support_confidence(1) == 0.5
    assert support_confidence(2) == pytest.approx(2 / 3)
    assert support_confidence(3) == 0.75


def test_fact_corroboration_bumps_support(db):
    job = _job(db)
    assert _apply_fact(db, job, "咖啡", "喜欢拿铁", "ADD", None)[0] == "add"
    assert _apply_fact(db, job, "咖啡", "喜欢拿铁", "ADD", None)[0] == "noop"
    assert _apply_fact(db, job, "咖啡", "喜欢拿铁", "ADD", None)[0] == "noop"
    db.commit()
    rows = list(db.scalars(select(m.MemoryFact).where(m.MemoryFact.valid_to.is_(None))))
    assert len(rows) == 1  # 佐证不产生新版本
    assert rows[0].support_count == 3 and rows[0].confidence == 0.75


def test_fact_update_resets_support(db):
    job = _job(db)
    _apply_fact(db, job, "咖啡", "喜欢拿铁", "ADD", None)
    _apply_fact(db, job, "咖啡", "喜欢拿铁", "ADD", None)
    assert _apply_fact(db, job, "咖啡", "改喝美式", "UPDATE", None)[0] == "update"
    db.commit()
    row = db.scalars(select(m.MemoryFact).where(m.MemoryFact.valid_to.is_(None))).first()
    assert row.content == "改喝美式" and row.support_count == 1 and row.confidence == 0.5


def test_preference_corroboration_bumps_support(db):
    job = _job(db)
    assert _apply_preference(db, job, "咖啡", "拿铁", "ADD")[0] == "add"
    assert _apply_preference(db, job, "咖啡", "拿铁", "ADD")[0] == "noop"
    db.commit()
    row = db.scalars(select(m.MemoryPreference).where(
        m.MemoryPreference.valid_to.is_(None))).first()
    assert row.support_count == 2 and row.confidence == pytest.approx(2 / 3)


def test_recall_prefers_corroborated_fact(db):
    a = m.MemoryFact(app_id=U.app_id, user_id="alice", subject="部署甲",
                     content="部署时用甲方案处理", support_count=3, confidence=0.75)
    b = m.MemoryFact(app_id=U.app_id, user_id="alice", subject="部署乙",
                     content="部署时用乙方案处理", support_count=1, confidence=0.5)
    db.add_all([a, b])
    db.commit()
    out = recall_facts(db, U, "部署方案", 5, 1500)
    assert [o.fact_id for o in out] == [a.id, b.id]
    assert out[0].confidence == 0.75 and out[1].confidence == 0.5


# ---------- 前瞻记忆 ----------

def test_reminder_fires_once_on_keyword_match(db):
    r = create_reminder(db, U, condition="部署到生产环境时", action="先确认数据库备份完成")
    fired = check_triggers(db, U, "准备部署到生产环境，先看简报")
    assert [x.id for x in fired] == [r.id]
    db.expire_all()
    row = db.get(m.MemoryReminder, r.id)
    assert row.status == "fired" and row.fire_count == 1
    assert check_triggers(db, U, "准备部署到生产环境，先看简报") == []  # 一次性，不再触发


def test_reminder_recurring_stays_armed(db):
    r = create_reminder(db, U, condition="提交代码前", action="跑一遍全量测试", once=False)
    assert len(check_triggers(db, U, "提交代码前检查")) == 1
    assert len(check_triggers(db, U, "提交代码前检查")) == 1
    db.expire_all()
    row = db.get(m.MemoryReminder, r.id)
    assert row.status == "armed" and row.fire_count == 2


def test_reminder_no_match_and_project_gate(db):
    r = create_reminder(db, U, condition="部署到生产环境时", action="先备份",
                        project="repo-a")
    assert check_triggers(db, U, "写周报总结本周工作") == []
    assert check_triggers(db, U, "部署到生产环境", project="repo-b") == []  # 项目不符不撞
    assert len(check_triggers(db, U, "部署到生产环境", project="repo-a")) == 1
    assert db.get(m.MemoryReminder, r.id).status == "fired"


def test_reminder_dismiss_and_isolation(db):
    r = create_reminder(db, U, condition="出差前", action="订机票")
    dismiss_reminder(db, U, r.id)
    assert check_triggers(db, U, "出差前准备") == []
    assert list_reminders(db, U) == []
    with pytest.raises(ValueError):
        dismiss_reminder(db, BOB, r.id)  # 他人的意图动不了


def test_reminder_rest_and_brief_integration(client, db):
    consent(client, "alice")
    r = client.post("/v1/reminders", json={
        "user_id": "alice", "condition": "部署到生产环境时", "action": "先确认备份"})
    assert r.status_code == 201 and r.json()["status"] == "armed"
    r = client.get("/v1/reminders", params={"user_id": "alice"})
    assert len(r.json()) == 1
    r = client.post("/v1/brief", json={"user_id": "alice", "task": "部署到生产环境走一遍"})
    assert r.status_code == 200
    rems = r.json()["reminders"]
    assert len(rems) == 1 and rems[0]["action"] == "先确认备份" and rems[0]["status"] == "fired"


# ---------- 轨迹骨架化 ----------

def _old_done_episode(db, days=100, steps=6, status="done"):
    ep = submit_episode(db, U, source="t", title="长任务", outcome="success",
                        steps=[{"action": f"步骤{i}", "observation": f"观察细节{i}"}
                               for i in range(steps)], cost={})
    ep.distill_status = status
    ep.created_at = NOW - timedelta(days=days)
    if steps > 3:
        ep.steps[2] = {"action": "关键步骤", "error": "这里炸了"}
    db.commit()
    return ep


def test_skeletonize_old_done_episode(db):
    ep = _old_done_episode(db)
    assert skeletonize_episodes(db, now=NOW, retention_days=90) == 1
    db.expire_all()
    ep = db.get(m.TaskEpisode, ep.id)
    assert ep.skeletonized is True
    assert ep.steps[0].get("observation") == "观察细节0"      # 首步保全
    assert ep.steps[-1].get("observation") == "观察细节5"     # 尾步保全
    assert ep.steps[2].get("error") == "这里炸了"             # 出错步保全
    assert "observation" not in ep.steps[1]                   # 中间步只剩骨架
    assert ep.steps[1].get("skeletonized") is True
    assert skeletonize_episodes(db, now=NOW, retention_days=90) == 0  # 不重复处理


def test_skeletonize_skips_young_and_pending(db):
    _old_done_episode(db, days=10)
    _old_done_episode(db, days=100, status="pending")
    assert skeletonize_episodes(db, now=NOW, retention_days=90) == 0


# ---------- 管理端 ----------

def test_admin_disabled_without_keys(client, db, monkeypatch):
    monkeypatch.setattr(settings, "admin_keys", "")
    r = client.get("/v1/admin/overview", headers={"X-Admin-Key": "whatever"})
    assert r.status_code == 404  # 没配密钥 = 没有管理端


def test_admin_overview_and_user_memories(client, db, monkeypatch):
    monkeypatch.setattr(settings, "admin_keys", "test-admin-key")
    job = _job(db)
    _apply_fact(db, job, "咖啡", "喜欢拿铁", "ADD", None)
    job_b = _job(db, user="bob")
    _apply_fact(db, job_b, "城市", "住在上海", "ADD", None)
    db.commit()
    assert client.get("/v1/admin/overview").status_code == 403
    assert client.get("/v1/admin/overview",
                      headers={"X-Admin-Key": "wrong"}).status_code == 403
    r = client.get("/v1/admin/overview", headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200
    users = {u["user_id"]: u for u in r.json()["users"]}
    assert users["alice"]["facts"] == 1 and users["bob"]["facts"] == 1
    r = client.get("/v1/admin/users/alice/memories",
                   headers={"X-Admin-Key": "test-admin-key"})
    data = r.json()
    assert [f["content"] for f in data["facts"]] == ["喜欢拿铁"]  # 只看得到 alice 的
    assert data["facts"][0]["confidence"] == 0.5
