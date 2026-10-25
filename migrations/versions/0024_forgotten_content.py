"""修复第 2 轮：内容级遗忘墓碑表。

forgotten_content：forget/clear 时按内容规范化哈希记行，写路径落库前
查它——新 id 重发同一内容也命中。旧 forgotten_id（id 粒度）不动，
恢复侧仍用它（第 3 轮再接内容哈希重删）。与 0021 同手法：inspect
先查再补，重复跑不报错；RLS 与授权只在 PG 上办，形式与本轮前各新表
一致（严格化是审计 RLS 项，另轮处理）。

升级注意：旧库无内容墓碑，升级前已遗忘的内容只受旧 id-tombstone
（恢复侧）保护；写入侧禁写自升级后新发生的遗忘起生效。
"""

import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401

revision = "0024_forgotten_content"
down_revision = "0023_fact_feedback_counts"


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "forgotten_content" not in insp.get_table_names():
        op.create_table(
            "forgotten_content",
            sa.Column("app_id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("app_id", "user_id", "kind", "content_hash"),
        )
        op.create_index("ix_forgotten_content_user", "forgotten_content", ["app_id", "user_id", "forgotten_at"])
        if op.get_bind().dialect.name == "postgresql":
            op.execute("ALTER TABLE forgotten_content ENABLE ROW LEVEL SECURITY")
            op.execute(
                "CREATE POLICY forgotten_content_user_isolation "
                "ON forgotten_content USING ("
                "current_setting('app.user_id', true) IS NULL OR "
                "user_id = current_setting('app.user_id', true))"
            )
            op.execute(
                "DO $$ BEGIN "
                "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_app') "
                "THEN GRANT SELECT, INSERT, UPDATE, DELETE ON forgotten_content "
                "TO memsvc_app; END IF; "
                "END $$;"
            )


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "forgotten_content" in insp.get_table_names():
        op.drop_table("forgotten_content")
