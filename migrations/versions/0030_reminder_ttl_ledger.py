"""第 38 轮：提醒 TTL + 投递账本（审计 F-13 提醒部分）。

memory_reminder 加 expires_at（可选截止时间，NULL = 永不过期）；
新建 memory_reminder_delivery 表（逐次投递账）。无回填：旧提醒
expires_at 全 NULL（与旧语义一致），旧展示无账本（不补历史、不编造）。
"""

import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401

revision = "0030_reminder_ttl_ledger"
down_revision = "0029_reminder_timing"


def upgrade():
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("memory_reminder")}
    if "expires_at" not in cols:
        op.add_column("memory_reminder", sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index("ix_memory_reminder_expires_at", "memory_reminder", ["expires_at"])
    tables = set(insp.get_table_names())
    if "memory_reminder_delivery" not in tables:
        op.create_table(
            "memory_reminder_delivery",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("app_id", sa.String(64), nullable=False, server_default="tiancode"),
            sa.Column("user_id", sa.String(64), nullable=False, index=True),
            sa.Column("reminder_id", sa.String(32), nullable=False, index=True),
            sa.Column("event", sa.String(16), nullable=False),
            sa.Column("task", sa.Text(), nullable=False, server_default=""),
            sa.Column("timing", sa.String(16), nullable=True),
            sa.Column("kind", sa.String(16), nullable=True),
            sa.Column("score", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_reminder_delivery_reminder_created",
                        "memory_reminder_delivery", ["reminder_id", "created_at"])


def downgrade():
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    if "memory_reminder_delivery" in tables:
        op.drop_index("ix_reminder_delivery_reminder_created",
                      table_name="memory_reminder_delivery")
        op.drop_table("memory_reminder_delivery")
    cols = {c["name"] for c in insp.get_columns("memory_reminder")}
    if "expires_at" in cols:
        op.drop_index("ix_memory_reminder_expires_at", table_name="memory_reminder")
        op.drop_column("memory_reminder", "expires_at")
