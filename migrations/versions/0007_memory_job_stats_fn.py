"""/metrics 的任务统计不再受行级权限影响：新增 SECURITY DEFINER 汇总函数 memory_job_stats()。

背景：memory_job 开了 FORCE RLS，策略只放行 user_id = app.user_id 的行。/metrics 用应用账号
memsvc_app（NOBYPASSRLS）且不设置 app.user_id，直接 SELECT memory_job 永远是 0 行，任务积压告警失效。

做法：函数只返回按 status 聚合的数量和最旧 pending 时间，不暴露 user_id / 内容；
函数归 memsvc_worker（BYPASSRLS、非超级用户、非表 owner）所有，以它的身份执行时绕过 RLS；
只把 EXECUTE 授给 memsvc_app / memsvc_worker，PUBLIC 无权调用。RLS 策略本身不变。
"""
from alembic import op

revision = "0007_memory_job_stats_fn"
down_revision = "0006_drop_tenant_id"


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
    CREATE OR REPLACE FUNCTION memory_job_stats()
    RETURNS TABLE (status text, job_count bigint, oldest_created_at timestamptz)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
      SELECT j.status::text, count(*)::bigint, min(j.created_at)
      FROM public.memory_job j
      GROUP BY j.status
    $$""")
    op.execute("REVOKE ALL ON FUNCTION memory_job_stats() FROM PUBLIC")
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_worker') THEN
        ALTER FUNCTION memory_job_stats() OWNER TO memsvc_worker;
        GRANT EXECUTE ON FUNCTION memory_job_stats() TO memsvc_worker;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_app') THEN
        GRANT EXECUTE ON FUNCTION memory_job_stats() TO memsvc_app;
      END IF;
    END $$;""")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP FUNCTION IF EXISTS memory_job_stats()")
