"""行级权限真正生效：FORCE RLS + 不允许“未设置用户=全可见”，并给运行账号授权

0001 的策略在 app.user_id 未设置时放行全部行，且表 owner 默认不受 RLS 约束，等于没开。
运行账号 memsvc_app 需非超级用户（超级用户永远绕过 RLS），由 deploy/initdb 创建。
Worker 通过 app.rls_bypass=on 处理全体任务（v2 拆成独立账号）。
"""
from alembic import op

revision = "0002_force_rls"
down_revision = "0001_init"

TABLES = ("session_message", "memory_fact", "memory_preference", "session_summary", "memory_job")
POLICY = ("NULLIF(current_setting('app.rls_bypass', true), '') = 'on' "
          "OR user_id = NULLIF(current_setting('app.user_id', true), '')")


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    for t in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {t}_user_isolation ON {t}")
        op.execute(f"CREATE POLICY {t}_user_isolation ON {t} USING ({POLICY}) WITH CHECK ({POLICY})")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_app') THEN
        GRANT USAGE ON SCHEMA public TO memsvc_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO memsvc_app;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO memsvc_app;
      END IF;
    END $$;""")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
