# 部署说明（Jenkins → 部署机）

> 公开仓库：这里不写真实 IP、密码、Token。部署机地址只配在 Jenkins 全局环境变量 `DEPLOY_HOST` 里。

## 拓扑
```
浏览器 ──:80──> nginx-gw（宿主机网络）
                 ├─ /           → 原有服务（不动）
                 ├─ /jenkins/   → Jenkins 容器 127.0.0.1:8088
                 ├─ /memory/ui/ → 预留：前端静态站点 127.0.0.1:8091（尚未部署，现在返回 502）
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
| `ENV` / `APP_ENV` | `prod`。非 dev/test 时，`JWT_SECRET` 仍是 `dev-only-` 默认值会拒绝启动 |
| `POSTGRES_PASSWORD` | 表 owner 账号 `memory` 的密码，只用于迁移和备份 |
| `APP_DB_PASSWORD` | api 运行账号 `memsvc_app`（非 owner、受 FORCE RLS 约束） |
| `WORKER_DB_PASSWORD` | worker 账号 `memsvc_worker`（BYPASSRLS、非超级用户） |
| `JWT_SECRET` | 签发/校验用户 JWT 的密钥，`openssl rand -hex 32` |
| `API_BIND` / `API_PORT` | api 映射到宿主的地址和端口，默认 `127.0.0.1:8090`，不对公网直接开放 |
| `ROOT_PATH` | 反向代理前缀 `/memory`，传给 `uvicorn --root-path`，Swagger 才能加载正确的 `openapi.json` |
| `DOCS_ENABLED` | Swagger UI 开关。默认 prod 关闭；演示部署设 `true` 打开 `/memory/docs`。正式上线前改回 `false` |

改 `.env`：同时改部署机文件和 Jenkins 凭据 `deploy-env`（下次部署会用凭据覆盖部署机文件）。**注意**：数据库三个密码只在第一次初始化数据卷时生效，之后改要在库里 `ALTER ROLE ... PASSWORD`。

## 回滚
- 自动：Smoke 失败 → 读 `.last_tag`（上一次冒烟成功的镜像）→ `compose up -d` → 再确认 `/readyz` 200。没有 `.last_tag` 时报错，不瞎回滚。
- 手动：
  ```bash
  cd /opt/agent-memory && cat .last_tag            # 例：ghcr.io/zhangqi77ok-sys/agent-memory:<tag>
  IMAGE=ghcr.io/zhangqi77ok-sys/agent-memory TAG=<要回到的tag> docker compose -p agent-memory up -d
  curl -s 127.0.0.1:8090/readyz
  ```
- 迁移只做「先扩后缩」（只加列/表），所以回滚镜像不需要回滚数据库。

## 前端预留（还没做）
- 访问路径：`/memory/ui/`，nginx 已预留 `location /memory/ui/ → 127.0.0.1:8091/`（必须写在 `/memory/` 之前）。
- 前端：仓库 `web/`（Vue3），Jenkins `Frontend` 阶段跑 `npm ci && npm test && npm run build`；镜像 `ghcr.io/zhangqi77ok-sys/agent-memory-web`，compose 服务名 `web`，默认绑定 `127.0.0.1:8091`。外层 Nginx 把 `/memory/ui/` 转到该容器（由运维配置）。和 api 共用同一次部署的 TAG、同一次回滚。
- 前端调接口用相对路径 `/memory/v1/...`，同源，不需要 CORS。

## 规则
- **GHCR Token 会过期**：`ghcr-token` 过期后，main 的每次构建都会在 Push GHCR 阶段失败（不会部署）。在 GitHub 重新生成只带 `write:packages` 的 PAT，更新 Jenkins 凭据 `ghcr-token`，再重跑。建议在日历里记下过期日期。
- **已部署的迁移绝不修改**：`migrations/versions/` 里已经在任何环境跑过的文件只读，要改表结构就新增一个迁移（先扩后缩），否则各环境的 `alembic_version` 和真实表结构会不一致，回滚也会失效。
