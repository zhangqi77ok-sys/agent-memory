#!/usr/bin/env bash
# 恢复演练：pg_restore → 按 forgotten_id 重删 → alembic 校验 → 再放流量。
set -euo pipefail
dump=${1:?usage: restore.sh memory-YYYYmmdd-HHMM.dump}
C="docker compose -f $(dirname "$0")/docker-compose.yml"
$C exec -T db pg_restore -U memory -d memory --clean --if-exists < "$dump"
$C exec -T db psql -U memory -d memory -v ON_ERROR_STOP=1 -c \
  "DELETE FROM memory_fact WHERE id IN (SELECT id FROM forgotten_id WHERE kind='fact');"
$C run --rm api alembic current
curl -fsS http://127.0.0.1:8000/readyz
