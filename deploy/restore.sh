#!/usr/bin/env bash
# 恢复：pg_restore → 按 forgotten_id 重删（备份里可能还有用户已遗忘的数据）→ alembic 校验 → readyz。
# 用法：restore.sh /srv/backups/agent-memory/memory-xxx.dump [目标库名，默认 memory]
# 演练时传一个临时库名（如 memory_drill），不影响线上库。
set -euo pipefail
cd "$(dirname "$0")"
dump=${1:?usage: restore.sh <dump> [dbname]}
DB=${2:-memory}
C="docker compose -p agent-memory"
if [ "$DB" != memory ]; then
  $C exec -T db psql -U memory -d memory -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"
fi
$C exec -T db pg_restore -U memory -d "$DB" --clean --if-exists --no-owner < "$dump"
$C exec -T db psql -U memory -d "$DB" -v ON_ERROR_STOP=1 -c \
  "DELETE FROM memory_fact WHERE id IN (SELECT id FROM forgotten_id WHERE kind='fact');"
if [ "$DB" = memory ]; then
  $C run --rm --no-deps api alembic current
  P=$(grep ^API_PORT= .env | cut -d= -f2)
  curl -fsS "http://127.0.0.1:${P:-8000}/readyz"; echo
fi
