#!/usr/bin/env bash
# 恢复：pg_restore → 合并「线上库当前的 forgotten_id」→ 按 forgotten_id 重删 → alembic 校验 → readyz。
# 为什么要合并线上 forgotten_id：备份可能早于某次遗忘，备份里的 forgotten_id 表不含这次遗忘，
# 只用备份自己的表会让已遗忘的数据「复活」。
# 用法：restore.sh <dump> [目标库名，默认 memory]；演练时传临时库名（如 memory_drill），不影响线上库。
set -euo pipefail
cd "$(dirname "$0")"
dump=${1:?usage: restore.sh <dump> [dbname]}
DB=${2:-memory}
C="docker compose -p agent-memory"
PSQL="$C exec -T db psql -U memory -v ON_ERROR_STOP=1 -qAt"
# 1) 先把线上库现有的 forgotten_id 取出来（线上库已损坏/不存在时允许为空）
fid=$($PSQL -d memory -c "COPY (SELECT id, kind, forgotten_at FROM forgotten_id) TO STDOUT WITH CSV" 2>/dev/null || true)
echo "live forgotten_id rows: $(printf '%s' "$fid" | grep -c . || true)"
if [ "$DB" != memory ]; then
  $PSQL -d memory -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"
fi
# 2) 恢复
$C exec -T db pg_restore -U memory -d "$DB" --clean --if-exists --no-owner < "$dump"
echo "facts after restore: $($PSQL -d "$DB" -c 'SELECT count(*) FROM memory_fact')"
# 3) 合并 forgotten_id 并重删
{ echo "CREATE TEMP TABLE f(id text, kind text, at timestamptz); COPY f FROM STDIN WITH CSV;"
  printf '%s\n' "$fid" | grep . || true; echo '\.'
  echo "INSERT INTO forgotten_id(id,kind,forgotten_at) SELECT id,kind,at FROM f ON CONFLICT (id) DO NOTHING;"
} | $PSQL -d "$DB"
echo "re-deleted facts: $($PSQL -d "$DB" -c "WITH d AS (DELETE FROM memory_fact WHERE id IN (SELECT id FROM forgotten_id WHERE kind='fact') RETURNING 1) SELECT count(*) FROM d")"
echo "facts now: $($PSQL -d "$DB" -c 'SELECT count(*) FROM memory_fact')"
if [ "$DB" = memory ]; then
  $C run --rm --no-deps api alembic current
  P=$(grep ^API_PORT= .env | cut -d= -f2)
  curl -fsS "http://127.0.0.1:${P:-8000}/readyz"; echo
fi
