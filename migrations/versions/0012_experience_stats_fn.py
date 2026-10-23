"""/metrics 的经验侧统计不再受行级权限影响：新增 SECURITY DEFINER 汇总函数 experience_stats()。

背景同 0007：task_episode / memory_insight / audit_log 都开了 RLS（0008），应用账号
memsvc_app 不带 app.user_id 直查永远是 0 行——待提炼积压、简报命中率这类告警会静默失效。

做法：函数只返回聚合行 (metric, label, val)：episode 按 distill_status 计数、
insights_active 现行经验数、brief_total / brief_hits 简报请求与命中数（按审计表）。
不暴露 user_id 与任何内容；所有权与授权同 0007（归 memsvc_worker，EXECUTE 只给两个服务账号）。
"""
from alembic import op

revision = "0012_experience_stats_fn"
down_revision = "0011_insight_pin_mute"


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
    CREATE OR REPLACE FUNCTION experience_stats()
    RETURNS TABLE (metric text, label text, val bigint)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
      SELECT 'episode'::text, e.distill_status::text, count(*)::bigint
        FROM public.task_episode e GROUP BY e.distill_status
      UNION ALL
      SELECT 'insights_active', '', count(*)::bigint
        FROM public.memory_insight i WHERE i.valid_to IS NULL
      UNION ALL
      SELECT 'brief_total', '', count(*)::bigint
        FROM public.audit_log a WHERE a.action = 'brief'
      UNION ALL
      SELECT 'brief_hits', '', count(*)::bigint
        FROM public.audit_log a WHERE a.action = 'brief'
         AND a.fact_ids IS NOT NULL AND a.fact_ids::text NOT IN ('[]', '')
    $$""")
    op.execute("REVOKE ALL ON FUNCTION experience_stats() FROM PUBLIC")
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_worker') THEN
        ALTER FUNCTION experience_stats() OWNER TO memsvc_worker;
        GRANT EXECUTE ON FUNCTION experience_stats() TO memsvc_worker;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_app') THEN
        GRANT EXECUTE ON FUNCTION experience_stats() TO memsvc_app;
      END IF;
    END $$;""")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP FUNCTION IF EXISTS experience_stats()")
