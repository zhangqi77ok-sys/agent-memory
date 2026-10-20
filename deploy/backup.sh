#!/usr/bin/env bash
# 每日 pg_dump -Fc 备份到 GCP 练习机，保留 7 天（= 遗忘最长 7 天后在备份中彻底消失）。
# 用法（cron）：0 3 * * * /opt/agent-memory/deploy/backup.sh
set -euo pipefail
: "${BACKUP_HOST:?}" "${BACKUP_DIR:=/backups/agent-memory}"
ts=$(date +%Y%m%d-%H%M)
f="/tmp/memory-$ts.dump"
docker compose -f "$(dirname "$0")/docker-compose.yml" exec -T db pg_dump -U memory -Fc memory > "$f"
scp -q "$f" "$BACKUP_HOST:$BACKUP_DIR/"
ssh "$BACKUP_HOST" "find $BACKUP_DIR -name 'memory-*.dump' -mtime +7 -delete"
rm -f "$f"
