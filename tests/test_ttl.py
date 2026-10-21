from datetime import datetime, timedelta, timezone

from app import models as m
from app.worker import expire_memories


def test_expire_memories_when_ttl_is_zero(db):
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=100)
    fact = m.MemoryFact(app_id="tiancode", user_id="alice", subject="s", content="c", valid_from=old_time)
    pref = m.MemoryPreference(app_id="tiancode", user_id="alice", key="k", value="v", valid_from=old_time)
    db.add_all([fact, pref])
    db.commit()

    expired = expire_memories(db, ttl_days=0, now=now)
    assert expired == 0
    assert fact.valid_to is None
    assert pref.valid_to is None


def test_expire_memories_marks_old_items_invalid(db):
    now = datetime.now(timezone.utc)
    past_40_days = now - timedelta(days=40)
    past_10_days = now - timedelta(days=10)

    # 40 天前，应过期
    f1 = m.MemoryFact(app_id="tiancode", user_id="alice", subject="s1", content="old fact", valid_from=past_40_days)
    p1 = m.MemoryPreference(app_id="tiancode", user_id="alice", key="k1", value="old pref", valid_from=past_40_days)
    # 10 天前，不应过期 (TTL=30)
    f2 = m.MemoryFact(app_id="tiancode", user_id="alice", subject="s2", content="recent fact", valid_from=past_10_days)
    p2 = m.MemoryPreference(app_id="tiancode", user_id="alice", key="k2", value="recent pref", valid_from=past_10_days)

    db.add_all([f1, p1, f2, p2])
    db.commit()

    count = expire_memories(db, ttl_days=30, now=now)
    assert count == 2

    assert f1.valid_to == now
    assert p1.valid_to == now
    assert f2.valid_to is None
    assert p2.valid_to is None
