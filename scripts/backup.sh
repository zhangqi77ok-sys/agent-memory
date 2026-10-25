#!/usr/bin/env bash
# agent-memory 数据备份（第十九轮）：pg_dump 快照，不含任何密钥。
#
# 用法（部署机上，compose 服务名与库名与 deploy/docker-compose.yml 一致）：
#   BACKUP_DIR=/opt/agent-memory/backups ./scripts/backup.sh
# 产物：$BACKUP_DIR/agent-memory-YYYYMMDD-HHMMSS.dump
#   + 同名 agent-memory-YYYYMMDD-HHMMSS.tombstones.json（修复第 3 轮：
#     墓碑 sidecar，与库备份分离存放；只含遗忘 id 与内容哈希，不含原文。
#     恢复时 deploy/restore.sh 合并 sidecar，即使活库损坏墓碑也不丢）
# 恢复：deploy/restore.sh <dump> [目标库] [sidecar]，然后 alembic
# upgrade head 核对迁移版本。恢复脚本会按墓碑重删五类已遗忘数据。
# 注意：只备数据不备 .env；数据库三个密码只在首次建卷时生效，恢复后改密码
# 要在库里 ALTER ROLE（见 docs/deploy.md）。
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
DB_CONTAINER="${DB_CONTAINER:-agent-memory-db-1}"
DB_NAME="${DB_NAME:-memory}"
DB_USER="${DB_USER:-memory}"
mkdir -p "$BACKUP_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/agent-memory-$STAMP.dump"
SIDECAR="$BACKUP_DIR/agent-memory-$STAMP.tombstones.json"

if docker ps --format '{{.Names}}' | grep -qx "$DB_CONTAINER"; then
    docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$OUT"
    BACKUP_PSQL="docker exec $DB_CONTAINER psql -U $DB_USER -d $DB_NAME -v ON_ERROR_STOP=1 -qAt"
else
    # 容器名对不上时退回直连（本机 PG 需 PGPASSWORD 或 trust 认证；
    # 直连演练用 PGHOST/PGPORT 环境变量指到目标集群）
    pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$OUT"
    BACKUP_PSQL="psql -U $DB_USER -d $DB_NAME -v ON_ERROR_STOP=1 -qAt"
fi

# 墓碑 sidecar：与 dump 分离，活库损坏时仍可凭它重删。内容墓碑表
# 不存在的老库（0024 前）只导 id 段，不中断备份。
FID_JSON=$($BACKUP_PSQL -c "SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json) FROM (SELECT id, kind, forgotten_at FROM forgotten_id) t" 2>/dev/null || echo '[]')
if [ -n "$($BACKUP_PSQL -tAc "SELECT to_regclass('public.forgotten_content')" 2>/dev/null || true)" ]; then
    FC_JSON=$($BACKUP_PSQL -c "SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json) FROM (SELECT app_id, user_id, kind, content_hash, forgotten_at FROM forgotten_content) t" 2>/dev/null || echo '[]')
else
    echo "note: no forgotten_content table (pre-0024 DB), sidecar carries id tombstones only"
    FC_JSON='[]'
fi
printf '{"version":1,"exported_at":"%s","forgotten_id":%s,"forgotten_content":%s}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$FID_JSON" "$FC_JSON" > "$SIDECAR"

ls -la "$OUT" "$SIDECAR"
echo "backup written: $OUT"
echo "sidecar written: $SIDECAR"
