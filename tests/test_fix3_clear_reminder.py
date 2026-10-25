"""修复第 3 轮：clear_all 给提醒写墓碑（此前提醒从未写过任何墓碑，
恢复五类重删里的 reminder 一类会是空文）。

SQLite 可跑（不依赖 PG 演练库）：consent 后给 alice/bob 各写一条
提醒，清空 alice，断言 alice 的提醒消失、bob 的 intact，且墓碑两
张表里都有 alice 那条提醒的 id 段与内容哈希段。
"""

from sqlalchemy import select

from app import models as m
from app.tombstone import content_hash
from tests.conftest import consent, uid


def test_clear_all_writes_reminder_tombstones(client, db):
    consent(client, "alice")
    mine = m.MemoryReminder(user_id="alice", condition_text="fix3条件甲", action_text="fix3动作甲")
    other = m.MemoryReminder(user_id="bob", condition_text="fix3条件乙", action_text="fix3动作乙")
    db.add_all([mine, other])
    db.commit()
    mine_id = mine.id
    assert client.delete("/v1/me/memories", params=uid("alice")).status_code == 204
    db.expire_all()
    assert db.get(m.MemoryReminder, mine_id) is None
    assert db.get(m.MemoryReminder, other.id) is not None
    assert db.scalar(select(m.ForgottenId).where(m.ForgottenId.id == mine_id, m.ForgottenId.kind == "reminder")) is not None
    assert (
        db.scalar(
            select(m.ForgottenContent).where(
                m.ForgottenContent.user_id == "alice",
                m.ForgottenContent.kind == "reminder",
                m.ForgottenContent.content_hash == content_hash("fix3条件甲", "fix3动作甲"),
            )
        )
        is not None
    )
