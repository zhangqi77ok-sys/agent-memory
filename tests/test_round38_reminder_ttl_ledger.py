"""第 38 轮：提醒 TTL 覆盖 + 投递账本（审计 F-13 提醒部分）。

核实过的缺口：提醒行此前没有任何 TTL 字段（连 valid_from 都没有），
巡检 expire_memories 只覆盖事实/偏好/经验 + 删旧轨迹，armed 提醒是永生的；
投递只有行上计数器（fire_count/hold_count/last_timing），答不出每次展示 /
暂缓 / 救援是何时、何种结论。本轮：可选 expires_at + 终态 expired（读时
惰性过期 + 巡检过期步）+ 逐次投递账本（memory_reminder_delivery，每提醒
上限 200 条写时裁剪）。假向量 2 维（与 fix9 / round37 同款 DictEmbedder），
PG 下 EMBEDDING_DIM=2 一致。
"""

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import models as m
from app import reminders as R
from app.auth import UserContext
from app.config import settings
from app.reminders import check_triggers, create_reminder, expire_reminders, list_reminder_deliveries
from app.schemas import CONSENT_VERSION
from tests.conftest import consent, uid

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")
OTHER = UserContext(app_id=APP, user_id="bob")
SESS = "sess-38"

COND = "部署服务时"  # tokens: 部署/署服/服务/务时（4 个）
REL_SUBJECT = "部署服务流程"  # 与条件重叠 3/4 = 0.75 → 相关
IRR_SUBJECT = "写周报总结"  # 与条件重叠 0 → 不相关


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


def _past():
    return datetime.now(timezone.utc) - timedelta(days=1)


def _future():
    return datetime.now(timezone.utc) + timedelta(days=30)


def _row(db, reminder_id):
    return db.get(m.MemoryReminder, reminder_id)


def _ledger(db, reminder_id, user=U):
    return list(db.scalars(select(m.MemoryReminderDelivery).where(
        m.MemoryReminderDelivery.reminder_id == reminder_id,
        m.MemoryReminderDelivery.user_id == user.user_id)))


def _insight(db, subject, user=U):
    row = m.MemoryInsight(
        app_id=user.app_id, user_id=user.user_id, kind="strategy",
        subject=subject, content="", applies_when="", project=None,
    )
    db.add(row)
    db.commit()
    return row


def _buffer(db, insight_row, session=SESS, user=U):
    db.add(m.MemoryWorkingEntry(
        app_id=user.app_id, user_id=user.user_id, session_id=session,
        insight_id=insight_row.id, project=None, version=1,
        rehearsal_count=0, last_touched=datetime.now(timezone.utc)))
    db.commit()


# ---------- TTL：过期语义 ----------

def test_expired_reminder_never_fires(db):
    """过期提醒不触发：截止已过 → 检查后 status=expired，不在返回里。"""
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True,
                            expires_at=_past())
    assert check_triggers(db, U, "部署服务时先检查", embedder=None) == []
    row = _row(db, armed.id)
    assert row.status == "expired" and row.fire_count == 0 and row.last_timing == "expired"


def test_expired_reminder_does_not_occupy_dedup_slot(db):
    """过期提醒不占同动作去重名额：同动作另一 armed 行照常展示。"""
    create_reminder(db, U, condition=COND, action="先备份", once=False,
                    expires_at=_past())
    alive = create_reminder(db, U, condition=COND, action="先备份", once=False)
    fired = check_triggers(db, U, "部署服务时先检查", embedder=None)
    assert [r.id for r in fired] == [alive.id]


def test_expired_reminder_not_rescued(db):
    """过期提醒不被上下文救援：救援池里见不到过期行。"""
    _buffer(db, _insight(db, REL_SUBJECT))
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True,
                            expires_at=_past())
    assert check_triggers(db, U, "继续", embedder=None, session_id=SESS) == []
    assert _row(db, armed.id).status == "expired"


def test_not_yet_expired_fires_normally(db):
    """未到期提醒照常触发（截止在将来）。"""
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True,
                            expires_at=_future())
    fired = check_triggers(db, U, "部署服务时先检查", embedder=None)
    assert [r.id for r in fired] == [armed.id]
    assert _row(db, armed.id).status == "fired"


def test_no_expiry_never_expires(db):
    """无 expires_at 永不过期（旧行为）：常驻提醒多次触发都不变 expired。"""
    armed = create_reminder(db, U, condition=COND, action="先备份", once=False)
    for _ in range(2):
        assert check_triggers(db, U, "部署服务时先检查", embedder=None)
    assert _row(db, armed.id).status == "armed"


def test_patrol_expire_marks_and_counts(db):
    """巡检 expire_reminders 标过期 + stats 计数；未到期行不动。"""
    dead = create_reminder(db, U, condition=COND, action="先备份", once=True,
                           expires_at=_past())
    alive = create_reminder(db, U, condition=COND, action="先备份", once=True,
                            expires_at=_future())
    stats = expire_reminders(db)
    assert stats == {"expired": 1}
    assert _row(db, dead.id).status == "expired"
    assert _row(db, alive.id).status == "armed"


def test_patrol_expire_skips_switched_off_user(client, db):
    """巡检跳过开关关闭用户（fix4 §5 口径：有设置行且未开启）。"""
    consent(client, "alice")
    client.put("/v1/me/settings", json={"user_id": "alice", "memory_enabled": False,
                                        "consent_version": CONSENT_VERSION})
    dead = create_reminder(db, U, condition=COND, action="先备份", once=True,
                           expires_at=_past())
    assert expire_reminders(db) == {"expired": 0}
    assert _row(db, dead.id).status == "armed"


# ---------- 账本：事件语义 ----------

def test_ledger_shown_fields(db):
    """账本：强命中常驻型 → shown 事件字段全（task、kind、score、timing）。"""
    armed = create_reminder(db, U, condition=COND, action="先备份", once=False)
    check_triggers(db, U, "部署服务时先检查", embedder=None)
    rows = _ledger(db, armed.id)
    assert len(rows) == 1
    ev = rows[0]
    assert ev.event == "shown" and ev.timing == "content" and ev.kind == "keyword"
    assert ev.score == 1.0 and ev.task == "部署服务时先检查"


def test_ledger_consumed_once_no_double_count(db):
    """账本：once 强命中 → consumed 一条，不另记 shown。"""
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True)
    check_triggers(db, U, "部署服务时先检查", embedder=None)
    rows = _ledger(db, armed.id)
    assert [r.event for r in rows] == ["consumed"]
    assert _row(db, armed.id).status == "fired"


def test_ledger_gray_shown_keeps_armed(db):
    """账本：灰区展示 → shown 事件（kind=vector）+ once 仍 armed。"""
    emb = _gray_embedder("把这件事情做完")
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True, embedder=emb)
    check_triggers(db, U, "把这件事情做完", embedder=emb)
    rows = _ledger(db, armed.id)
    assert len(rows) == 1 and rows[0].event == "shown" and rows[0].kind == "vector"
    assert abs(rows[0].score - 0.75) < 1e-9
    assert _row(db, armed.id).status == "armed"


def test_ledger_held(db):
    """账本：灰区暂缓 → held 事件 + hold_count+1。"""
    _buffer(db, _insight(db, IRR_SUBJECT))
    emb = _gray_embedder("把这件事情做完")
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True, embedder=emb)
    assert check_triggers(db, U, "把这件事情做完", embedder=emb, session_id=SESS) == []
    rows = _ledger(db, armed.id)
    assert len(rows) == 1 and rows[0].event == "held" and rows[0].timing == "held"
    assert _row(db, armed.id).hold_count == 1


def test_ledger_rescued_never_consumes(db):
    """账本：救援 → rescued 事件 + once 不落幕。"""
    _buffer(db, _insight(db, REL_SUBJECT))
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True)
    check_triggers(db, U, "继续", embedder=None, session_id=SESS)
    rows = _ledger(db, armed.id)
    assert len(rows) == 1 and rows[0].event == "rescued" and rows[0].timing == "context"
    assert _row(db, armed.id).status == "armed"


def test_ledger_deduped_row_leaves_no_event(db):
    """账本：去重掉的行不留事件（简报里没出现，账上不能“展示过”）。"""
    a = create_reminder(db, U, condition=COND, action="先备份", once=False)
    b = create_reminder(db, U, condition=COND, action="先备份", once=False)
    fired = check_triggers(db, U, "部署服务时先检查", embedder=None)
    assert len(fired) == 1
    assert _ledger(db, a.id) != [] and _ledger(db, b.id) == []


def test_ledger_expired_event(db):
    """账本：读时惰性过期记 expired 事件。"""
    armed = create_reminder(db, U, condition=COND, action="先备份", once=True,
                            expires_at=_past())
    check_triggers(db, U, "部署服务时先检查", embedder=None)
    rows = _ledger(db, armed.id)
    assert len(rows) == 1 and rows[0].event == "expired" and rows[0].timing == "expired"


def test_ledger_cap_pruning_keeps_newest(db, monkeypatch):
    """账本：上限裁剪（CAP 缩到 3 做探针）——写 5 事件后剩最新 3 条。"""
    monkeypatch.setattr(R, "REMINDER_LEDGER_CAP", 3)
    armed = create_reminder(db, U, condition=COND, action="先备份", once=False)
    for i in range(5):
        check_triggers(db, U, f"部署服务时先检查{i}", embedder=None)
    rows = list_reminder_deliveries(db, U, armed.id)
    assert len(rows) == 3
    assert [r.task for r in rows] == ["部署服务时先检查4", "部署服务时先检查3",
                                      "部署服务时先检查2"]


def test_ledger_cross_user_isolation(db):
    """账本：跨用户隔离——他人提醒查账本 404；账本行不串用户。"""
    armed = create_reminder(db, OTHER, condition=COND, action="先备份", once=False)
    check_triggers(db, OTHER, "部署服务时先检查", embedder=None)
    assert _ledger(db, armed.id, user=OTHER) != []
    try:
        list_reminder_deliveries(db, U, armed.id)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_ledger_cleared_by_clear_all(client, db):
    """账本：clear_all 删账本行（fix8 清单口径）。"""
    consent(client, "alice")
    armed = create_reminder(db, U, condition=COND, action="先备份", once=False)
    check_triggers(db, U, "部署服务时先检查", embedder=None)
    assert _ledger(db, armed.id) != []
    assert client.delete("/v1/me/memories", params=uid("alice")).status_code == 204
    assert _ledger(db, armed.id) == []


# ---------- REST ----------

def test_rest_create_with_expiry_and_deliveries(client, db):
    """REST：创建带 expires_at 回显；deliveries 接口列事件、limit 生效。"""
    consent(client, "alice")
    r = client.post("/v1/reminders", json={"user_id": "alice", "condition": COND,
                                            "action": "先备份", "once": False,
                                            "expires_at": _future().isoformat()})
    assert r.status_code == 201, r.text
    rid = r.json()["reminder_id"]
    assert r.json()["expires_at"] is not None
    check_triggers(db, U, "部署服务时先检查", embedder=None)
    check_triggers(db, U, "部署服务时再检查", embedder=None)
    d = client.get(f"/v1/reminders/{rid}/deliveries", params={"user_id": "alice"})
    assert d.status_code == 200 and len(d.json()) == 2
    d1 = client.get(f"/v1/reminders/{rid}/deliveries",
                    params={"user_id": "alice", "limit": 1})
    assert d1.status_code == 200 and len(d1.json()) == 1


def test_rest_deliveries_other_user_404(client, db):
    """REST：他人提醒查账本 404（不泄露存在性）。"""
    consent(client, "alice")
    armed = create_reminder(db, OTHER, condition=COND, action="先备份", once=False)
    r = client.get(f"/v1/reminders/{armed.id}/deliveries", params={"user_id": "alice"})
    assert r.status_code == 404


def test_rest_deliveries_switched_off_returns_empty(client, db):
    """REST：开关关闭用户查账本返回 []（与 list_ 同形，不 404）。"""
    consent(client, "alice")
    armed = create_reminder(db, U, condition=COND, action="先备份", once=False)
    check_triggers(db, U, "部署服务时先检查", embedder=None)
    client.put("/v1/me/settings", json={"user_id": "alice", "memory_enabled": False,
                                        "consent_version": CONSENT_VERSION})
    r = client.get(f"/v1/reminders/{armed.id}/deliveries", params={"user_id": "alice"})
    assert r.status_code == 200 and r.json() == []
