"""修复第 10 轮：现行边部分唯一索引（审计 F3b）。

同一用户同一端点同一关系的现行边只能有一行，并发检查-插入由该索引
兜底（create_link 撞索引后 SAVEPOINT 回查）。先去重后建索引：同键多
现行行时保留 strength 最大者、并列取 created_at 最早者，其余置
valid_to=now；无现行重复时去重为空操作。PG 与 SQLite 均支持部分索引
（WHERE valid_to IS NULL）与窗口函数，故同一份 SQL 双库通用。
模型层（MemoryLink.__table_args__）已声明同名索引，测试的 create_all
直接带上；本迁移只给存量库补。
"""

import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401

revision = "0025_link_current_unique"
down_revision = "0024_forgotten_content"


def upgrade():
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE memory_link SET valid_to = CURRENT_TIMESTAMP WHERE id IN ("
            " SELECT id FROM ("
            "  SELECT id, ROW_NUMBER() OVER (PARTITION BY app_id, user_id, src_kind, src_id,"
            "   dst_kind, dst_id, relation ORDER BY strength DESC, created_at ASC) AS rn"
            "  FROM memory_link WHERE valid_to IS NULL"
            " ) WHERE rn > 1)"
        )
    )
    insp = sa.inspect(bind)
    idx_names = {ix["name"] for ix in insp.get_indexes("memory_link")}
    if "ix_link_current_unique" not in idx_names:
        op.create_index(
            "ix_link_current_unique",
            "memory_link",
            ["app_id", "user_id", "src_kind", "src_id", "dst_kind", "dst_id", "relation"],
            unique=True,
            postgresql_where=sa.text("valid_to IS NULL"),
            sqlite_where=sa.text("valid_to IS NULL"),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    idx_names = {ix["name"] for ix in insp.get_indexes("memory_link")}
    if "ix_link_current_unique" in idx_names:
        op.drop_index("ix_link_current_unique", table_name="memory_link")
