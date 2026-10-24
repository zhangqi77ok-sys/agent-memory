"""第十九轮：前端落地之外的后端收口——证据链、经验语义合并、提醒排序去重、
agent 维度、管理端审计。

每一项都对应深度反思里点名的缺口；每一项都有「旧行为不变」的对照：
没报步号的提炼仍走全文校验；同主题合并口径不动；没命中时提醒列表为空。
"""
from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.distill import InsightCandidate
from app.episodes import consolidate_insights, distill_episode, submit_episode
from app.reminders import check_triggers, create_reminder

U = UserContext(app_id=settings.app_id, user_id="alice")
APP = settings.app_id
STEPS = [
    {"action": "拉取最新代码并安装依赖，一切正常"},
    {"action": "在 8080 启动服务", "error": "OSError: Address already in use"},
    {"action": "改用 ss -tlnp 全量列出监听端口，发现占用"},
]


class OneCand:
    def __init__(self, cand):
        self._c = cand

    def distill(self, rendered: str):
        return [self._c]


def _distill_one(db, cand, source="codex"):
    ep = submit_episode(db, U, source=source, title="部署服务",
                        outcome="partial", steps=STEPS, cost={})
    distill_episode(db, ep, OneCand(cand))
    rows = list(db.scalars(select(m.MemoryInsight).where(
        m.MemoryInsight.valid_to.is_(None))))
    assert len(rows) == 1, rows
    return rows[0]


# ---------- 1. 证据链 ----------

def test_evidence_chain_right_step_verifies_and_chains(db):
    row = _distill_one(db, InsightCandidate(
        kind="pitfall", subject="部署端口选择", content="先探测端口再启动",
        applies_when="部署时", evidence="在 8080 启动服务", evidence_step=2))
    assert row.verified is True
    assert row.evidence_chained is True


def test_evidence_chain_wrong_step_fails_verification(db):
    # 引文是第 2 步的，却报成第 1 步——假引用，不再靠全文子串蒙混。
    row = _distill_one(db, InsightCandidate(
        kind="pitfall", subject="部署端口选择", content="先探测端口再启动",
        applies_when="部署时", evidence="在 8080 启动服务", evidence_step=1))
    assert row.verified is False
    assert row.evidence_chained is False


def test_evidence_chain_no_step_falls_back_to_whole_text(db):
    # 旧提炼器不报步号：全文校验照旧，只是不给链标记。
    row = _distill_one(db, InsightCandidate(
        kind="pitfall", subject="部署端口选择", content="先探测端口再启动",
        applies_when="部署时", evidence="改用 ss -tlnp 全量列出监听端口"))
    assert row.verified is True
    assert row.evidence_chained is False


def test_insight_records_agent_dimension(db):
    row = _distill_one(db, InsightCandidate(
        kind="lesson", subject="部署端口选择", content="先探测端口再启动",
        applies_when="部署时", evidence="在 8080 启动服务", evidence_step=2),
        source="codex")
    assert row.agent == "codex"


# ---------- 2. 跨主题语义合并 ----------

def test_cross_subject_semantic_merge(db):
    a = m.MemoryInsight(app_id=APP, user_id="alice", kind="pitfall",
                         subject="部署端口选择", content="先探测端口占用再启动",
                         embedding=[1.0, 0.0], embed_model="fake-1")
    b = m.MemoryInsight(app_id=APP, user_id="alice", kind="pitfall",
                         subject="服务启动端口", content="先探测端口占用再启动",
                         embedding=[0.999, 0.045], embed_model="fake-1")
    db.add_all([a, b])
    db.commit()
    out = consolidate_insights(db)
    assert out["merged"] == 1
    alive = list(db.scalars(select(m.MemoryInsight).where(
        m.MemoryInsight.valid_to.is_(None))))
    assert len(alive) == 1
    assert "合并自同类经验" in alive[0].content  # 败者表述留痕（第十八轮）


def test_cross_subject_far_vectors_do_not_merge(db):
    a = m.MemoryInsight(app_id=APP, user_id="alice", kind="pitfall",
                         subject="部署端口选择", content="先探测端口占用再启动",
                         embedding=[1.0, 0.0], embed_model="fake-1")
    b = m.MemoryInsight(app_id=APP, user_id="alice", kind="pitfall",
                         subject="服务启动端口", content="备份数据库再上线",
                         embedding=[0.0, 1.0], embed_model="fake-1")
    db.add_all([a, b])
    db.commit()
    out = consolidate_insights(db)
    assert out["merged"] == 0


# ---------- 3. 提醒排序去重 ----------

def test_reminder_sorted_by_score_and_deduped_by_action(db):
    create_reminder(db, U, condition="部署服务", action="先检查端口占用")
    create_reminder(db, U, condition="部署服务到生产", action="上线前先备份")
    create_reminder(db, U, condition="部署服务", action="先检查端口占用")  # 同动作换个武装
    fired = check_triggers(db, U, "部署服务")
    assert [r.action_text for r in fired] == ["先检查端口占用", "上线前先备份"]
    # 被去重掉的那条不记账
    rows = list(db.scalars(select(m.MemoryReminder).order_by(m.MemoryReminder.created_at)))
    assert [r.fire_count for r in rows] == [1, 1, 0]


# ---------- 4. 管理端审计 ----------

def test_admin_audit_records_views_but_not_audit_reads(client, db, monkeypatch):
    monkeypatch.setattr(settings, "admin_keys", "test-admin-key")
    assert client.get("/v1/admin/overview",
                      headers={"X-Admin-Key": "test-admin-key"}).status_code == 200
    assert client.get("/v1/admin/users/alice/memories",
                      headers={"X-Admin-Key": "test-admin-key"}).status_code == 200
    r = client.get("/v1/admin/audit", headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert [(e["endpoint"], e["target_user"]) for e in entries] == [
        ("user_memories", "alice"), ("overview", None)]
    assert all(len(e["admin_key_hash"]) == 12 for e in entries)
    # 查审计本身不记审计：再查一次，条数不变
    r2 = client.get("/v1/admin/audit", headers={"X-Admin-Key": "test-admin-key"})
    assert len(r2.json()["entries"]) == 2
