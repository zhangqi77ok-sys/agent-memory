"""第十九轮：经验 agent 维度 + 证据链标记 + 管理端审计日志。

- memory_insight.agent：经验从哪个 agent 的轨迹提炼出来（episode.source，
  如 codex/mcp/api）。此前跨 agent 的视角差异会被误判为矛盾——裁决时看
  不到这列就分不清“真矛盾”与“不同 agent 的不同做法”。
- memory_insight.evidence_chained：提炼附的证据引文是否链到了具体步骤
  （第十九轮证据链）。旧行默认 False，含义是“只过全文子串校验”。
- admin_audit：管理端每次跨用户查看留痕（密钥只存 SHA-256，不存原文；
  查谁、查什么、什么时候）。此前管理端是共享密钥、无审计——谁看过谁的
  记忆，事后无从查起。

与 0018 同手法：inspect 先查再补，重复跑不报错。admin_audit 是管理端
专用表，应用账号平时不查它；RLS 与授权按 0017 的 reminder 表同款办理。
"""
import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401
from app.models import _id

revision = "0019_agent_evidence_audit"
down_revision = "0018_preference_embedding"


def upgrade():
    insp = sa.inspect(op.get_bind())
    have = {c["name"] for c in insp.get_columns("memory_insight")}
    with op.batch_alter_table("memory_insight") as batch:
        if "agent" not in have:
            batch.add_column(sa.Column("agent", sa.String(64), nullable=True))
        if "evidence_chained" not in have:
            batch.add_column(sa.Column("evidence_chained", sa.Boolean(), nullable=True,
                                       server_default=sa.false()))
    if "admin_audit" not in insp.get_table_names():
        op.create_table(
            "admin_audit",
            sa.Column("id", sa.String(32), primary_key=True, default=_id),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("admin_key_hash", sa.String(64), nullable=False),
            sa.Column("endpoint", sa.String(128), nullable=False),
            sa.Column("target_user", sa.String(128), nullable=True),
        )
        # RLS 与授权只在 PG 上办（0017 同款）；SQLite 没有角色概念。
        if op.get_bind().dialect.name == "postgresql":
            op.execute("ALTER TABLE admin_audit ENABLE ROW LEVEL SECURITY")
            op.execute(
                "DO $$ BEGIN "
                "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_app') "
                "THEN GRANT SELECT, INSERT ON admin_audit TO memsvc_app; END IF; "
                "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_worker') "
                "THEN GRANT SELECT, INSERT ON admin_audit TO memsvc_worker; END IF; "
                "END $$")


def downgrade():
    insp = sa.inspect(op.get_bind())
    have = {c["name"] for c in insp.get_columns("memory_insight")}
    with op.batch_alter_table("memory_insight") as batch:
        if "agent" in have:
            batch.drop_column("agent")
        if "evidence_chained" in have:
            batch.drop_column("evidence_chained")
    if "admin_audit" in insp.get_table_names():
        op.drop_table("admin_audit")
