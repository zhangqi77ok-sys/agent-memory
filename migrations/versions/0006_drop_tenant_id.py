"""删除 tenant_id（双写过渡第二步）：旧索引/约束/列全部移除，只留 app_id。

downgrade 会把列加回并从 app_id 回填，便于回滚到 PR1 镜像。
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_drop_tenant_id"
down_revision = "0005_app_id"

TABLES = ("session_message", "memory_fact", "memory_preference", "session_summary", "memory_job",
          "audit_log", "user_setting")
OLD_UNIQUES = {"memory_job": ("uq_job_idem", ["tenant_id", "user_id", "idempotency_key"]),
               "user_setting": ("uq_user_setting", ["tenant_id", "user_id"])}


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_fact_active")
    if "ix_fact_user_subject" in {i["name"] for i in insp.get_indexes("memory_fact")}:
        op.drop_index("ix_fact_user_subject", table_name="memory_fact")
    for t, (name, _) in OLD_UNIQUES.items():
        if name in {u["name"] for u in insp.get_unique_constraints(t)}:
            with op.batch_alter_table(t) as b:
                b.drop_constraint(name, type_="unique")
    for t in TABLES:
        if "tenant_id" in {c["name"] for c in insp.get_columns(t)}:
            with op.batch_alter_table(t) as b:
                b.drop_column("tenant_id")


def downgrade():
    for t in TABLES:
        op.add_column(t, sa.Column("tenant_id", sa.String(64), nullable=True))
        op.execute(f"UPDATE {t} SET tenant_id = app_id")
    op.create_index("ix_fact_user_subject", "memory_fact", ["tenant_id", "user_id", "subject"])
    for t, (name, cols) in OLD_UNIQUES.items():
        with op.batch_alter_table(t) as b:
            b.create_unique_constraint(name, cols)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE INDEX IF NOT EXISTS ix_fact_active ON memory_fact (tenant_id, user_id) WHERE valid_to IS NULL")
