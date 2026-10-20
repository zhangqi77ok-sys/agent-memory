# 运维手册（agent-memory）

所有命令在部署机上以 `deploy` 用户执行，目录 `/opt/agent-memory`，compose 项目名 `agent-memory`。

## 1. 看状态
```bash
cd /opt/agent-memory
docker compose -p agent-memory ps
docker compose -p agent-memory logs --tail 100 api worker
curl -s 127.0.0.1:8090/readyz
```

## 2. 备份
- `./backup.sh`：`pg_dump -Fc` 到 `/srv/backups/agent-memory/memory-<时间>.dump`（目录 700、文件 600），保留 7 天。
- 第二份（建议，否则 Jenkins 和备份同机是单点）：`BACKUP_REMOTE=user@另一台:/path ./backup.sh`。
- cron：`0 3 * * * /opt/agent-memory/backup.sh >> /srv/backups/agent-memory/backup.log 2>&1`
- 为什么只保留 7 天：用户「遗忘」后，旧备份里还有这条数据；7 天后备份轮换掉才算彻底删除（隐私政策里要写清楚）。

## 3. 恢复 + 遗忘重删演练（每月一次，只用假数据）
```bash
./backup.sh                                   # 得到 dump 文件
./restore.sh /srv/backups/agent-memory/memory-XXX.dump memory_drill   # 恢复到临时库，不碰线上库
docker compose -p agent-memory exec -T db psql -U memory -d memory_drill -c \
  "SELECT count(*) FROM memory_fact WHERE id IN (SELECT id FROM forgotten_id);"   # 必须是 0
docker compose -p agent-memory exec -T db psql -U memory -d memory -c "DROP DATABASE memory_drill"
```
`restore.sh` 恢复后一定先执行 `DELETE FROM memory_fact WHERE id IN (SELECT id FROM forgotten_id ...)`，再放流量。
真出事恢复线上库：先停 api/worker（`docker compose -p agent-memory stop api worker`），`./restore.sh <dump>`，脚本会重删、检查 `alembic current`、调 `/readyz`，再 `up -d`。
`forgotten_id` 表本身也在备份里；如果恢复的备份比某次遗忘更早，要从最新库/审计日志里把这之后的 forgotten_id 补回来再重删。

## 4. 回滚
见 `docs/deploy.md#回滚`。流水线冒烟失败会自动回滚；手动回滚就是用旧 TAG 再 `compose up -d`。

## 5. 磁盘满
```bash
df -h /; docker system df
docker image prune -af --filter "until=168h"        # 删 7 天前没被使用的镜像（正在用的不会删）
docker builder prune -af --filter "until=168h"
ls -lh /srv/backups/agent-memory | tail             # 备份是否异常变大
docker compose -p agent-memory logs --tail 0        # 日志已限 10m×3，不会无限长
```
**不要**删 `agent-memory_pgdata` 卷，也不要 `docker volume prune`。Postgres 因磁盘满停写后，清出空间它会自己恢复；确认 `/readyz` 200。

## 6. 死信任务
worker 失败 3 次的任务 `status='dead'`。查：`SELECT id,last_error FROM memory_job WHERE status='dead';`
修复原因后重放：`UPDATE memory_job SET status='pending', attempts=0 WHERE id='...';`
