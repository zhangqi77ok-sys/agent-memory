"""第 33 轮：经验动作序列倒排（类比召回）。

建 memory_insight_move（insight_id, position, move）+ (move, insight_id)
索引：探针按动作取 posting 后在 Python 侧按位置拼回序列算 LCS。
模型层（MemoryInsightMove.__table_args__）已声明同名索引，测试的
create_all 直接带上；本迁移只给存量库补。存量行回填不在迁移里做
（动作提取是 Python 词表，SQL 算不出）：升级后跑
scripts/rebuild_moves.py 一次；未回填的旧行类比探针看不见，
不报错、不错召。
"""

import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401

revision = "0026_insight_move"
down_revision = "0025_link_current_unique"


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "memory_insight_move" not in insp.get_table_names():
        op.create_table(
            "memory_insight_move",
            sa.Column("insight_id", sa.String(32), nullable=False),
            sa.Column("position", sa.Integer, nullable=False),
            sa.Column("move", sa.String(32), nullable=False),
            sa.Column("app_id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.PrimaryKeyConstraint("insight_id", "position"),
        )
    idx_names = {ix["name"] for ix in insp.get_indexes("memory_insight_move")}
    if "ix_insight_move_move" not in idx_names:
        op.create_index("ix_insight_move_move", "memory_insight_move",
                        ["move", "insight_id"])


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "memory_insight_move" in insp.get_table_names():
        idx_names = {ix["name"] for ix in insp.get_indexes("memory_insight_move")}
        if "ix_insight_move_move" in idx_names:
            op.drop_index("ix_insight_move_move", table_name="memory_insight_move")
        op.drop_table("memory_insight_move")
