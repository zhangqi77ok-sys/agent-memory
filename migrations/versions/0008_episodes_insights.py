"""经验记忆两张表：task_episode（任务过程）与 memory_insight（提炼出的经验）。

与 0001 同一手法：只建本迁移自己的表；PG 下开 RLS 用户隔离并给 worker/app 角色授权。
"""
from alembic import op

import app.models  # noqa: F401
from app.db import Base

revision = "0008_episodes_insights"
down_revision = "0007_memory_job_stats_fn"

TABLES = ("task_episode", "memory_insight")


def upgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    Base.metadata.create_all(bind, tables=[Base.metadata.tables[t] for t in TABLES])
    if not is_pg:
        return
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {t}_user_isolation ON {t} USING ("
            f"current_setting('app.user_id', true) IS NULL OR user_id = current_setting('app.user_id', true))"
        )
        for role in ("memsvc_app", "memsvc_worker"):
            op.execute(f"""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO {role}; END IF; END $$;""")


def downgrade():
    Base.metadata.drop_all(op.get_bind(), tables=[Base.metadata.tables[t] for t in TABLES])
