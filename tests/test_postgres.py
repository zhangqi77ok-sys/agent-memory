"""只在 Postgres 上有意义的检查：迁移产物、行级权限（真实运行账号）、SKIP LOCKED。"""
import os

import pytest
from sqlalchemy import select, text

from app import models as m
from app.db import SessionLocal, WorkerSession, engine, worker_engine
from app.worker import claim_job

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs Postgres")
TABLES = ("memory_fact", "session_message", "memory_preference", "session_summary", "memory_job")


def test_migration_artifacts(db):
    c = db.connection()
    assert c.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar() == 1
    idx = {r[0] for r in c.execute(text("SELECT indexname FROM pg_indexes WHERE tablename='memory_fact'"))}
    assert {"ix_fact_active", "ix_fact_hnsw", "ix_fact_fts"} <= idx
    rows = c.execute(text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:t)"),
                     {"t": list(TABLES)}).all()
    assert len(rows) == 5 and all(r[1] and r[2] for r in rows)


def test_service_account_is_not_superuser():
    with engine.connect() as c:
        su, bypass = c.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()
    assert (su, bypass) == (False, False)


def test_worker_account_bypasses_rls_but_is_not_superuser():
    with worker_engine.connect() as c:
        su, bypass = c.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()
    assert (su, bypass) == (False, True)


def test_app_account_cannot_self_grant_bypass(db):
    """负例：应用账号自己设 app.rls_bypass / 伪造其它会话变量，仍然看不到别人的行。"""
    db.add(m.MemoryJob(user_id="bob", idempotency_key="kb", session_id="s"))
    db.commit()
    with SessionLocal(info={"user_id": "alice"}) as s:
        s.execute(text("SELECT set_config('app.rls_bypass', 'on', true)"))
        assert s.scalars(select(m.MemoryJob)).all() == []
    with SessionLocal() as s:
        s.execute(text("SELECT set_config('app.rls_bypass', 'on', true)"))
        assert s.scalars(select(m.MemoryJob)).all() == []


def _seed(db):
    db.add_all([m.MemoryFact(user_id="alice", subject="s", content="A"),
                m.MemoryFact(user_id="bob", subject="s", content="B")])
    db.commit()


def test_rls_hides_other_users_even_without_where(db):
    """模拟代码漏写 user_id 条件：服务账号 + 当前用户上下文，仍只能看到自己的行。"""
    _seed(db)
    with SessionLocal(info={"user_id": "alice"}) as s:
        assert s.scalars(select(m.MemoryFact.user_id)).all() == ["alice"]
    with SessionLocal() as s:  # 没有用户上下文 → 一行都看不到（默认拒绝）
        assert s.scalars(select(m.MemoryFact)).all() == []


def test_rls_blocks_writing_rows_for_other_user():
    with SessionLocal(info={"user_id": "alice"}) as s:
        s.add(m.MemoryFact(user_id="bob", subject="s", content="伪造"))
        with pytest.raises(Exception, match="row-level security"):
            s.commit()


def test_api_cross_user_recall_and_forget_via_service_account(client, db):
    from tests.conftest import consent, token
    consent(client, 'alice', 'bob')

    _seed(db)
    bob_id = db.scalar(select(m.MemoryFact.id).where(m.MemoryFact.user_id == "bob"))
    facts = client.post("/v1/memories/recall", headers=token("alice"), json={"query": "A"}).json()["facts"]
    assert [f["content"] for f in facts] == ["A"]
    assert client.delete(f"/v1/me/memories/{bob_id}", headers=token("alice")).status_code == 404


def test_api_write_goes_through_rls(client, db):
    from tests.conftest import consent, token
    consent(client, 'alice')

    r = client.post("/v1/memories", headers={**token("alice"), "Idempotency-Key": "k"},
                    json={"session_id": "s", "messages": [{"msg_id": "1", "role": "user", "content": "hi"}]})
    assert r.status_code == 202
    assert db.scalar(select(m.MemoryJob.user_id)) == "alice"


def test_skip_locked_two_workers_get_different_jobs(db):
    db.add_all([m.MemoryJob(user_id="u", idempotency_key="k1", session_id="s"),
                m.MemoryJob(user_id="u", idempotency_key="k2", session_id="s")])
    db.commit()
    s1, s2 = WorkerSession(), WorkerSession()
    try:
        j1 = s1.scalar(select(m.MemoryJob).order_by(m.MemoryJob.created_at).limit(1).with_for_update(skip_locked=True))
        j2 = claim_job(s2)
        assert j1 and j2 and j1.id != j2.id
    finally:
        s1.rollback()
        s1.close()
        s2.close()


def test_user_setting_is_rls_protected(db):
    c = db.connection()
    r = c.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='user_setting'")).one()
    assert r == (True, True)
    db.add(m.UserSetting(user_id="bob", memory_enabled=True))
    db.commit()
    with SessionLocal(info={"user_id": "alice"}) as s:
        assert s.scalars(select(m.UserSetting)).all() == []
