"""v1 初始表结构

规则：迁移只做向后兼容变更（先加列，下个版本再删列）。
"""
from alembic import op

import app.models  # noqa: F401
from app.db import Base

revision = "0001_init"
down_revision = None


def upgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind)
    if is_pg:
        op.execute("CREATE INDEX IF NOT EXISTS ix_fact_active ON memory_fact (tenant_id, user_id) WHERE valid_to IS NULL")
        op.execute("CREATE INDEX IF NOT EXISTS ix_fact_hnsw ON memory_fact USING hnsw (embedding vector_cosine_ops)")
        op.execute("CREATE INDEX IF NOT EXISTS ix_fact_fts ON memory_fact USING gin (to_tsvector('simple', content))")
        for t in ("session_message", "memory_fact", "memory_preference", "session_summary", "memory_job"):
            op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY {t}_user_isolation ON {t} USING ("
                f"current_setting('app.user_id', true) IS NULL OR user_id = current_setting('app.user_id', true))"
            )


def downgrade():
    Base.metadata.drop_all(op.get_bind())
