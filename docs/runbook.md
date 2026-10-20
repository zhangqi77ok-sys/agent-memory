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
`restore.sh` 会先取出**线上库当前的** `forgotten_id`，恢复后合并进恢复库，再执行 `DELETE FROM memory_fact WHERE id IN (SELECT id FROM forgotten_id ...)`，再放流量。
真出事恢复线上库：先停 api/worker（`docker compose -p agent-memory stop api worker`），`./restore.sh <dump>`，脚本会重删、检查 `alembic current`、调 `/readyz`，再 `up -d`。
这样即使备份早于某次遗忘，被遗忘的数据也不会复活（2026-10-20 演练：遗忘前备份→遗忘→恢复，`re-deleted facts: 1`，recall 查不到）。线上库整个丢失时，从审计日志 `audit_log`（action=forget）补回 forgotten_id 再重删。

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

## 7. 数据库账号（initdb 只在空卷时执行）
`deploy/initdb/01-app-role.sh` 只在数据卷第一次初始化时跑。卷已存在（比如从旧版本升级、换了密码）时，
流水线 Deploy 阶段会在迁移前执行 `./ensure-roles.sh`：幂等创建 `memsvc_app`（NOBYPASSRLS）和 `memsvc_worker`（BYPASSRLS），
并把密码对齐成 `.env` 里的 `APP_DB_PASSWORD` / `WORKER_DB_PASSWORD`。手动执行同一个脚本即可。

## 8. 部署/回滚期间的短暂不可用
- **现象**：部署或自动回滚时，`/memory/` 会短暂返回 502。实测 main #4（冒烟失败→自动回滚）期间约 1–2 分钟 502。
- **原因**：
  1. `docker compose up -d` 是**原地重建**：先停旧 api 容器再起新容器，新容器还要跑 `alembic upgrade head` 和启动 uvicorn，这段时间 8090 端口没人监听，nginx 返回 502。worker 依赖 api 健康，会随后重建。
  2. 冒烟最多试 30 次×3 秒（约 90 秒）。如果新版本有问题，这 90 秒里对外一直是坏的；失败后回滚又要再重建一次容器。
- **当前判断**：作品集/演示阶段、单机部署，可以接受；部署尽量放在没人用的时段。
- **以后要零停机**：蓝绿部署。
  - compose 里起两套 api：`api-blue`（127.0.0.1:8090）和 `api-green`（127.0.0.1:8092）。
  - 部署时只起空闲那一套的新版本，在它自己的端口上冒烟；通过后改 nginx `upstream memory_api { server 127.0.0.1:<新端口>; }`，`nginx -t && nginx -s reload`（reload 不断开现有连接），再停旧的那套。
  - 冒烟失败就直接停掉新的那套，nginx 没切换过，所以线上不受影响，也不需要「回滚」。
  - 前提：迁移必须先扩后缩（新旧版本能同时跑在同一个库上）。
- 回滚目标是 web 上线前的 tag（没有对应 web 镜像）时，只回滚 api/worker，web 会被停掉，`/memory/ui/` 返回 502，直到下一次成功部署。
- 网关配置（模板 `deploy/nginx/gateway-memory.conf.example`）在 nginx-gw 里是单文件 bind mount：改完必须 `docker restart nginx-gw`，不要 `nginx -s reload`，否则容器里仍是旧文件。重启期间整个网关（含 `/jenkins/`）会短暂不可用。
