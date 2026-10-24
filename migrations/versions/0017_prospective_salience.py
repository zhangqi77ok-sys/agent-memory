"""第十七轮：类人记忆补全——事实/偏好佐证置信度、episode 显著性与骨架化标记、
新建前瞻记忆表 memory_reminder。

加列与 0011/0016 同一手法（inspect 先查再补）；新表与 0015 同一手法：
按当前 models 的子集 create_all，PG 下开 RLS 并给 app/worker 角色授权。
存量事实/偏好 support_count=1、confidence=0.5（单次来源的中性口径）；
存量 episode salience=0（领取排序时同分按 created_at，等同旧 FIFO，提炼后不再回补）。
"""
import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401
from app.db import Base

revision = "0017_prospective_salience"
down_revision = "0016_trust_trio"

ADD_COLS = {
    "memory_fact": [("confidence", sa.Float(), "0.5"), ("support_count", sa.Integer(), "1")],
    "memory_preference": [("confidence", sa.Float(), "0.5"), ("support_count", sa.Integer(), "1")],
    "task_episode": [("salience", sa.Float(), "0"), ("skeletonized", sa.Boolean(), None)],
}
NEW_TABLE = "memory_reminder"


def upgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    insp = sa.inspect(bind)
    for table, cols in ADD_COLS.items():
        have = {c["name"] for c in insp.get_columns(table)}
        with op.batch_alter_table(table) as batch:
            for name, typ, default in cols:
                if name in have:
                    continue
                if default is None:
                    batch.add_column(sa.Column(name, typ, nullable=False, server_default=sa.false()))
                else:
                    batch.add_column(sa.Column(name, typ, nullable=False,
                                               server_default=sa.text(default)))
    Base.metadata.create_all(bind, tables=[Base.metadata.tables[NEW_TABLE]])
    if is_pg:
        op.execute(f"ALTER TABLE {NEW_TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {NEW_TABLE}_user_isolation ON {NEW_TABLE} USING ("
            f"current_setting('app.user_id', true) IS NULL OR user_id = current_setting('app.user_id', true))"
        )
        for role in ("memsvc_app", "memsvc_worker"):
            op.execute(
                f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
                f"THEN GRANT SELECT, INSERT, UPDATE, DELETE ON {NEW_TABLE} TO {role}; END IF; END $$"
            )


def downgrade():
    op.drop_table(NEW_TABLE)
    insp = sa.inspect(op.get_bind())
    for table, cols in ADD_COLS.items():
        have = {c["name"] for c in insp.get_columns(table)}
        with op.batch_alter_table(table) as batch:
            for name, _typ, _d in cols:
                if name in have:
                    batch.drop_column(name)
