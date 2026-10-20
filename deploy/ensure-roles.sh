#!/usr/bin/env bash
# 幂等创建/对齐数据库运行账号。initdb/ 只在数据卷为空时执行；卷已存在时由流水线 Deploy 阶段在迁移前调用本脚本。
# 密码取自 db 容器环境（来自 .env），每次都 ALTER 成 .env 里的值，保证两边一致。
set -euo pipefail
cd "$(dirname "$0")"
docker compose -p agent-memory exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v app_pw="$APP_DB_PASSWORD" -v worker_pw="$WORKER_DB_PASSWORD"' <<'SQL'
SELECT 'CREATE ROLE memsvc_app LOGIN NOSUPERUSER NOBYPASSRLS' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='memsvc_app') \gexec
SELECT 'CREATE ROLE memsvc_worker LOGIN NOSUPERUSER BYPASSRLS' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='memsvc_worker') \gexec
ALTER ROLE memsvc_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'app_pw';
ALTER ROLE memsvc_worker LOGIN NOSUPERUSER BYPASSRLS PASSWORD :'worker_pw';
SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname LIKE 'memsvc_%' ORDER BY 1;
SQL
