"""第 34 轮：工作记忆缓冲表 memory_working_entry。

会话内“还热着”的经验条目：只存 id + 项目 + 版本快照，不存正文。
模型层（MemoryWorkingEntry.__table_args__）已声明同名索引与唯一约束，
测试的 create_all 直接带上；本迁移只给存量库补。缓冲从空开始，
旧会话无热条目是正常态，无回填。
"""

import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401

revision = "0027_working_memory"
down_revision = "0026_insight_move"


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "memory_working_entry" not in insp.get_table_names():
        op.create_table(
            "memory_working_entry",
            sa.Column("id", sa.String(32), nullable=False),
            sa.Column("app_id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("session_id", sa.String(64), nullable=False),
            sa.Column("insight_id", sa.String(32), nullable=False),
            sa.Column("project", sa.String(128), nullable=True),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("rehearsal_count", sa.Integer, nullable=False),
            sa.Column("last_touched", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("app_id", "user_id", "session_id", "insight_id",
                                name="uq_working_session_insight"),
        )
    idx_names = {ix["name"] for ix in insp.get_indexes("memory_working_entry")}
    if "ix_working_session" not in idx_names:
        op.create_index("ix_working_session", "memory_working_entry",
                        ["app_id", "user_id", "session_id"])


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "memory_working_entry" in insp.get_table_names():
        idx_names = {ix["name"] for ix in insp.get_indexes("memory_working_entry")}
        if "ix_working_session" in idx_names:
            op.drop_index("ix_working_session", table_name="memory_working_entry")
        op.drop_table("memory_working_entry")
