"""修复第 2 轮：内容级遗忘墓碑——遗忘后重发/重提炼不复活。

口径（见 fix2-design-draft.md）：无条件内容哈希禁写，不用时间门——
时间门修不了审计自己的复现 3（新幂等键重发建新 job，时间门会放行）。
旧任务整单丢弃只走用户遗忘水位（创建于最近一次删除之前的任务）。
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.episodes import _apply_insight, _Cand, run_episode_once, submit_episode
from app.extract import Candidate
from app.tombstone import content_hash
from app.worker import _apply, _extract_and_write
from tests.conftest import consent

U = UserContext(app_id=settings.app_id, user_id="alice")


def _fact(db, subject, content):
    row = m.MemoryFact(app_id=U.app_id, user_id="alice", subject=subject, content=content)
    db.add(row)
    db.commit()
    return row.id


def _job(db, key, created_at=None):
    job = m.MemoryJob(
        app_id=U.app_id, user_id="alice", idempotency_key=key, session_id="s", **({"created_at": created_at} if created_at else {})
    )
    db.add(job)
    db.commit()
    return db.get(m.MemoryJob, job.id)


def _forget(client, item_id):
    r = client.delete(f"/v1/me/memories/{item_id}", params={"user_id": "alice"})
    assert r.status_code == 204, r.text


def _live_facts(db):
    return list(db.scalars(select(m.MemoryFact).where(m.MemoryFact.user_id == "alice", m.MemoryFact.valid_to.is_(None))))


def _tombstone_audits(db):
    return list(db.scalars(select(m.AuditLog).where(m.AuditLog.action == "tombstone", m.AuditLog.decision == "blocked")))


class FakeExtractor:
    def __init__(self, items):
        self.items = items

    def extract(self, text):
        return list(self.items)


class FakeDistiller:
    def __init__(self, items):
        self.items = items

    def distill(self, text):
        return list(self.items)


def test_forget_fact_then_rewrite_blocked(client, db):
    fid = _fact(db, "咖啡", "喜欢咖啡")
    _forget(client, fid)
    assert _live_facts(db) == []
    decision, new_id = _apply(db, _job(db, "k1"), Candidate(kind="fact", subject="咖啡", content="喜欢咖啡", action="ADD"), None)
    db.commit()
    assert decision == "tombstoned" and new_id is None
    assert _live_facts(db) == []
    assert _tombstone_audits(db), "阻断须留审计"


def test_norm_variant_same_hash_blocked(client, db):
    fid = _fact(db, "咖啡", "喜欢 咖啡")
    _forget(client, fid)
    # norm 只折叠空白：多空格与单空格同一哈希，照样挡住
    # （注意：有空格与无空格是不同串，norm 不删空格，不在此列）。
    assert content_hash("咖啡", "喜欢   咖啡") == content_hash("咖啡", "喜欢 咖啡")
    decision, _ = _apply(db, _job(db, "k2"), Candidate(kind="fact", subject="咖啡", content="喜欢   咖啡", action="ADD"), None)
    db.commit()
    assert decision == "tombstoned"
    assert _live_facts(db) == []


def test_other_content_and_other_user_unaffected(client, db):
    fid = _fact(db, "咖啡", "喜欢咖啡")
    _forget(client, fid)
    decision, new_id = _apply(db, _job(db, "k3"), Candidate(kind="fact", subject="咖啡", content="讨厌咖啡", action="ADD"), None)
    db.commit()
    assert decision == "add" and new_id is not None
    other = m.MemoryJob(app_id=U.app_id, user_id="bob", idempotency_key="kb", session_id="s")
    db.add(other)
    db.commit()
    decision, _ = _apply(db, other, Candidate(kind="fact", subject="咖啡", content="喜欢咖啡", action="ADD"), None)
    db.commit()
    assert decision == "add", "墓碑按用户隔离，他人同内容不受影响"


def test_forget_preference_and_insight_blocked(client, db):
    pref = m.MemoryPreference(app_id=U.app_id, user_id="alice", key="lang", value="中文")
    db.add(pref)
    db.commit()
    _forget(client, pref.id)
    decision, _ = _apply(db, _job(db, "k4"), Candidate(kind="preference", subject="lang", content="中文", action="ADD"), None)
    db.commit()
    assert decision == "tombstoned"

    ins = m.MemoryInsight(app_id=U.app_id, user_id="alice", kind="lesson", subject="部署端口", content="先探测端口占用")
    db.add(ins)
    db.commit()
    _forget(client, ins.id)
    decision, _ = _apply_insight(db, U, _Cand("lesson", "部署端口", "先探测端口占用", ""), "ep-x", None)
    db.commit()
    assert decision == "tombstoned"
    assert db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.user_id == "alice", m.MemoryInsight.valid_to.is_(None))).all() == []


def test_old_job_discarded_by_watermark(client, db):
    consent(client, "alice")
    fid = _fact(db, "无关主题", "无关内容")
    past = datetime.now(UTC) - timedelta(days=1)
    job = _job(db, "old-job", created_at=past)
    db.add(
        m.SessionMessage(app_id=U.app_id, user_id="alice", session_id="s", job_id=job.id, msg_id="m1", role="user", content="今天天气不错")
    )
    db.commit()
    _forget(client, fid)  # 设水位：此后处理创建于水位之前的旧任务须整单丢弃
    decision, ids = _extract_and_write(
        db, job, FakeExtractor([Candidate(kind="fact", subject="天气", content="今天天气不错", action="ADD")])
    )
    assert decision == "skip" and ids == []
    assert _live_facts(db) == [], "旧任务整单丢弃，无关内容也不写"


def test_new_job_after_forget_writes_fine(client, db):
    consent(client, "alice")
    fid = _fact(db, "无关主题", "无关内容")
    _forget(client, fid)
    job = _job(db, "new-job")
    db.add(
        m.SessionMessage(app_id=U.app_id, user_id="alice", session_id="s", job_id=job.id, msg_id="m1", role="user", content="今天天气不错")
    )
    db.commit()
    decision, ids = _extract_and_write(
        db, job, FakeExtractor([Candidate(kind="fact", subject="天气", content="今天天气不错", action="ADD")])
    )
    db.commit()
    assert decision == "add" and len(ids) == 1


def test_redistill_same_episode_blocked(client, db):
    steps = [{"action": "在 8080 启动", "error": "EADDRINUSE"}, {"action": "改用 3000 启动", "observation": "成功"}]
    ep1 = submit_episode(db, U, source="test", title="部署服务", outcome="partial", steps=steps, cost={"tokens": 400})
    _forget(client, ep1.id)  # 轨迹此前不写任何墓碑，本轮补 id + 内容两份
    submit_episode(db, U, source="test", title="部署服务", outcome="partial", steps=steps, cost={"tokens": 400})
    cand = _Cand("pitfall", "部署端口选择", "部署前先探测端口占用", "部署时")
    out = run_episode_once(db, FakeDistiller([cand]))
    assert out is not None and out.distill_status == "done"
    assert db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.valid_to.is_(None))).all() == []
    assert db.scalars(select(m.AuditLog).where(m.AuditLog.action == "distill", m.AuditLog.decision == "tombstoned")).all()


def test_markdown_import_tombstoned(client, db):
    consent(client, "alice")  # 修复第 7 轮：导入入口过同意门，墓碑断言需先开启
    fid = _fact(db, "咖啡", "喜欢咖啡")
    _forget(client, fid)
    files = {"facts/kafei.md": "---\ntype: facts\nsubject: 咖啡\n---\n\n# 事实 · 咖啡\n\n- 喜欢咖啡\n"}
    r = client.post("/v1/me/markdown/import", params={"user_id": "alice"}, json={"files": files})
    assert r.status_code == 200, r.text
    assert r.json()["tombstoned"] >= 1 and r.json()["created"] == 0
    assert _live_facts(db) == []


def test_correct_and_patch_tombstoned_409(client, db):
    consent(client, "alice")  # 修复第 7 轮：纠正/PATCH 入口过同意门，墓碑断言需先开启
    fid = _fact(db, "咖啡", "喜欢咖啡")
    _forget(client, fid)
    other = _fact(db, "咖啡", "讨厌咖啡")
    r = client.patch(f"/v1/me/memories/{other}", params={"user_id": "alice"}, json={"user_id": "alice", "content": "喜欢咖啡"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "tombstoned"

    ins = m.MemoryInsight(app_id=U.app_id, user_id="alice", kind="lesson", subject="部署端口", content="先探测端口占用")
    db.add(ins)
    db.commit()
    _forget(client, ins.id)
    ins2 = m.MemoryInsight(app_id=U.app_id, user_id="alice", kind="lesson", subject="部署端口", content="其他做法")
    db.add(ins2)
    db.commit()
    r = client.patch(f"/v1/me/insights/{ins2.id}", params={"user_id": "alice"}, json={"content": "先探测端口占用"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "tombstoned"


def test_clear_all_then_rewrite_blocked(client, db):
    _fact(db, "咖啡", "喜欢咖啡")
    r = client.delete("/v1/me/memories", params={"user_id": "alice"})
    assert r.status_code == 204, r.text
    decision, _ = _apply(db, _job(db, "k5"), Candidate(kind="fact", subject="咖啡", content="喜欢咖啡", action="ADD"), None)
    db.commit()
    assert decision == "tombstoned"
    assert _live_facts(db) == []
