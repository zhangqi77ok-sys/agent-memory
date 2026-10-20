#!/bin/sh
# 首次初始化数据库时创建服务运行账号：非超级用户、非表 owner，受行级权限约束。
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_app') THEN
    CREATE ROLE memsvc_app LOGIN PASSWORD '${APP_DB_PASSWORD:?set APP_DB_PASSWORD}' NOSUPERUSER NOBYPASSRLS;
  END IF;
END \$\$;
SQL
