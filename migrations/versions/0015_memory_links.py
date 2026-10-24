"""第十五轮「织网」：记忆关联边 memory_link。

只建本表（0008/0014 同一手法：按当前 models 的子集 create_all）；PG 下开
RLS（策略与其它用户表同形）并给 app/worker 角色授权。存量记忆不回填边：
边由写入时织网与每小时 weave_links 巡检逐步长出来，历史关系靠巡检补齐。
"""
from alembic import op

import app.models  # noqa: F401
from app.db import Base

revision = "0015_memory_links"
down_revision = "0014_keyword_postings"

TABLE = "memory_link"


def upgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    Base.metadata.create_all(bind, tables=[Base.metadata.tables[TABLE]])
    if is_pg:
        op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {TABLE}_user_isolation ON {TABLE} USING ("
            f"current_setting('app.user_id', true) IS NULL OR user_id = current_setting('app.user_id', true))"
        )
        for role in ("memsvc_app", "memsvc_worker"):
            op.execute(
                f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
                f"THEN GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {role}; END IF; END $$"
            )


def downgrade():
    op.drop_table(TABLE)
