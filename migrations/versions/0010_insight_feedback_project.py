"""第十轮：反馈闭环与项目作用域的列。

task_episode.project（轨迹的来源项目/仓库）；memory_insight 补 project（继承来源轨迹）、
confidence / use_count / success_count / feedback_pos / feedback_neg / last_used_at。
与 0009 同一手法：inspect 先查再补（0008 用当前模型建表的库可能已有部分结构）。
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_insight_feedback_project"
down_revision = "0009_episode_claim_idem"

EP_COLS = [("project", sa.String(128), True)]
IN_COLS = [
    ("project", sa.String(128), True),
    ("confidence", sa.Float(), False),
    ("use_count", sa.Integer(), False),
    ("success_count", sa.Integer(), False),
    ("feedback_pos", sa.Integer(), False),
    ("feedback_neg", sa.Integer(), False),
    ("last_used_at", sa.DateTime(timezone=True), True),
]


def _add_missing(table: str, cols: list) -> None:
    insp = sa.inspect(op.get_bind())
    have = {c["name"] for c in insp.get_columns(table)}
    with op.batch_alter_table(table) as batch:
        for name, typ, nullable in cols:
            if name not in have:
                batch.add_column(sa.Column(name, typ, nullable=nullable))


def upgrade():
    _add_missing("task_episode", EP_COLS)
    _add_missing("memory_insight", IN_COLS)
    # 存量经验的置信度回填中性先验（新建行靠模型默认值，存量行靠这句）
    op.execute("UPDATE memory_insight SET confidence = 0.5 WHERE confidence IS NULL")


def downgrade():
    for table, cols in (("memory_insight", IN_COLS), ("task_episode", EP_COLS)):
        insp = sa.inspect(op.get_bind())
        have = {c["name"] for c in insp.get_columns(table)}
        with op.batch_alter_table(table) as batch:
            for name, _typ, _nullable in cols:
                if name in have:
                    batch.drop_column(name)
