"""第二十二轮：经验衰减结清锚点（遗忘曲线）。

memory_insight.decayed_until：与边的 decayed_until（0020）同哲学——
巡检每次只算锚点后新增的闲置，同一段闲置不重复扣；被用上刷新
last_used_at 后，锚点被新的最后使用时间盖过，宽限重算。
inspect 先查再补，重复跑不报错；既有 RLS 策略按表生效，加列无需新策略。
"""
import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401

revision = "0022_insight_decay_anchor"
down_revision = "0021_sleep_consolidation"


def upgrade():
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("memory_insight")}
    if "decayed_until" not in cols:
        with op.batch_alter_table("memory_insight") as batch:
            batch.add_column(sa.Column("decayed_until",
                                       sa.DateTime(timezone=True), nullable=True))


def downgrade():
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("memory_insight")}
    if "decayed_until" in cols:
        with op.batch_alter_table("memory_insight") as batch:
            batch.drop_column("decayed_until")
