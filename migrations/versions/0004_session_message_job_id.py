"""session_message.job_id：原文挂到写入任务上，Worker 只抽取这一次提交的消息。

0001 用「当前模型」create_all，新建库上这一列可能已经在。已部署库不会重跑 0001，所以这里补列。
列可空：回滚到旧镜像时，旧代码的 INSERT 不带 job_id 也能写。
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_session_message_job_id"
down_revision = "0003_user_setting"


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("session_message")}
    if "job_id" not in cols:
        op.add_column("session_message", sa.Column("job_id", sa.String(32), nullable=True))
    indexes = {i["name"] for i in insp.get_indexes("session_message")}
    if "ix_session_message_job_id" not in indexes:
        op.create_index("ix_session_message_job_id", "session_message", ["job_id"])


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    indexes = {i["name"] for i in insp.get_indexes("session_message")}
    if "ix_session_message_job_id" in indexes:
        op.drop_index("ix_session_message_job_id", table_name="session_message")
    cols = {c["name"] for c in insp.get_columns("session_message")}
    if "job_id" in cols:
        op.drop_column("session_message", "job_id")
