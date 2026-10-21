# agent-memory

纯记忆服务（Python / FastAPI / Postgres + pgvector）。给 tiancode 提供可插拔的远程记忆后端。应用由配置 `APP_ID` 固定（默认 `tiancode`）。

## v1 范围
- 长期事实与偏好 + 会话摘要
- 接口：`write / recall / summarize / forget`（`/v1` 前缀，业务传 `user_id`，应用由 `APP_ID` 固定，写入带幂等键）
- 事实变更：`valid_from / valid_to` 失效旧值，不物理删除；`forget` 才真删
- 召回：应用 + 用户硬过滤 → 向量 + BM25（RRF 融合）→ token 预算裁剪
- 评测门禁：记住 / 记错 / 失效后不召回旧值 / 不串用户

## 不在 v1
情景记忆、图谱、后台整合；任务状态与 checkpoint 属于 tiancode Runtime。

## 文档
| 文档 | 给谁看 | 内容 |
|---|---|---|
| [architecture.md](docs/architecture.md) | 所有人 | 唯一的架构出处 |
| [api.md](docs/api.md) | 调用方 | 接口、示例、错误码、幂等规则 |
| [eval.md](docs/eval.md) | 开发/评审 | 四类评测与门禁 |
| deploy.md / runbook.md | 运维 | 由 Docker运维 补充 |
| [adr/](docs/adr/) | 面试/评审 | 关键取舍 |
| [lessons/me-ui.md](docs/lessons/me-ui.md) | 演示/面试 | 「我的记忆」面板四条合规演示 |
| [lessons/app-id-pure-memory.md](docs/lessons/app-id-pure-memory.md) | 演示/面试 | 为什么不验令牌、应用标识与双写 |
| [lessons/extraction.md](docs/lessons/extraction.md) | 演示/面试 | 抽取、脱敏、咖啡案例的失效 |
| [web/](web/) | 前端 | C 端面板（Vue3 + Vite，路径 `/memory/ui/`） |

## 设计参考
借鉴 Mem0 的抽取+多信号检索、Graphiti 的时序失效模型，见 `docs/architecture.md`。

## 数据安全
代码在 GitHub；数据库每日 `pg_dump` 异地备份（见 `deploy/`），机器丢失可重建。

## 本地运行
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q                       # SQLite 单测
uvicorn app.main:app --reload   # 默认 SQLite
```

## 部署
`deploy/docker-compose.yml`（api + worker + pgvector），`Jenkinsfile`（gitleaks → lint/test → 真 Postgres 测试 → 构建镜像 → 推 GHCR → SSH 部署到部署机 → /readyz 冒烟，失败自动回滚上一个成功 tag），`deploy/backup.sh` / `deploy/restore.sh` / `deploy/ensure-roles.sh`。
详见 `docs/deploy.md`、`docs/runbook.md`、`docs/adr/0002-jenkins-to-deploy-host.md`。

**部署状态：已验证（2026-10-20，Jenkins 构建 `agent-memory` #2 全绿、#3 冒烟故意失败→自动回滚、备份恢复演练通过；main 持续部署由多分支任务 `agent-memory-mb` 负责）**

## 当前状态（骨架）
- 已实现：身份（业务传 `user_id`，演示页输入框或 `?user_id=`；应用来自 `APP_ID`）、写入入队（幂等）、召回（预过滤有效事实，关键词打分占位）、遗忘（删所有版本 + forgotten_id）、Worker 领取/死信、**抽取管线**（脱敏 → LLM → 代码判定 ADD/UPDATE/NOOP → `valid_to` 失效旧值）、v1 迁移（Postgres 下含 pgvector/HNSW/GIN/RLS）。
- 已实现（面板）：`/v1/me/*` + `web/`「我的记忆」前端（同意、列表、纠正、删除、清空、导出、总开关、任务状态）。
- 未实现：向量 + BM25 + RRF（事实的 embedding 仍为空）、摘要。
- Worker 生产配置：`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`（见 `deploy/.env.example`）。测试注入假抽取器，不读这些变量。
- 已验证：Postgres 上的迁移与 Compose 启动、Jenkins 实跑、GHCR 推送、自动回滚、备份恢复+遗忘重删。
