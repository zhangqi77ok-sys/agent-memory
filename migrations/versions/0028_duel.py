"""第 36 轮：跨项目矛盾对决记录表 memory_duel。

胜负裁决的审计留痕：无序对按 id 排序存 a/b，现行记录部分唯一。
模型层（MemoryDuel.__table_args__）已声明同名索引；测试的
create_all 直接带上；本迁移只给存量库补。无回填：旧矛盾对在
下一轮巡检对决步里自然建记录。
"""

import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401

revision = "0028_duel"
down_revision = "0027_working_memory"


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "memory_duel" not in insp.get_table_names():
        op.create_table(
            "memory_duel",
            sa.Column("id", sa.String(32), nullable=False),
            sa.Column("app_id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("a_id", sa.String(32), nullable=False),
            sa.Column("b_id", sa.String(32), nullable=False),
            sa.Column("a_project", sa.String(128), nullable=False),
            sa.Column("b_project", sa.String(128), nullable=False),
            sa.Column("winner_id", sa.String(32), nullable=True),
            sa.Column("loser_id", sa.String(32), nullable=True),
            sa.Column("verdict", sa.String(16), nullable=False),
            sa.Column("scores", sa.JSON, nullable=False),
            sa.Column("criteria", sa.String(32), nullable=False),
            sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
            sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    idx_names = {ix["name"] for ix in insp.get_indexes("memory_duel")}
    if "ix_duel_current_unique" not in idx_names:
        op.create_index("ix_duel_current_unique", "memory_duel",
                        ["app_id", "user_id", "a_id", "b_id"], unique=True,
                        postgresql_where=sa.text("valid_to IS NULL"),
                        sqlite_where=sa.text("valid_to IS NULL"))
    if "ix_duel_loser" not in idx_names:
        op.create_index("ix_duel_loser", "memory_duel",
                        ["app_id", "user_id", "loser_id"])


def downgrade():
    insp = sa.inspect(op.get_bind())
    idx_names = {ix["name"] for ix in insp.get_indexes("memory_duel")}
    if "ix_duel_loser" in idx_names:
        op.drop_index("ix_duel_loser", table_name="memory_duel")
    if "ix_duel_current_unique" in idx_names:
        op.drop_index("ix_duel_current_unique", table_name="memory_duel")
    if "memory_duel" in insp.get_table_names():
        op.drop_table("memory_duel")
