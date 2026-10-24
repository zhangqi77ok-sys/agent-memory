"""第二十三轮：事实/偏好反馈计数列。

memory_fact 与 memory_preference 各加 feedback_pos / feedback_neg
（Integer，默认 0），供事实级反馈 API 记人工好评/差评；置信度重算见
worker.fact_confidence（无反馈时恒等于旧 support_confidence，旧行数值
不动）。inspect 先查再补，重复跑不报错；既有 RLS 策略按表生效。
"""
import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401

revision = "0023_fact_feedback_counts"
down_revision = "0022_insight_decay_anchor"


def _add_columns(table: str):
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns(table)}
    with op.batch_alter_table(table) as batch:
        if "feedback_pos" not in cols:
            batch.add_column(sa.Column("feedback_pos", sa.Integer(),
                                       nullable=False, server_default=sa.text("0")))
        if "feedback_neg" not in cols:
            batch.add_column(sa.Column("feedback_neg", sa.Integer(),
                                       nullable=False, server_default=sa.text("0")))


def upgrade():
    _add_columns("memory_fact")
    _add_columns("memory_preference")


def downgrade():
    for table in ("memory_fact", "memory_preference"):
        insp = sa.inspect(op.get_bind())
        cols = {c["name"] for c in insp.get_columns(table)}
        with op.batch_alter_table(table) as batch:
            if "feedback_pos" in cols:
                batch.drop_column("feedback_pos")
            if "feedback_neg" in cols:
                batch.drop_column("feedback_neg")
