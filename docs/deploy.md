# 部署说明（Jenkins → 部署机）

> 公开仓库：这里不写真实 IP、密码、Token。部署机地址只配在 Jenkins 全局环境变量 `DEPLOY_HOST` 里。

## 拓扑
```
浏览器 ──:80──> nginx-gw（宿主机网络）
                 ├─ /           → 原有服务（不动）
                 ├─ /jenkins/   → Jenkins 容器 127.0.0.1:8088
                 ├─ /memory     → 301 /memory/
                 ├─ /memory/ui/ → 前端 web 容器 127.0.0.1:8091（原样转发，不去前缀）
                 └─ /memory/    → agent-memory api 127.0.0.1:8090（去掉 /memory 前缀）
Jenkins 容器 ──docker.sock──> 宿主 Docker（构建镜像、CI 临时 Postgres）
Jenkins 容器 ──SSH deploy@网关──> 宿主 /opt/agent-memory（docker compose -p agent-memory）
```
Jenkins 和应用在**同一台机器**。部署走 SSH 登录 `deploy` 用户而不是直接用 docker.sock 跑 compose，原因：
compose 里的 `./initdb` 等相对路径必须是**宿主机路径**，Jenkins 容器里的工作目录宿主看不到；而且以后部署机换成另一台时，流水线不用改，只改 `DEPLOY_HOST`。

## Jenkins 任务
| 任务 | 类型 | 用途 |
|---|---|---|
| `agent-memory-mb` | 多分支流水线（Multibranch） | 持续部署：所有分支/PR 分支跑 扫描→lint/测试→Postgres 测试→构建镜像；只有 `main` 继续 推 GHCR→部署→冒烟→失败回滚 |
| `agent-memory` | 普通流水线，固定分支 | 验收/演练用，可手动传 `DEPLOY_BRANCH`、`SMOKE_PATH` |

触发：GitHub webhook（push 事件）→ `/jenkins/git/notifyCommit?url=<仓库地址>&token=<git 插件访问令牌>`，令牌存在 Jenkins 和 GitHub webhook 配置里，不进仓库；兜底 `pollSCM('H/5 * * * *')` 和多分支每 5 分钟扫描一次。

## 流水线每个阶段
| 阶段 | 做什么 | 失败意味着 |
|---|---|---|
| Init | 计算分支名、`TAG=<commit 前 12 位>-b<构建号>`、是否部署（分支 == 参数 `DEPLOY_BRANCH`，默认 `main`） | `DEPLOY_HOST` 未配置 |
| Secrets scan | gitleaks 扫全仓库（`--volumes-from` 共享 Jenkins 工作目录） | 有密钥进了仓库，**先轮换密钥**再改历史 |
| Lint & Test | ruff + pytest（SQLite），JUnit 报告不允许为空 | 代码问题 |
| Test on Postgres | 在 `jenkins_default` 网络里临时起 `pgvector/pgvector:pg16`，跑全部测试；门禁：tests>0 且 skipped=0 | RLS/索引/SKIP LOCKED 有问题 |
| Build image | `docker build`，所有分支都跑 | Dockerfile 问题 |
| Push GHCR | 用凭据 `ghcr-token` 推 `ghcr.io/zhangqi77ok-sys/agent-memory:<TAG>` 和 `:latest`；只在部署分支运行；凭据缺失或 Token 过期**直接失败**，不会部署本地镜像（非部署分支不跑这一步） | Token 过期/权限不够：main 构建会变红，到 GitHub 重新生成 PAT 并更新凭据 `ghcr-token` |
| Deploy | （GHCR 包是私有的）部署机用 `ghcr-token` 执行 `docker login ghcr.io`，本机没有该镜像就 pull；scp compose、initdb、backup/restore、`.env`（来自凭据 `deploy-env`）到 `/opt/agent-memory`，写 `.last_tag.new`，`docker compose -p agent-memory up -d` | 看 `docker compose -p agent-memory logs api` |
| Smoke | 部署机上 `curl 127.0.0.1:$API_PORT$SMOKE_PATH`，最多 30 次×3 秒要 200 | 成功：`.last_tag.new → .last_tag`；失败：自动回滚 |

## 凭据（只写 ID）
| ID | 类型 | 内容 |
|---|---|---|
| `ghcr-token` | 用户名+密码 | GitHub 用户名 + 只有 `write:packages` 的 PAT（maxzq 自己生成并填写） |
| `deploy-ssh` | SSH 私钥 | 部署机 `deploy` 用户的专用密钥（只给 Jenkins 用） |
| `deploy-env` | Secret file | `/opt/agent-memory/.env` 全文 |

Jenkins 全局环境变量：`DEPLOY_HOST`（Jenkins 容器访问宿主用 docker 网络网关地址）、`DEPLOY_USER=deploy`。

## `.env` 变量（模板见 `deploy/.env.example`）
| 变量 | 含义 |
|---|---|
| `ENV` / `APP_ENV` | `prod`。`APP_ID` 必须是 1-64 位小写字母、数字、`-` 或 `_` |
| `POSTGRES_PASSWORD` | 表 owner 账号 `memory` 的密码，只用于迁移和备份 |
| `APP_DB_PASSWORD` | api 运行账号 `memsvc_app`（非 owner、受 FORCE RLS 约束） |
| `WORKER_DB_PASSWORD` | worker 账号 `memsvc_worker`（BYPASSRLS、非超级用户） |
| `APP_ID` | 固定应用标识，默认 `tiancode`。调用方不能切换 |
| `API_BIND` / `API_PORT` | api 映射到宿主的地址和端口，默认 `127.0.0.1:8090`，不对公网直接开放 |
| `ROOT_PATH` | 反向代理前缀 `/memory`，传给 `uvicorn --root-path`，Swagger 才能加载正确的 `openapi.json` |
| `DOCS_ENABLED` | Swagger UI 开关。默认 prod 关闭；演示部署设 `true` 打开 `/memory/docs`。正式上线前改回 `false` |
| `SERVICE_KEYS` | 业务调用方服务密钥，支持逗号分隔多个值以便轮换。业务接口必须在请求头带 `X-Service-Key` |
| `RATE_LIMIT_ENABLED` | 是否开启应用层限流，默认 `true` |
| `RATE_LIMIT_READ_PER_MIN` | 读接口按 `user_id` 每分钟限流阈值，默认 `120` |
| `RATE_LIMIT_WRITE_PER_MIN` | 写/导出/删除接口按 `user_id` 每分钟限流阈值（限得更严），默认 `30` |
| `LLM_BASE_URL` | Worker 抽取用的 OpenAI 兼容地址，要以 `/v1` 结尾。示例 `http://host.docker.internal:8080/v1`（容器访问宿主机上的网关）。不要写真实 IP |
| `LLM_API_KEY` | 该网关的密钥。只放在 `deploy-env` / 部署机 `.env`，不进仓库。缺了 Worker 会直接退出，不会改用假抽取器 |
| `LLM_MODEL` | 聊天补全的模型名，例如部署侧实际加载的模型 id |

Worker 容器有 `extra_hosts: ["host.docker.internal:host-gateway"]`，所以上面的示例地址在 compose 里能解析到宿主机。这三项由 api/worker 的 `env_file: .env` 注入，流水线不用另加 Jenkins 全局变量；改的是凭据 `deploy-env`（下次部署会覆盖部署机上的 `.env`）。缺任何一项时 Worker 启动失败，已入队的任务不会被假数据写进记忆。

改 `.env`：同时改部署机文件和 Jenkins 凭据 `deploy-env`（下次部署会用凭据覆盖部署机文件）。**注意**：数据库三个密码只在第一次初始化数据卷时生效，之后改要在库里 `ALTER ROLE ... PASSWORD`。

## 回滚
- 自动：Smoke 失败 → 读 `.last_tag`（上一次冒烟成功的镜像）→ `compose up -d` → 再确认 `/readyz` 200。没有 `.last_tag` 时报错，不瞎回滚。
- 手动（Jenkins 参数化）：在 Jenkins 界面传入 `ROLLBACK_TAG=<目标tag>` 并勾选 `CONFIRM_ROLLBACK` 进行二次确认。流水线将跳过测试与构建，直接拉取该镜像发布并冒烟。
- 手动（部署机命令行）：
  ```bash
  cd /opt/agent-memory && cat .last_tag            # 例：ghcr.io/zhangqi77ok-sys/agent-memory:<tag>
  IMAGE=ghcr.io/zhangqi77ok-sys/agent-memory TAG=<要回到的tag> docker compose -p agent-memory up -d
  curl -s 127.0.0.1:8090/readyz
  ```
- **注意（迁移降级陷阱）**：通常情况下迁移遵循「先扩后缩」，回滚镜像不需要回滚数据库。但若回滚到 **0006 之前的历史版本**，因迁移 0006 删除了历史标识列（旧列），而旧版代码强依赖该列，切镜像前**必须先在部署机执行 `alembic downgrade 0005_app_id`**，再启动旧镜像（详见 `docs/runbook.md`）。


## 前端
- 访问路径：`/memory/ui/`，nginx-gw 里 `location /memory/ui/ { proxy_pass http://127.0.0.1:8091; }`（**末尾不带斜杠**，必须写在 `/memory/` 之前）。裸路径 `location = /memory` 返回 301 `/memory/`。两条反代都有 `proxy_http_version 1.1` 和 `proxy_read_timeout 60s`；`/memory/` 另有 `client_max_body_size 50m`。模板见 `deploy/nginx/gateway-memory.conf.example`。
- 该模板在 nginx-gw 里是单文件 bind mount。改完这一份文件后执行 `docker restart nginx-gw`，不要 `nginx -s reload`，否则容器里仍是旧文件。重启期间整个网关（含 `/jenkins/`）会短暂不可用。
- 前端：仓库 `web/`（Vue3），Jenkins `Frontend` 阶段跑 `npm ci && npm test && npm run build`；镜像 `ghcr.io/zhangqi77ok-sys/agent-memory-web`，compose 服务名 `web`，默认绑定 `127.0.0.1:8091`。外层 Nginx 把 `/memory/ui/` 转到该容器（由运维配置）。和 api 共用同一次部署的 TAG、同一次回滚。
- 冒烟（`deploy/smoke-ui.sh`，部署机上执行）：经网关请求 `SMOKE_UI_URL`（Jenkins 参数，默认 `http://127.0.0.1/memory/ui/`），要求 200、HTML 含「我的记忆」、页面里的一个 `assets/*.js` 也返回 200；不直连 8091。任一失败即自动回滚。网关不在本机 80 端口时改这个参数。api 仍直连 `127.0.0.1:$API_PORT/readyz`。
- 前端调接口用相对路径 `/memory/v1/...`，同源，不需要 CORS。

## 部署去重（同一提交不重复部署）
- `Dedup check` 阶段在 Deploy 之前执行（只在部署分支）：SSH 到部署机读 `.last_tag`，取 tag 里的提交前缀（格式 `<commit 前 12 位>-b<构建号>`，如 `9de91ec9cc27-b15`，比较 `-b` 之前的部分）。
- 前缀等于本次 `GIT_COMMIT` 前 12 位，**并且** `127.0.0.1:$API_PORT/readyz` 返回 200、`web` 容器在运行 → 跳过 Deploy 和 Smoke，日志打印 `already deployed <tag>`，构建保持 SUCCESS（典型场景：webhook 和轮询对同一提交各触发一次）。
- 其他情况（没有 `.last_tag`、提交不同、不健康、SSH 失败）照常部署。
- 要强制重新部署同一提交：勾选参数 `FORCE_DEPLOY=true`。Init 里用 `params.FORCE_DEPLOY ?: false` 兜底（分支第一次构建参数未注册时不会误当成 true），再写成 `env.FORCE_DEPLOY`，`Dedup check` 的 `when` 只看 env。
- 注意：GHCR 推送在去重之前，仍会推一个本次构建号的 tag（不会被部署）。

## 规则
- **GHCR Token 会过期**：`ghcr-token` 过期后，main 的每次构建都会在 Push GHCR 阶段失败（不会部署）。在 GitHub 重新生成只带 `write:packages` 的 PAT，更新 Jenkins 凭据 `ghcr-token`，再重跑。建议在日历里记下过期日期。
- **已部署的迁移绝不修改**：`migrations/versions/` 里已经在任何环境跑过的文件只读，要改表结构就新增一个迁移（先扩后缩），否则各环境的 `alembic_version` 和真实表结构会不一致，回滚也会失效。
