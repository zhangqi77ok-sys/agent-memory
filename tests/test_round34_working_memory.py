"""第 34 轮「工作记忆」：会话级短期缓冲——刚递出的经验还热着，
名额未满时以 reason="working_memory" 补位；容量有界、复用提升、
删除与开关穿透、存量变化不端旧内容、成本 O(CAP)。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import event, insert, select

from app import models as m
from app import working_memory as wm
from app.auth import UserContext
from app.config import settings
from app.episodes import brief_for_task
from app.schemas import CONSENT_VERSION
from tests.conftest import consent

U = UserContext(app_id=settings.app_id, user_id="alice")
OTHER = UserContext(app_id=settings.app_id, user_id="bob")
SESS = "sess-34"


def _insight(db, subject, content, project=None, user=U):
    row = m.MemoryInsight(
        app_id=user.app_id, user_id=user.user_id, kind="strategy", subject=subject, content=content, applies_when="", project=project
    )
    db.add(row)
    db.commit()
    return row


def _brief(db, task, k=5, session=SESS, user=U, project=None):
    return brief_for_task(db, user, task, k, project=project, session_id=session)


def _entries(db, session=SESS, user=U):
    return list(
        db.scalars(
            select(m.MemoryWorkingEntry).where(
                m.MemoryWorkingEntry.app_id == user.app_id,
                m.MemoryWorkingEntry.user_id == user.user_id,
                m.MemoryWorkingEntry.session_id == session,
            )
        )
    )


def _now():
    return datetime.now(timezone.utc)


def test_rehearsal_bump(db):
    """会话内复用提升：第二次再递出首轮行，rehearsal_count 0→1。"""
    x = _insight(db, "部署前备份数据库", "上线前先对生产库做全量备份并验证备份可恢复")
    out1 = _brief(db, "部署前要做什么")
    assert [i.insight_id for i in out1] == [x.id]
    entries = _entries(db)
    assert len(entries) == 1 and entries[0].rehearsal_count == 0
    out2 = _brief(db, "部署前检查什么")
    assert x.id in [i.insight_id for i in out2]
    entries = _entries(db)
    assert len(entries) == 1 and entries[0].rehearsal_count == 1


def test_buffer_fill_reason(db):
    """无匹配任务时缓冲补位，reason 可区分。"""
    x = _insight(db, "部署前备份数据库", "上线前先对生产库做全量备份并验证备份可恢复")
    assert [i.insight_id for i in _brief(db, "部署前要做什么")] == [x.id]
    out = _brief(db, "今天天气怎么样")
    assert [(i.insight_id, i.reason) for i in out] == [(x.id, "working_memory")]


def test_capacity_eviction_rule(db):
    """容量沉降：超容时被沉降的是 (rehearsal, touched) 最小那条，余量恰好 CAP。"""
    base = _now()
    rows = []
    for i in range(wm.WORKING_CAP):
        r = _insight(db, f"沉降样本{i}", f"内容{i}")
        rows.append(r)
    # 手工布 20 条：19 条高复述、1 条零复述最旧；再递 1 条新的 → 淘汰零复述那条。
    for i, r in enumerate(rows):
        db.add(
            m.MemoryWorkingEntry(
                app_id=U.app_id,
                user_id=U.user_id,
                session_id=SESS,
                insight_id=r.id,
                project=None,
                version=1,
                rehearsal_count=5 if i else 0,
                last_touched=base - timedelta(hours=1) if i else base - timedelta(hours=5),
            )
        )
    db.commit()
    new = _insight(db, "沉降新行", "新内容")
    wm.record_deliveries(db, U.app_id, U.user_id, SESS, [new], _now())
    db.commit()
    kept = {e.insight_id for e in _entries(db)}
    assert len(kept) == wm.WORKING_CAP
    assert rows[0].id not in kept  # 零复述最旧者被沉降
    assert new.id in kept
    assert all(r.id in kept for r in rows[1:])


def test_cross_user_isolation(db):
    """同 session_id 也绝不跨用户：B 的简报看不见 A 的热条目。"""
    x = _insight(db, "部署前备份数据库", "上线前先对生产库做全量备份并验证备份可恢复")
    assert [i.insight_id for i in _brief(db, "部署前要做什么")] == [x.id]
    assert _brief(db, "今天天气怎么样", user=OTHER) == []
    assert _entries(db, user=OTHER) == []


def test_forget_purges_buffer(client, db):
    """真实 DELETE 删经验后，缓冲条目清掉，下轮简报不再出现该行。"""
    x = _insight(db, "部署前备份数据库", "上线前先对生产库做全量备份并验证备份可恢复")
    assert [i.insight_id for i in _brief(db, "部署前要做什么")] == [x.id]
    assert len(_entries(db)) == 1
    r = client.delete(f"/v1/me/memories/{x.id}", params={"user_id": "alice"})
    assert r.status_code == 204
    assert _entries(db) == []
    assert _brief(db, "今天天气怎么样") == []


def test_stale_version_and_closed_row_dropped(db):
    """存量行版本变化 / 关闭后，条目被删、旧内容不出现。"""
    x = _insight(db, "部署前备份数据库", "上线前先对生产库做全量备份并验证备份可恢复")
    y = _insight(db, "发布后验证接口", "发布后跑一遍核心接口冒烟")
    assert len(_brief(db, "部署前要做什么")) == 1
    # 第二轮：y 正常命中；x 已在缓冲，名额未满时补位（这正是补位语义）。
    out2 = _brief(db, "发布后要做什么")
    assert y.id in [i.insight_id for i in out2]
    assert len(_entries(db)) == 2
    db.execute(m.MemoryInsight.__table__.update().where(m.MemoryInsight.id == x.id).values(version=2, content="新版内容不再提备份"))
    db.execute(m.MemoryInsight.__table__.update().where(m.MemoryInsight.id == y.id).values(valid_to=_now()))
    db.commit()
    # Core 更新绕过了 ORM 会话：同一会话的 identity map 仍攥着旧行，
    # 必须 expire 模拟生产“每请求新会话”——否则测的是会话陈旧，不是缓冲校验。
    db.expire_all()
    assert _brief(db, "今天天气怎么样") == []
    assert _entries(db) == []


def test_bounded_cost_with_fillers(db):
    """2000 填充行下，缓冲读写查询数有界（事件计数，非计时断言）。"""
    db.execute(
        insert(m.MemoryInsight.__table__),
        [
            {
                "id": f"filler{i:05d}",
                "app_id": U.app_id,
                "user_id": U.user_id,
                "kind": "strategy",
                "subject": f"填充{i}",
                "content": f"填充内容{i}",
            }
            for i in range(2000)
        ],
    )
    db.commit()
    x = _insight(db, "部署前备份数据库", "上线前先对生产库做全量备份并验证备份可恢复")
    assert [i.insight_id for i in _brief(db, "部署前要做什么")] == [x.id]

    def _counting(block):
        seen = []

        def _hook(conn, cursor, statement, parameters, context, executemany):
            seen.append(1)

        event.listen(db.get_bind(), "before_cursor_execute", _hook)
        try:
            block()
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", _hook)
        return len(seen)

    n_fill = _counting(lambda: wm.fill_candidates(db, U.app_id, U.user_id, SESS, None, set(), 5, _now()))
    assert n_fill <= 5
    n_record = _counting(lambda: wm.record_deliveries(db, U.app_id, U.user_id, SESS, [x], _now()))
    assert n_record <= 5


def test_invalid_session_disables_buffer(db):
    """非法会话身份（空串/超长）时缓冲不启用，简报照常。"""
    x = _insight(db, "部署前备份数据库", "上线前先对生产库做全量备份并验证备份可恢复")
    out = brief_for_task(db, U, "部署前要做什么", 5, session_id="")
    assert [i.insight_id for i in out] == [x.id]
    out = brief_for_task(db, U, "部署前要做什么", 5, session_id="s" * 65)
    assert [i.insight_id for i in out] == [x.id]
    assert _entries(db, session="") == []
    assert db.scalar(select(m.MemoryWorkingEntry.id).limit(1)) is None


def test_project_bucketing(db):
    """A 项目缓冲条目不补位进 B 项目简报，同项目简报可以。"""
    x = _insight(db, "部署前备份数据库", "上线前先对生产库做全量备份并验证备份可恢复", project="projA")
    assert [i.insight_id for i in _brief(db, "部署前要做什么", project="projA")] == [x.id]
    assert _brief(db, "今天天气怎么样", project="projB") == []
    out = _brief(db, "今天天气怎么样", project="projA")
    assert [(i.insight_id, i.reason) for i in out] == [(x.id, "working_memory")]


def test_ttl_purge(db):
    """last_touched 超 24 小时条目读时 purge。"""
    x = _insight(db, "部署前备份数据库", "上线前先对生产库做全量备份并验证备份可恢复")
    assert [i.insight_id for i in _brief(db, "部署前要做什么")] == [x.id]
    db.execute(m.MemoryWorkingEntry.__table__.update().values(last_touched=_now() - timedelta(hours=25)))
    db.commit()
    assert _brief(db, "今天天气怎么样") == []
    assert _entries(db) == []


def test_switched_off_disables_buffer(client, db):
    """显式关开关用户直接调简报：不建条目、不补位（后备闸）。

    注意：从未同意的用户本就没有设置行，PUT False 不建行（端点既有语义）；
    显式关闭必须先同意建行、再关——否则测的是“无行”而不是“关开关”。
    """
    consent(client, "alice")
    r = client.put("/v1/me/settings", json={"user_id": "alice", "memory_enabled": False, "consent_version": CONSENT_VERSION})
    assert r.status_code == 200
    x = _insight(db, "部署前备份数据库", "上线前先对生产库做全量备份并验证备份可恢复")
    out = brief_for_task(db, U, "部署前要做什么", 5, session_id=SESS)
    assert [i.insight_id for i in out] == [x.id]
    assert _entries(db) == []
    assert brief_for_task(db, U, "今天天气怎么样", 5, session_id=SESS) == []


def test_no_settings_row_still_buffers(db):
    """无设置行直接调用：缓冲照常工作（后备闸只拦显式关闭）。"""
    x = _insight(db, "部署前备份数据库", "上线前先对生产库做全量备份并验证备份可恢复")
    assert [i.insight_id for i in _brief(db, "部署前要做什么")] == [x.id]
    assert len(_entries(db)) == 1


def test_rest_brief_session_passthrough(client, db):
    """REST 简报透传 session_id：两轮同会话，第二轮无匹配任务时补位。"""
    consent(client, "alice")
    x = _insight(db, "部署前备份数据库", "上线前先对生产库做全量备份并验证备份可恢复")
    r1 = client.post("/v1/brief", json={"user_id": "alice", "task": "部署前要做什么", "session_id": SESS})
    assert r1.status_code == 200
    assert [i["insight_id"] for i in r1.json()["insights"]] == [x.id]
    r2 = client.post("/v1/brief", json={"user_id": "alice", "task": "今天天气怎么样", "session_id": SESS})
    assert r2.status_code == 200
    assert [(i["insight_id"], i["reason"]) for i in r2.json()["insights"]] == [(x.id, "working_memory")]
