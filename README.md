# agent-memory

纯记忆服务（Python / FastAPI / Postgres + pgvector）。给 tiancode 提供可插拔的远程记忆后端。应用由配置 `APP_ID` 固定（默认 `tiancode`）。除事实与偏好外，还把任务过程提炼成可复用的经验，经 MCP 门面供多个 AI 编程工具共享。

## 与主流方案的区别
一句话：**主流记忆系统让 agent 记住你是谁；这个系统让 agent 记住活是怎么干的——并且能证明下一次干得更省。**

- Mem0 一类从对话抽取事实与偏好，做的是个性化；Zep / Graphiti 是时序知识图谱，强在实体关系与变更追踪；Letta 是自带记忆的 agent 框架（用它的记忆要先用它的运行时）；Claude-mem 一类单工具插件的记忆只属于那一个工具。
- 本系统的原料是**任务轨迹**：目标、步骤、出错点、重试与成本整份入库，提炼成教训 / 策略 / 避坑 / 捷径四类经验，每条带适用情境；新任务开工前以**情境简报**递回。失败的任务同样是原料——栽过的跟头会被结构化地存下来，下次主动递到手边。
- **效果可测，而且是门禁**：50 例经验评测是 CI 门禁的一部分；A/B 演示中带简报完成同类任务 token −68%；召回的余弦门槛 0.66 由真实向量实测校准（命中集 0.670–0.785、未命中集 0.488–0.656），每个阈值都有出处，不是拍脑袋填的。
- **经验属于人，不属于工具**：MCP 门面是第一公民，Codex / Grok / Gemini 等客户端共用一份经验库；轨迹入库前先脱敏（密钥、口令不进库），删除权与数据导出覆盖事实 / 偏好 / 经验 / 轨迹四类，全链路审计，自托管、数据不出自己的库。
- **记忆成网，不成堆**（第十五轮）：信息是点，智慧是线与网。事实与经验落库即织网——互相佐证、细化、泛化、相关、矛盾，都连成带类型与出身（规则推出 / 语义判定）的边；简报沿边扩散激活，捞到一条能带出字面不像但强相关的另一条，互相矛盾的经验在同一份简报里会被明确标出，不再并排沉默；巡检会把跨项目反复出现的同构经验抽象成通用原则。
- **信任有账，遗忘有真**（第十六轮）：简报递出只是「曝光」，不再给经验续命——曝光与应用分开记账，后续任务的成败自动结回被递出的经验（半权计入置信度）；提炼出的每条经验必须附轨迹原文引文，核对不上的标记未验证并降权，不让编造的教训与真经验平权；被判定为矛盾的两条经验，证据悬殊时自动裁决关掉负方；闲置半年且从未被用过的经验关行退出召回——会真遗忘的记忆，才值得信任。
- **像人一样记将来、辨轻重**（第十七轮）：前瞻记忆可以武装「当 X 发生时提醒我做 Y」，新任务开简报时自动撞触发；任务提炼按显著性排队——翻车和烧钱的经历先复盘，不再按提交顺序慢慢磨；事实与偏好有了佐证置信度，被独立来源反复证实的记得更牢、排得更前；老轨迹提炼完只留骨架，细节让位给经验。前台面板与星图每轮同步更新这些新能力，另有管理端（密钥开启）可只读总览所有人的记忆。
- 边界（主动说清）：不做世界实体知识图谱，实体关系推理请选 Zep（本系统织的是记忆与记忆之间的联想网，不是世界知识）；不是 agent 框架，不替你跑 agent；定位个人与小团队，没有托管云与 SDK 生态。

## v1 范围
- 长期事实与偏好；任务轨迹提炼的经验（教训/策略/避坑/捷径）与情境简报
- 接口：`write / recall / forget` + 经验线 `episodes / brief / insights`（`/v1` 前缀，业务传 `user_id`，应用由 `APP_ID` 固定，写入带幂等键）。会话摘要表为预留结构，暂无生成路径，不算现行能力
- 事实变更：`valid_from / valid_to` 失效旧值，不物理删除；`forget` 才真删
- 召回：应用 + 用户硬过滤 → 向量 + BM25（RRF 融合）→ token 预算裁剪
- 评测门禁：记住 / 记错 / 失效后不召回旧值 / 不串用户

## 信任边界与安全
- 服务仅供内网及业务服务器调用，不对公网开放。
- 业务接口调用必须在请求头中携带服务密钥 `X-Service-Key`（取自环境变量 `SERVICE_KEYS`，支持多值轮换），缺失或不匹配返回 `401`；健康检查 `/healthz`、`/readyz` 不需要密钥。
- 本服务不做用户登录认证，`user_id` 由业务调用方在上游完成身份校验与鉴权后传入；`APP_ID` 由服务配置固定；数据隔离由 `user_id + app_id`（Postgres RLS）保证。

## 不在 v1
图谱、后台整合；任务状态与 checkpoint 属于 tiancode Runtime。

## 文档
| 文档 | 给谁看 | 内容 |
|---|---|---|
| [architecture.md](docs/architecture.md) | 所有人 | 唯一的架构出处 |
| [api.md](docs/api.md) | 调用方 | 接口、示例、错误码、幂等规则 |
| [tiancode-integration.md](docs/tiancode-integration.md) | tiancode 研发 | 接入指南、完整交互示例、异常处理 |
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
scripts/test-pg.sh              # 真 Postgres(+pgvector) 全套：需本机 docker，与 CI 的 Test on Postgres 阶段同口径
uvicorn app.main:app --reload   # 默认 SQLite
```

### 一键版（lite，个人自用）
```bash
python -m app.lite              # 单进程全套：SQLite + 自动迁移 + 自动生成服务密钥
```
数据在 `~/.agent-memory/`（`AGENT_MEMORY_HOME` 可改），服务密钥存在该目录的
`service_key.txt`，MCP 与脚本用它连接。配上 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL`
（任意 OpenAI 兼容端点）后自动抽取与提炼开启；配 `GEMINI_API_KEY` 后向量召回开启。
都不配也能用：关键词简报、Markdown 同步、Skills 导出照常。多用户/高并发请用下面的部署形态。

### 经验走出记忆库（Skills 与 Markdown）
```bash
SERVICE_KEY=<密钥> python3 scripts/export_skills.py --user <你> --out <仓库目录>   # 高置信经验 → SKILL.md/AGENTS.md，编程工具直接加载
SERVICE_KEY=<密钥> python3 scripts/sync_markdown.py --pull --user <你> --dir <目录>  # 记忆 → 人可读 .md（带 id，可进 git）
SERVICE_KEY=<密钥> python3 scripts/sync_markdown.py --push --user <你> --dir <目录>  # 编辑器里改过的 .md 导回（删除只报告不执行）
SERVICE_KEY=<密钥> python3 scripts/import_episodes.py --user <你> --file history.jsonl  # 冷启动：历史任务记录批量导入成轨迹
```

## 部署
`deploy/docker-compose.yml`（api + worker + pgvector），`Jenkinsfile`（gitleaks → lint/test → 真 Postgres 测试 → 构建镜像 → 推 GHCR → SSH 部署到部署机 → /readyz 冒烟，失败自动回滚上一个成功 tag），`deploy/backup.sh` / `deploy/restore.sh` / `deploy/ensure-roles.sh`。
详见 `docs/deploy.md`、`docs/runbook.md`、`docs/adr/0002-jenkins-to-deploy-host.md`。

**部署状态：已验证（2026-10-20，Jenkins 构建 `agent-memory` #2 全绿、#3 冒烟故意失败→自动回滚、备份恢复演练通过；main 持续部署由多分支任务 `agent-memory-mb` 负责）**

## 当前状态（骨架）
- 已实现：身份（业务传 `user_id`，演示页输入框或 `?user_id=`；应用来自 `APP_ID`）、写入入队（幂等）、召回（预过滤有效事实，关键词打分占位）、遗忘（删所有版本 + forgotten_id）、Worker 领取/死信、**抽取管线**（脱敏 → LLM → 代码判定 ADD/UPDATE/NOOP → `valid_to` 失效旧值）、v1 迁移（Postgres 下含 pgvector/HNSW/GIN/RLS）。
- 已实现（面板）：`/v1/me/*` + `web/`「我的记忆」前端（同意、列表、纠正、删除、清空、导出、总开关、任务状态）。
- 已实现（跨工具共享，第一轮）：MCP 门面 `app/mcp_server.py`（memory_write / search / list / forget），与 REST 共用同一条代码路径；Codex、Gemini CLI 等 stdio 客户端连同一个库即互通。
- 已实现（语义召回，第一/二轮）：事实 embedding 由 Gemini `gemini-embedding-001` 生成（1536 维）；混合召回 = 0.7 向量 + 0.3 关键词，入选需关键词命中或余弦 ≥ 0.66（实测标定）；主题合并 same_subject 兜底、worker 并发锁、CJK token 估算。
- 已实现（经验记忆，第三轮）：任务过程提交（`POST /v1/episodes`、MCP `memory_episode_submit`）→ Worker 复盘提炼为经验条目（教训/打法/避坑/省钱招，`memory_insight` 表，同走脱敏/注入闸门/decide 裁决/时间失效）→ 开工前情境召回（`POST /v1/brief`、MCP `memory_brief`）。A/B 演示：`scripts/episode_demo.py`。
- 未实现：摘要（summarize）；召回融合为线性加权而非 RRF。
- Worker 生产配置：`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`（见 `deploy/.env.example`）。测试注入假抽取器，不读这些变量。
- 已验证：Postgres 上的迁移与 Compose 启动、Jenkins 实跑、GHCR 推送、自动回滚、备份恢复+遗忘重删。
