"""第 37 轮：提醒时机审计列（memory_reminder）。

hold_count / last_held_at 记灰区暂缓账；last_timing 记最近一次检查结论
（content / context / held）。只加列、无回填：旧行三列默认 0 / NULL，
即“从未暂缓、无时机结论”，与旧语义一致。
"""

import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401

revision = "0029_reminder_timing"
down_revision = "0028_duel"


def upgrade():
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("memory_reminder")}
    if "hold_count" not in cols:
        op.add_column("memory_reminder", sa.Column("hold_count", sa.Integer(), nullable=False, server_default="0"))
    if "last_held_at" not in cols:
        op.add_column("memory_reminder", sa.Column("last_held_at", sa.DateTime(timezone=True), nullable=True))
    if "last_timing" not in cols:
        op.add_column("memory_reminder", sa.Column("last_timing", sa.String(16), nullable=True))


def downgrade():
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("memory_reminder")}
    for name in ("last_timing", "last_held_at", "hold_count"):
        if name in cols:
            op.drop_column("memory_reminder", name)
