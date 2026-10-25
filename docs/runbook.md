# 运维手册（agent-memory）

所有命令在部署机上以 `deploy` 用户执行，目录 `/opt/agent-memory`，compose 项目名 `agent-memory`。

## 1. 看状态
```bash
cd /opt/agent-memory
docker compose -p agent-memory ps
docker compose -p agent-memory logs --tail 100 api worker
curl -s 127.0.0.1:8090/readyz
```

## 2. 备份（O2）
- **核实线上定时备份（必需）**：
  必须在部署机上以 `deploy` 用户配置 crontab：
  ```bash
  crontab -e -u deploy
  # 添加如下定时任务（每天凌晨 3 点自动备份）：
  0 3 * * * /opt/agent-memory/backup.sh >> /srv/backups/agent-memory/backup.log 2>&1
  ```
- **检查备份结果**：
  - 检查备份目录：`ls -lh /srv/backups/agent-memory`
  - 必须确认每日均有最新生成的 `memory-<时间>.dump` 文件，且大小大于 0（非空）。
  - 查看执行日志：`tail -n 50 /srv/backups/agent-memory/backup.log`
- **异地第二份备份（防部署机单点故障）**：
  若部署机硬件或磁盘损坏，本地备份将同机丢失。
  - 在 `deploy` 用户的环境配置 `BACKUP_REMOTE=backup_user@远程主机:/srv/backups/agent-memory`。
  - 配置 deploy 用户到该远程主机的 SSH 免密登录：`ssh-copy-id backup_user@远程主机`。
  - `backup.sh` 每次备份成功后会自动 scp 将最新 dump 发送至远端，并自动清理远端超过 7 天的文件。
- **保留周期与隐私保护**：
  本地与异地严格保留 7 天（`KEEP_DAYS=7`）。用户行使「遗忘」后，旧物理备份中尚存该记录；7 天后备份轮换删除才算物理彻底销毁（需与隐私政策对齐）。

## 3. 恢复 + 遗忘重删演练（每月一次，O2）
为了保证备份有效且符合遗忘合规，每月必须在演练库执行一次恢复演练（只用假数据，绝不影响生产库）。仓库内可直接跑的演练（第三十轮）：`scripts/restore_drill.sh`（自带 scratch 集群，不依赖 docker）；下面是部署机上的生产路径写法，语义与演练一致：
```bash
cd /opt/agent-memory
./backup.sh                                                    # 生成最新 dump 文件
LATEST=$(ls -t /srv/backups/agent-memory/memory-*.dump | head -n1)
./restore.sh "$LATEST" memory_drill                            # 恢复到临时演练库 memory_drill
# 验证：演练库中的 memory_fact 绝不能包含已遗忘的数据（查询结果必须为 0）
docker compose -p agent-memory exec -T db psql -U memory -d memory_drill -c \
  "SELECT count(*) FROM memory_fact WHERE id IN (SELECT id FROM forgotten_id);"
# 演练完毕，清理临时库
docker compose -p agent-memory exec -T db psql -U memory -d memory -c "DROP DATABASE memory_drill;"
```
- **真实线上库灾难恢复 SOP**：
  1. 停止业务流量与应用容器：`docker compose -p agent-memory stop api worker`
  2. 选定目标 dump 执行恢复：`./restore.sh /srv/backups/agent-memory/memory-XXX.dump`
     - `restore.sh` 脚本会先取出当前线上库的 `forgotten_id`，恢复后合并到恢复库，并自动执行重删，防止被遗忘的数据意外复活。
     - 脚本随后校验 `alembic current` 以及 `/readyz`。
  3. 恢复应用容器：`docker compose -p agent-memory up -d`
  4. 验证对外服务：`curl -s 127.0.0.1:8090/readyz` 返回 `{"status":"ready"}`。

## 4. 回滚（O3）
### 4.1 自动回滚
部署期间 Smoke 阶段（API `/readyz` 或前端 UI）失败时，流水线 post failure 自动读取 `.last_tag`（上一次冒烟成功的镜像 TAG），重新拉起上一个稳定版本。无需人工介入。

### 4.2 手动回滚方法
- **方式一：Jenkins 参数化触发（推荐）**
  1. 打开 Jenkins 任务页面，选择「Build with Parameters」。
  2. 在 `ROLLBACK_TAG` 中填入目标镜像 TAG（例如 `8ad4cca20259-b24`）。
  3. 勾选 `CONFIRM_ROLLBACK` 进行二次确认（未勾选则构建报错中止，防误触）。
  4. 点击开始构建。流水线将自动跳过代码检查与构建，直接拉取该 TAG 部署并执行冒烟验证。
- **方式二：部署机手动回滚**
  ```bash
  cd /opt/agent-memory
  cat .last_tag  # 查看上一版本 TAG
  IMAGE=ghcr.io/zhangqi77ok-sys/agent-memory TAG=<目标TAG> \
  WEB_IMAGE=ghcr.io/zhangqi77ok-sys/agent-memory-web WEB_TAG=<目标TAG> \
  docker compose -p agent-memory up -d
  curl -s 127.0.0.1:8090/readyz
  ```

### 4.3 核心避坑：回滚至 0006 之前的历史版本（数据库结构降级）
> [!WARNING]
> 迁移 0006 移除了历史标识列（旧列），而 0006 之前的应用代码启动和查询强依赖该历史列。如果直接切回到 0006 之前的镜像，API 和 Worker 会因为缺少该列而启动失败或报错 500！

**操作 SOP（如需回滚到 0006 之前版本）**：
1. 先停止当前应用容器：
   ```bash
   docker compose -p agent-memory stop api worker
   ```
2. **先执行数据库结构降级**：
   ```bash
   docker compose -p agent-memory run --rm --no-deps api alembic downgrade 0005_app_id
   ```
   （`0006_drop_...` 的 downgrade 会自动加回历史标识列并从 `app_id` 回填数据）。
3. 降级成功后，再启动 0006 之前的目标镜像：
   ```bash
   IMAGE=ghcr.io/zhangqi77ok-sys/agent-memory TAG=<目标旧TAG> docker compose -p agent-memory up -d
   ```
4. 验证服务：`curl -s 127.0.0.1:8090/readyz` 确认返回 200。

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
常见原因：`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 缺失或网关不可达（Worker 启动时缺配置会直接退出；运行中连不上则重试后进 dead）。
修复原因后重放：`UPDATE memory_job SET status='pending', attempts=0, last_error=NULL WHERE id='...';`

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

## 9. 监控告警处理（O4）
应用暴露 Prometheus 格式指标端点：`GET /memory/metrics`（公网网关可直接采集，无需服务密钥）。

### 9.1 核心监控告警规则与应急排查
| 告警项 | 阈值规则 | 级别 | 含义与排查步骤 |
|---|---|---|---|
| **服务不可用** | `memory_readyz_status == 0` (持续 1m) | P0 紧急 | 数据库连不上或迁移不一致。<br>排查：`docker compose -p agent-memory logs --tail 100 api`；检查 `docker compose -p agent-memory ps db`。 |
| **任务死信告警** | `memory_job_count{status="dead"} > 0` (持续 5m) | P1 警告 | Worker 抽取重试 3 次后失败。<br>排查：`SELECT id, user_id, last_error FROM memory_job WHERE status='dead';`。检查 `LLM_BASE_URL` 与 `LLM_API_KEY` 是否有效。修复后重放任务。 |
| **任务调度延迟** | `memory_job_oldest_pending_age_seconds > 300` (持续 5m) | P1 警告 | 最旧待处理任务排队超过 5 分钟，说明 Worker 挂掉或任务积压。<br>排查：`docker compose -p agent-memory ps worker`，若退出查看日志；若积压考虑扩容 worker。 |
| **API 5xx 比例过高** | `rate(http_requests_5xx_total[5m]) / rate(http_requests_total[5m]) > 0.05` (持续 2m) | P1 警告 | 服务端内部错误率超过 5%。<br>排查：`docker compose -p agent-memory logs --tail 200 api` 查找异常堆栈与数据库慢查询。 |

