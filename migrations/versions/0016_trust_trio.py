"""第十六轮「信任三件套」：memory_insight 补应用/自动信号/证据校验四列，
新建简报交付记录表 brief_delivery（结果回流的挂钩）。

加列与 0011 同一手法：inspect 先查再补；brief_delivery 与 0015 同一手法：
按当前 models 的子集 create_all，PG 下开 RLS 并给 app/worker 角色授权。
存量经验 verified 默认 true（它们是旧管线产出的，不追溯打假标）；新列计数默认 0。
"""
import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401
from app.db import Base

revision = "0016_trust_trio"
down_revision = "0015_memory_links"

TABLE = "memory_insight"
NEW_TABLE = "brief_delivery"
COLS = [
    ("applied_count", sa.Integer(), "0"),
    ("auto_pos", sa.Integer(), "0"),
    ("auto_neg", sa.Integer(), "0"),
    ("verified", sa.Boolean(), None),  # 默认 true，见下方 UPDATE 口径
]


def upgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    insp = sa.inspect(bind)
    have = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for name, typ, default in COLS:
            if name not in have:
                if default is None:
                    batch.add_column(sa.Column(name, typ, nullable=False, server_default=sa.true()))
                else:
                    batch.add_column(sa.Column(name, typ, nullable=False,
                                               server_default=sa.text(default)))
    op.execute("UPDATE memory_insight SET verified = true WHERE verified IS NULL")
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
    have = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for name, _typ, _d in COLS:
            if name in have:
                batch.drop_column(name)
