#!/bin/sh
# 首次初始化数据库时创建服务运行账号：非超级用户、非表 owner，受行级权限约束。
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_app') THEN
    CREATE ROLE memsvc_app LOGIN PASSWORD '${APP_DB_PASSWORD:?set APP_DB_PASSWORD}' NOSUPERUSER NOBYPASSRLS;
  END IF;
  -- Worker 账号：BYPASSRLS 但非超级用户；绕过权限由账号属性决定，应用会话无法自行开启
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_worker') THEN
    CREATE ROLE memsvc_worker LOGIN PASSWORD '${WORKER_DB_PASSWORD:?set WORKER_DB_PASSWORD}' NOSUPERUSER BYPASSRLS;
  END IF;
END \$\$;
SQL
