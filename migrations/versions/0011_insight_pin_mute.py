"""第十一轮：memory_insight 补 pinned / muted 两列（用户置顶与禁用）。

与 0010 同一手法：inspect 先查再补。"""
import sqlalchemy as sa
from alembic import op

revision = "0011_insight_pin_mute"
down_revision = "0010_insight_feedback_project"

COLS = [("pinned", sa.Boolean()), ("muted", sa.Boolean())]
TABLE = "memory_insight"


def upgrade():
    insp = sa.inspect(op.get_bind())
    have = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for name, typ in COLS:
            if name not in have:
                batch.add_column(sa.Column(name, typ, nullable=False, server_default=sa.false()))
    op.execute("UPDATE memory_insight SET pinned = false WHERE pinned IS NULL")
    op.execute("UPDATE memory_insight SET muted = false WHERE muted IS NULL")


def downgrade():
    insp = sa.inspect(op.get_bind())
    have = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for name, _typ in COLS:
            if name in have:
                batch.drop_column(name)
