"""user_setting：记忆授权同意与总开关；同样 FORCE RLS。"""
import sqlalchemy as sa
from alembic import op

revision = "0003_user_setting"
down_revision = "0002_force_rls"

POLICY = "user_id = NULLIF(current_setting('app.user_id', true), '')"


def upgrade():
    op.create_table(
        "user_setting",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("memory_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("consent_version", sa.String(32)),
        sa.Column("consent_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_user_setting"),
    )
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE user_setting ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY user_setting_user_isolation ON user_setting USING ({POLICY}) WITH CHECK ({POLICY})")
    op.execute("ALTER TABLE user_setting FORCE ROW LEVEL SECURITY")
    for role in ("memsvc_app", "memsvc_worker"):
        op.execute(f"""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON user_setting TO {role}; END IF; END $$;""")


def downgrade():
    op.drop_table("user_setting")
