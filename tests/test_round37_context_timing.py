"""第 37 轮：提醒时机——触发时机结合会话上下文（工作记忆缓冲）。

三车道（内容判定之后叠加，内容阈值不动）：
- 强命中（关键词/both、确信向量）→ 立即触发，上下文不拦；
- 灰区命中（纯向量 < 0.80）→ 上下文缺席或相关时照旧展示，上下文在场
  且不相关时暂缓（不展示、不记 fire_count、不消费）；
- 无内容命中但上下文相关 → 救援展示（once 型永不因此落幕）。

上下文 = 第 34 轮会话缓冲里的现行经验行（主题+正文 token 并集），
相关性复用关键词路证据线（overlap ≥ 0.5，短条件 ≤2 token 需全中）。
假向量 2 维（与 fix9 同款 DictEmbedder），PG 下 EMBEDDING_DIM=2 一致。
"""

import math
from datetime import datetime, timezone

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.reminders import check_triggers, create_reminder
from app.schemas import CONSENT_VERSION
from tests.conftest import consent

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")
OTHER = UserContext(app_id=APP, user_id="bob")
SESS = "sess-37"

COND = "部署服务时"  # tokens: 部署/署服/服务/务时（4 个）
REL_SUBJECT = "部署服务流程"  # 与条件重叠 3/4 = 0.75 → 相关
IRR_SUBJECT = "写周报总结"  # 与条件重叠 0 → 不相关
SHORT_COND = "上线前"  # tokens: 上线/线前（2 个，短条件需全中）
SHORT_PARTIAL = "准备上线"  # 与短条件重叠 上线 1/2 → 不相关
SHORT_FULL = "上线前检查清单"  # 与短条件重叠 2/2 → 相关


class DictEmbedder:
    """按文本查表的假嵌入器（fix9 同款）：条件向量 [1,0]，任务向量按 cos 定。"""

    def __init__(self, mapping, model="fake-embed-1"):
        self.mapping = mapping
        self.model = model

    def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        return [list(self.mapping[t]) for t in texts]


def _vec(cos):
    return [cos, math.sqrt(1 - cos * cos)]


def _gray_embedder(task):
    """纯向量灰区：cos=0.75（≥0.66 触发线，<0.80 确信线），任务串与条件零关键词重叠。"""
    return DictEmbedder({COND: [1.0, 0.0], task: _vec(0.75)})


def _insight(db, subject, content="", user=U):
    row = m.MemoryInsight(
        app_id=user.app_id, user_id=user.user_id, kind="strategy",
        subject=subject, content=content, applies_when="", project=None,
    )
    db.add(row)
    db.commit()
    return row


def _buffer(db, insight_row, session=SESS, user=U, project=None):
    """直接往会话缓冲里放一条（确定性，不走简报递出路径）。"""
    db.add(
        m.MemoryWorkingEntry(
            app_id=user.app_id, user_id=user.user_id, session_id=session,
            insight_id=insight_row.id, project=project, version=1,
            rehearsal_count=0, last_touched=datetime.now(timezone.utc),
        )
    )
    db.commit()


def _row(db, reminder_id):
    return db.get(m.MemoryReminder, reminder_id)


def test_strong_hit_ignores_irrelevant_context(db):
    """强命中不受无关上下文拦：关键词命中 + 无关缓冲 → 照常落幕。"""
    _buffer(db, _insight(db, IRR_SUBJECT))
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True)
    fired = check_triggers(db, U, "部署服务时先检查", embedder=None, session_id=SESS)
    assert [r.id for r in fired] == [armed.id]
    row = _row(db, armed.id)
    assert row.status == "fired" and row.fire_count == 1 and row.last_timing == "content"


def test_no_session_gray_displays_old_behavior(db):
    """无会话 = 旧行为：灰区命中照旧展示、once 不落幕。"""
    emb = _gray_embedder("把这件事情做完")
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True, embedder=emb)
    fired = check_triggers(db, U, "把这件事情做完", embedder=emb)
    assert [r.id for r in fired] == [armed.id]
    row = _row(db, armed.id)
    assert row.status == "armed" and row.fire_count == 1 and row.last_timing == "content"


def test_gray_held_when_context_irrelevant(db):
    """灰区 + 在场不相关上下文 → 暂缓：不展示、不记 fire_count、不消费。"""
    _buffer(db, _insight(db, IRR_SUBJECT))
    emb = _gray_embedder("把这件事情做完")
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True, embedder=emb)
    fired = check_triggers(db, U, "把这件事情做完", embedder=emb, session_id=SESS)
    assert fired == []
    row = _row(db, armed.id)
    assert row.status == "armed" and row.fire_count == 0
    assert row.hold_count == 1 and row.last_held_at is not None and row.last_timing == "held"


def test_gray_displays_when_context_relevant(db):
    """灰区 + 相关上下文 → 照旧展示。"""
    _buffer(db, _insight(db, REL_SUBJECT))
    emb = _gray_embedder("把这件事情做完")
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True, embedder=emb)
    fired = check_triggers(db, U, "把这件事情做完", embedder=emb, session_id=SESS)
    assert [r.id for r in fired] == [armed.id]
    row = _row(db, armed.id)
    assert row.status == "armed" and row.fire_count == 1 and row.last_timing == "content"


def test_held_fires_when_context_turns_relevant(db):
    """暂缓后上下文变相关 → 再次检查展示（灰区仍不落幕），随后强命中落幕。"""
    _buffer(db, _insight(db, IRR_SUBJECT))
    emb = _gray_embedder("把这件事情做完")
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True, embedder=emb)
    assert check_triggers(db, U, "把这件事情做完", embedder=emb, session_id=SESS) == []
    _buffer(db, _insight(db, REL_SUBJECT))
    fired = check_triggers(db, U, "把这件事情做完", embedder=emb, session_id=SESS)
    assert [r.id for r in fired] == [armed.id]
    row = _row(db, armed.id)
    assert row.hold_count == 1 and row.fire_count == 1 and row.status == "armed"
    fired = check_triggers(db, U, "部署服务时先检查", embedder=None, session_id=SESS)
    assert [r.id for r in fired] == [armed.id]
    assert _row(db, armed.id).status == "fired"


def test_rescue_terse_task_never_consumes_once(db):
    """救援：简短任务无内容命中、上下文相关 → 展示且 once 不落幕。"""
    _buffer(db, _insight(db, REL_SUBJECT))
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True)
    fired = check_triggers(db, U, "继续", embedder=None, session_id=SESS)
    assert [r.id for r in fired] == [armed.id]
    row = _row(db, armed.id)
    assert row.status == "armed" and row.fire_count == 1 and row.last_timing == "context"


def test_rescue_then_strong_consumes(db):
    """救援不消费：救援展示后，强命中照常落幕。"""
    _buffer(db, _insight(db, REL_SUBJECT))
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True)
    check_triggers(db, U, "继续", embedder=None, session_id=SESS)
    assert _row(db, armed.id).status == "armed"
    fired = check_triggers(db, U, "部署服务时先检查", embedder=None, session_id=SESS)
    assert [r.id for r in fired] == [armed.id]
    assert _row(db, armed.id).status == "fired"


def test_short_condition_rescue_needs_full_match(db):
    """短条件救援需全中：部分重叠不救援，全中才救援。"""
    _buffer(db, _insight(db, SHORT_PARTIAL))
    armed = create_reminder(db, U, condition=SHORT_COND, action="先检查清单", once=True)
    assert check_triggers(db, U, "继续", embedder=None, session_id=SESS) == []
    assert _row(db, armed.id).fire_count == 0
    _buffer(db, _insight(db, SHORT_FULL))
    fired = check_triggers(db, U, "继续", embedder=None, session_id=SESS)
    assert [r.id for r in fired] == [armed.id]


def test_no_context_no_rescue(db):
    """无上下文不救援：简短任务 alone 撞不上条件就是撞不上。"""
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True)
    assert check_triggers(db, U, "继续", embedder=None, session_id=SESS) == []
    assert check_triggers(db, U, "继续", embedder=None) == []
    assert _row(db, armed.id).fire_count == 0


def test_switched_off_user_has_no_context(client, db):
    """开关关闭用户无上下文：灰区照旧展示、简短任务无救援（旧行为）。"""
    consent(client, "alice")
    r = client.put("/v1/me/settings", json={"user_id": "alice", "memory_enabled": False,
                                            "consent_version": CONSENT_VERSION})
    assert r.status_code == 200
    _buffer(db, _insight(db, REL_SUBJECT))
    emb = _gray_embedder("把这件事情做完")
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True, embedder=emb)
    fired = check_triggers(db, U, "把这件事情做完", embedder=emb, session_id=SESS)
    assert [r.id for r in fired] == [armed.id]  # 开关关闭 → buffer_enabled 为空 → 无上下文
    assert check_triggers(db, U, "继续", embedder=None, session_id=SESS) == []


def test_cross_user_buffer_isolation(db):
    """跨用户隔离：bob 的缓冲救不了 alice，alice 的缓冲也拦不住 bob 的灰区。"""
    _buffer(db, _insight(db, REL_SUBJECT, user=OTHER), user=OTHER)
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True)
    assert check_triggers(db, U, "继续", embedder=None, session_id=SESS) == []
    assert _row(db, armed.id).fire_count == 0


def test_project_gate_beats_rescue(db):
    """项目门优先于救援：提醒项目与任务项目明确不同 → 即使上下文相关也不救援。"""
    _buffer(db, _insight(db, REL_SUBJECT))
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True, project="proj-a")
    fired = check_triggers(db, U, "继续", embedder=None, project="proj-b", session_id=SESS)
    assert fired == []
    assert _row(db, armed.id).fire_count == 0


def test_dedup_rescue_and_content_same_action(db):
    """去重：内容命中与救援同动作 → 只展示分数最高者一条。"""
    _buffer(db, _insight(db, "数据库迁移流程"))
    a = create_reminder(db, U, condition=COND, action="先备份", once=False)
    b = create_reminder(db, U, condition="数据库迁移时", action="先备份", once=False)
    fired = check_triggers(db, U, "部署服务时先检查", embedder=None, session_id=SESS)
    assert len(fired) == 1 and fired[0].id == a.id
    assert _row(db, b.id).fire_count == 0


def test_rest_check_session_wiring(client, db):
    """REST /v1/reminders/check 的 session_id 接线：简短任务 + 相关缓冲 → 救援展示。"""
    consent(client, "alice")
    _buffer(db, _insight(db, REL_SUBJECT))
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True)
    r = client.get("/v1/reminders/check", params={"task": "继续", "user_id": "alice",
                                                  "session_id": SESS})
    assert r.status_code == 200
    got = r.json()
    assert [g["reminder_id"] for g in got] == [armed.id]
    assert got[0]["last_timing"] == "context" and got[0]["hold_count"] == 0
    assert _row(db, armed.id).status == "armed"
