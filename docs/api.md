# API 文档（v1）

- Base URL：`http(s)://<host>:8000`，所有业务接口带 `/v1` 前缀。
- 信任边界与服务密钥：服务仅供内网/业务服务器调用。所有 `/v1/*` 业务接口必须在请求头中携带服务密钥 `X-Service-Key`（取自环境变量 `SERVICE_KEYS`，支持多值轮换），缺失或错误返回 `401`；健康检查 `/healthz`、`/readyz` 不需要密钥。
- 身份：业务调用方在上游完成身份校验与鉴权后传入 `user_id`（有 JSON body 的接口放 body，GET/DELETE 用 `?user_id=`），本服务不做用户登录。缺失或空串返回 `422`。`app_id` 由服务配置 `APP_ID` 固定（默认 `tiancode`），调用方不能切换；body 里出现 `app_id` 等未定义字段一律 `422`。
- 身份绑定（第二十五轮，opt-in）与未绑定断言默认拒绝（修复第 1 轮）：配置 `SERVICE_KEY_USERS`（逗号分隔 `key:user` 对）后，该密钥的请求即以绑定用户为准，查询与 body 里的 `user_id` 断言全部失效；未配置绑定时，默认不再接受远程断言——未绑定密钥带 `user_id` 调任何业务接口一律 `403`（错误码 `unbound_assertion_denied`），出路二选一：用 `SERVICE_KEY_USERS` 绑密钥，或在确认单用户自用的前提下设 `ALLOW_UNBOUND_ASSERTION=1` 显式恢复断言行为。绑定只收窄不放宽——映射里的 key 仍须先过 `SERVICE_KEYS` 校验。面板可用 `GET /v1/me/identity` 查看当前身份与模式（`bound`）。MCP 不受绑定影响（工具传参面，见 architecture.md）。
- 交互式文档：非 prod 环境或配置 `DOCS_ENABLED=true` 时可访问 `/docs`（Swagger UI）或 `/openapi.json`；prod 默认关闭（返回 `404`）。
- C 端面板：`/memory/ui/`（Vue 静态站，只调本文 `/v1/me/*`；仅内网可访问；演示填写 `user_id`，也可用 `?user_id=` 预填）。
- 接入指南与调用代码示例：详见 [tiancode 业务方接入指南](tiancode-integration.md)。
- 状态：✅ 已实现　🚧 已设计未实现

## 错误码
| 状态码 | 含义 |
|---|---|
| 401 | 未授权：缺少或错误的服务密钥请求头 `X-Service-Key` |
| 404 | 资源不存在**或不属于当前用户**（不区分，避免暴露存在性） |
| 409 | 冲突：隐私协议版本升级，需用户重新同意（返回 `code: consent_required`） |
| 422 | 缺少或非法 `user_id` / 参数校验失败 / 请求体含未定义字段（如 `app_id`） |
| 429 | 请求过多：超过 user_id 限流阈值（写/导出更严格），带 Retry-After 响应头 |
| 5xx | 服务异常；调用方按降级策略处理（见 architecture.md 5.3） |

## 健康检查与监控 ✅
- `GET /healthz` → `200 {"status":"ok"}`：进程存活
- `GET /readyz` → `200 {"status":"ready"}`：数据库可连，可接流量
- `GET /metrics` → `200`（Prometheus 文本格式）：请求计数、5xx 数量、`memory_job` 各状态堆积与最旧任务年龄、就绪探针状态

## 写入记忆 ✅　`POST /v1/memories`
- 头：`Idempotency-Key: <调用方生成，建议 session_id + 轮次>`（必填）
- **幂等**：同一用户 + 同一键重复提交返回同一个 `job_id`，`duplicate=true`，不会重复写入。
```json
// 请求
{"user_id": "alice", "session_id": "s-001", "messages": [{"msg_id": "m-1", "role": "user", "content": "我平时用 Python 写后端"}]}
// 202
{"job_id": "9f2c...", "duplicate": false}
```
约束：`messages` 1–50 条，单条 ≤ 8000 字，`role` 只能是 `user`/`assistant`。接口只落原文和任务（原文带 `job_id`）。Worker 异步处理这一次提交的消息：

1. 处理时再查同意和总开关。已关闭则任务 `done`，不调用模型、不写事实。
2. 手机号、邮箱、身份证号、银行卡号先换成占位符，再请求 LLM。
3. 模型返回的候选会再脱敏一次。ADD / UPDATE / NOOP 由代码决定：同一个 `subject`（偏好则是 `key`）没有旧值就新增，值相同就跳过，值矛盾则旧行 `valid_to=now()` 并插入 `version+1`。
4. 列表和召回只返回 `valid_to IS NULL`。导出仍包含历史版本（旧值带 `valid_to`）。
5. 连不上模型会重试，最多 3 次后 `status=dead`，`last_error` 留下原因。模型输出不是合法 JSON 时丢弃该批、任务完成、不入库。

同一条任务重复处理不会插入第二份相同事实。抽取用的环境变量见 `docs/deploy.md`。

## 召回 ✅　`POST /v1/memories/recall`
- 只返回当前用户、当前有效（`valid_to IS NULL`）的事实与偏好。**幂等**（只读，但会写一条审计）。
```json
// 请求
{"user_id": "alice", "query": "我用什么语言", "k": 8, "token_budget": 1500}
// 200
{"preferences": {"language": "zh"},
 "facts": [{"fact_id": "a1b2...", "subject": "lang", "content": "用 Python 写后端", "score": 0.42, "reason": "keyword"}]}
```
`k` 1–50，`token_budget` 100–8000。当前打分为关键词占位，🚧 向量 + BM25 + RRF。

## 遗忘一条 ✅　`DELETE /v1/me/memories/{fact_id}?user_id=`
- `204`：若是事实，删除**同一 subject 的所有历史版本**（含向量）；若是偏好，删除同一 key 的所有版本。id 记入 `forgotten_id`；内容规范化哈希另记入 `forgotten_content`（修复第 2 轮）：此后同一内容以任何新 id 重发、重提炼、经 Markdown 导回或纠正/PATCH 改回时一律禁写（写路径返回 `tombstoned`，纠正/PATCH 返回 409 `{"code": "tombstoned"}`，Markdown 导回计入 `tombstoned`）；创建于删除之前的在途任务整单丢弃。注意：此后想一字不差重新记住同一内容会被挡住，须改写措辞（显式重新确认语义后续轮次再定）。
- `404`：不存在或不属于当前用户。
- **幂等**：重复删除返回 `404`，无副作用。
- 🚧 级联：重生成受影响的会话摘要、删除来源原文。

## 授权同意与总开关 ✅　`GET/PUT /v1/me/settings`
- **默认关闭**：用户没同意之前，`POST /v1/memories` 返回 `202 {"job_id": null, "skipped": true}`，**什么都不存**（连原始消息都不存）；召回返回空。调用方（tiancode）照常继续，不用特殊处理。
- 读取：`GET /v1/me/settings?user_id=alice`。
- 开启：`PUT {"user_id": "alice", "memory_enabled": true, "consent_version": "2026-10-v1"}`。版本必须等于 `GET` 返回的 `current_consent_version`，否则 `409 consent_required`，前端重新展示授权页。隐私说明改版时升版本号，老用户需要重新同意。
- 关闭：`PUT {"user_id": "alice", "memory_enabled": false}`，立即生效：不写入、不召回，**已有数据保留**（要删请用清空）。再次开启同一版本无需重新同意。

## 查看我的记忆 ✅　`GET /v1/me/memories?user_id=&kind=fact|preference&offset=0&limit=50`
- v1 先全量查出再在内存里分页（单用户条数有上限，见架构文档）；数据量上来后改成 SQL 分页。
- 只列当前有效版本，按时间倒序；每条带 `kind`、`subject`、`version`、`valid_from`、`source_session_id`（来源会话）。

## 查看我的经验 ✅　`GET /v1/me/insights?user_id=&offset=0&limit=50`
- 第二十五轮改为分页（与 `/v1/me/memories` 同口径，默认 50、上限 200），返回 `{"items","total","offset","limit"}`；此前无界列表，面板只能静默截断在前 100 条。含已被禁用的（本人要能看见才能取消禁用）；每条带 `source_episode_id`（来源轨迹，见下）。
- 溯源：`GET /v1/me/episodes/{episode_id}?user_id=` 返回轨迹详情（目标、步骤、结果、提炼状态、显著性）；跨用户与不存在一律 `404`。事实行只有 `source_session_id`（来源会话），没有轨迹可链，面板只作文本展示。

## 纠正一条 ✅　`PATCH /v1/me/memories/{id}`　`{"user_id": "alice", "content": "..."}`
- 事实或偏好都可以。旧版本 `valid_to=now` 失效，新建 `version+1`；召回只会看到新版本。
- `404`：不存在、已经失效、或不属于当前用户。

## 全部清空 ✅　`DELETE /v1/me/memories?user_id=`
- `204`：删除我的事实（含历史版本和向量）、偏好、摘要、原始消息、写入任务；id 记入 `forgotten_id`，备份恢复后据此重删。设置（是否同意）保留。v1 同步执行。
- 前端必须二次确认。

## 导出 ✅　`GET /v1/me/memories/export?user_id=`
- 返回 JSON：设置、全部事实（含历史版本）、偏好；不含向量。写一条审计。

## 写入任务状态 ✅　`GET /v1/me/jobs?user_id=&limit=20`
- 最近的写入任务：`pending / running / done / dead`、重试次数、最后错误。

## 提交任务过程 ✅　`POST /v1/episodes`
- 任务结束后提交全过程，供复盘提炼经验：`{"user_id","title","outcome":"success|failure|partial","steps":[{"action","observation?","error?"}],"cost":{...}}`。返回 202 + `episode_id`；未同意/已暂停返回 `skipped=true` 且不落数据。Worker 随后提炼为经验条目（`memory_insight`），坏输出整批丢弃、瞬时故障重试 3 次。

## 任务简报（情境召回）✅　`POST /v1/brief`
- 新任务开始前按情境捞经验：`{"user_id","task","k"=5}` → `{"insights":[{"insight_id","kind","subject","content","applies_when","score","reason"}]}`。kind 为 lesson/strategy/pitfall/cost_saver；打分与事实混合召回同构（0.7 向量 + 0.3 关键词，余弦门槛 0.66），无向量时退化关键词。MCP 侧对应 `memory_episode_submit` / `memory_brief`。

## 🚧 待实现（v1 范围内）
| 接口 | 说明 |
|---|---|
| `POST /v1/sessions/{id}/summarize` | 增量滚动摘要 |

## 迁移规矩
- 已经部署过的迁移文件**不准再改**，任何调整都新开一个迁移。（#5 收窄 0001 的建表范围是唯一一次例外：已有库不会重跑 0001，影响限于新建库。）

## 已知边界
- 行级权限防的是「代码漏写用户条件」。`app.user_id` 由服务按请求里的 `user_id` 设置；谁是当前用户由业务调用方保证。Worker 用独立 `BYPASSRLS` 账号，应用账号无法绕过。
- `deploy/initdb` 只在数据库卷为空时执行；已有数据库需要手动建 `memsvc_app` / `memsvc_worker`（见 runbook）。
