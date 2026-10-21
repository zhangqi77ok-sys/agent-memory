"""app_id：tenant_id 的新名字（双写过渡第一步）。

加列（server_default='tiancode'，旧镜像回滚后 INSERT 不带 app_id 也能写）→ 回填 → NOT NULL → 并行索引/约束。
旧 tenant_id 与旧索引保留，0006 再删。0001 用当前模型 create_all，新库上部分对象已在，所以全部按存在性判断。
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_app_id"
down_revision = "0004_session_message_job_id"

TABLES = ("session_message", "memory_fact", "memory_preference", "session_summary", "memory_job",
          "audit_log", "user_setting")
UNIQUES = {"memory_job": ("uq_job_idem_app", ["app_id", "user_id", "idempotency_key"]),
           "user_setting": ("uq_user_setting_app", ["app_id", "user_id"])}


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for t in TABLES:
        cols = {c["name"] for c in insp.get_columns(t)}
        if "app_id" not in cols:
            op.add_column(t, sa.Column("app_id", sa.String(64), nullable=True, server_default="tiancode"))
        # 0001 用当前模型 create_all。PR2 模型不再有 tenant_id 时，全新库没有这一列，不能回填。
        if "tenant_id" in cols:
            op.execute(f"UPDATE {t} SET app_id = tenant_id WHERE tenant_id IS NOT NULL")
        with op.batch_alter_table(t) as b:  # batch：SQLite 也能 ALTER NOT NULL
            b.alter_column("app_id", existing_type=sa.String(64), nullable=False, server_default="tiancode")
    if "ix_fact_app_user_subject" not in {i["name"] for i in insp.get_indexes("memory_fact")}:
        op.create_index("ix_fact_app_user_subject", "memory_fact", ["app_id", "user_id", "subject"])
    for t, (name, cols) in UNIQUES.items():
        if name not in {u["name"] for u in insp.get_unique_constraints(t)}:
            with op.batch_alter_table(t) as b:
                b.create_unique_constraint(name, cols)
    if bind.dialect.name == "postgresql":
        op.execute("CREATE INDEX IF NOT EXISTS ix_fact_active_app ON memory_fact (app_id, user_id) "
                   "WHERE valid_to IS NULL")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_fact_active_app")
    for t, (name, _) in UNIQUES.items():
        with op.batch_alter_table(t) as b:
            b.drop_constraint(name, type_="unique")
    op.drop_index("ix_fact_app_user_subject", table_name="memory_fact")
    for t in TABLES:
        with op.batch_alter_table(t) as b:
            b.drop_column("app_id")
