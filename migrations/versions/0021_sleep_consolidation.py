"""第二十一轮：睡眠巩固报告表。

consolidation_report：每用户每轮一行（含全零行——零行本身就是共用
强化的水位，下一轮只看本行 run_at 之后的新交付）。与 0020 同手法：
inspect 先查再补，重复跑不报错；RLS 与授权只在 PG 上办。
"""
import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401
from app.models import _id

revision = "0021_sleep_consolidation"
down_revision = "0020_delivery_edge"


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "consolidation_report" not in insp.get_table_names():
        op.create_table(
            "consolidation_report",
            sa.Column("id", sa.String(32), primary_key=True, default=_id),
            sa.Column("app_id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("merged", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("merged_pairs", sa.JSON(), nullable=False,
                      server_default=sa.text("'[]'")),
            sa.Column("pruned", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("pruned_links", sa.JSON(), nullable=False,
                      server_default=sa.text("'[]'")),
            sa.Column("strengthened", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("strengthened_links", sa.JSON(), nullable=False,
                      server_default=sa.text("'[]'")),
            sa.Column("linked", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("linked_ids", sa.JSON(), nullable=False,
                      server_default=sa.text("'[]'")),
            sa.Column("generalized", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("generalized_ids", sa.JSON(), nullable=False,
                      server_default=sa.text("'[]'")),
        )
        op.create_index("ix_consolidation_report_user", "consolidation_report",
                        ["app_id", "user_id"])
        if op.get_bind().dialect.name == "postgresql":
            op.execute("ALTER TABLE consolidation_report ENABLE ROW LEVEL SECURITY")
            op.execute(
                "CREATE POLICY consolidation_report_user_isolation "
                "ON consolidation_report USING ("
                "current_setting('app.user_id', true) IS NULL OR "
                "user_id = current_setting('app.user_id', true))")
            op.execute(
                "DO $$ BEGIN "
                "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_app') "
                "THEN GRANT SELECT, INSERT, UPDATE, DELETE ON consolidation_report "
                "TO memsvc_app; END IF; "
                "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_worker') "
                "THEN GRANT SELECT, INSERT, UPDATE, DELETE ON consolidation_report "
                "TO memsvc_worker; END IF; "
                "END $$")


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "consolidation_report" in insp.get_table_names():
        op.drop_table("consolidation_report")
