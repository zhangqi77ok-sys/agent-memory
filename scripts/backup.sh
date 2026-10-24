#!/usr/bin/env bash
# agent-memory 数据备份（第十九轮）：pg_dump 快照，不含任何密钥。
#
# 用法（部署机上，compose 服务名与库名与 deploy/docker-compose.yml 一致）：
#   BACKUP_DIR=/opt/agent-memory/backups ./scripts/backup.sh
# 产物：$BACKUP_DIR/agent-memory-YYYYMMDD-HHMMSS.dump
# 恢复：pg_restore -d memory <文件>，然后 alembic upgrade head 核对迁移版本。
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

if docker ps --format '{{.Names}}' | grep -qx "$DB_CONTAINER"; then
    docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$OUT"
else
    # 容器名对不上时退回直连（本机 PG 需 PGPASSWORD 或 trust 认证）
    pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$OUT"
fi

ls -la "$OUT"
echo "backup written: $OUT"
