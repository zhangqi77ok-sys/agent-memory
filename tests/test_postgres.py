"""只在 Postgres 上有意义的检查：迁移产物（pgvector、索引、RLS）。"""
import os

import pytest
from sqlalchemy import text

from app.db import engine

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs Postgres")


def test_migration_artifacts():
    with engine.connect() as c:
        assert c.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar() == 1
        idx = {r[0] for r in c.execute(text("SELECT indexname FROM pg_indexes WHERE tablename='memory_fact'"))}
        assert {"ix_fact_active", "ix_fact_hnsw", "ix_fact_fts"} <= idx
        rls = dict(c.execute(text("SELECT relname, relrowsecurity FROM pg_class WHERE relname IN "
                                  "('memory_fact','session_message','memory_preference','session_summary','memory_job')")).all())
        assert len(rls) == 5 and all(rls.values())


def test_rls_blocks_other_user_for_app_role():
    """应用账号（非超级用户）设置 app.user_id 后，只能看到自己的行。"""
    with engine.begin() as c:
        c.execute(text("INSERT INTO memory_fact (id, tenant_id, user_id, subject, content, valid_from, version) VALUES "
                       "('a','tiancode','alice','s','A', now(), 1), ('b','tiancode','bob','s','B', now(), 1)"))
        c.execute(text("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='memsvc_app') THEN "
                       "CREATE ROLE memsvc_app NOLOGIN; END IF; END $$"))
        c.execute(text("GRANT USAGE ON SCHEMA public TO memsvc_app; GRANT SELECT ON memory_fact TO memsvc_app"))
    with engine.begin() as c:
        c.execute(text("SET LOCAL ROLE memsvc_app"))
        c.execute(text("SELECT set_config('app.user_id', 'alice', true)"))
        assert [r[0] for r in c.execute(text("SELECT user_id FROM memory_fact"))] == ["alice"]


def test_skip_locked_two_workers_get_different_jobs():
    from app import models as m
    from app.db import SessionLocal
    from app.worker import claim_job

    with SessionLocal() as s:
        s.add_all([m.MemoryJob(user_id="u", idempotency_key="k1", session_id="s"),
                   m.MemoryJob(user_id="u", idempotency_key="k2", session_id="s")])
        s.commit()
    s1, s2 = SessionLocal(), SessionLocal()
    try:
        # s1 领到后保持事务内锁，模拟并发
        from sqlalchemy import select
        j1 = s1.scalar(select(m.MemoryJob).order_by(m.MemoryJob.created_at).limit(1).with_for_update(skip_locked=True))
        j2 = claim_job(s2)
        assert j1 and j2 and j1.id != j2.id
    finally:
        s1.rollback()
        s1.close()
        s2.close()
