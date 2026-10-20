#!/usr/bin/env bash
# pg_dump -Fc 备份：本机 /srv/backups/agent-memory 保留 7 天（= 遗忘的数据最多 7 天后在备份里彻底消失）；
# 可选第二份：设置 BACKUP_REMOTE=user@host:/path（scp 目标），远端同样只留 7 天。
# 用法（cron，deploy 用户）：0 3 * * * /opt/agent-memory/backup.sh >> /srv/backups/agent-memory/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"
BACKUP_DIR=${BACKUP_DIR:-/srv/backups/agent-memory}
KEEP_DAYS=${KEEP_DAYS:-7}
mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
ts=$(date +%Y%m%d-%H%M%S)
f="$BACKUP_DIR/memory-$ts.dump"
docker compose -p agent-memory exec -T db pg_dump -U memory -Fc memory > "$f.part"
mv "$f.part" "$f"; chmod 600 "$f"
[ -s "$f" ] || { echo "备份为空: $f" >&2; exit 1; }
echo "local backup: $f ($(du -h "$f" | cut -f1))"
find "$BACKUP_DIR" -name 'memory-*.dump' -mtime +"$KEEP_DAYS" -delete
if [ -n "${BACKUP_REMOTE:-}" ]; then
  scp -q "$f" "$BACKUP_REMOTE/"
  ssh "${BACKUP_REMOTE%%:*}" "find '${BACKUP_REMOTE#*:}' -name 'memory-*.dump' -mtime +$KEEP_DAYS -delete"
  echo "remote copy: $BACKUP_REMOTE"
fi
