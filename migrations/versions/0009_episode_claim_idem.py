"""task_episode 补两列：locked_until（领取锁定，与 memory_job 同构）与 idempotency_key（提交防重）。

与 0004 同一手法：inspect 先查再补，已建好的库（0008 用当前模型建表）可能已有部分结构。
唯一约束 (app_id, user_id, idempotency_key)：键为空的行不参与防重（PG/SQLite 的唯一约束都不约束 NULL）。
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_episode_claim_idem"
down_revision = "0008_episodes_insights"

TABLE = "task_episode"
UQ = "uq_episode_idem_app"


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        if "locked_until" not in cols:
            batch.add_column(sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
        if "idempotency_key" not in cols:
            batch.add_column(sa.Column("idempotency_key", sa.String(128), nullable=True))
    uqs = {u["name"] for u in insp.get_unique_constraints(TABLE)}
    if UQ not in uqs:
        op.create_unique_constraint(UQ, TABLE, ["app_id", "user_id", "idempotency_key"])


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    uqs = {u["name"] for u in insp.get_unique_constraints(TABLE)}
    if UQ in uqs:
        op.drop_constraint(UQ, TABLE, type_="unique")
    cols = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        if "idempotency_key" in cols:
            batch.drop_column("idempotency_key")
        if "locked_until" in cols:
            batch.drop_column("locked_until")
