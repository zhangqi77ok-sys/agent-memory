# API 文档（v1）

- Base URL：`http(s)://<host>:8000`，所有业务接口带 `/v1` 前缀。
- 认证：`Authorization: Bearer <JWT>`。`sub` = 用户 ID，`app` = 接入应用（默认 `tiancode`）。**身份只来自 JWT**，请求体里出现 `user_id`/`tenant_id` 等未定义字段一律 `422`。
- 交互式文档：服务启动后访问 `/docs`（Swagger UI）或 `/openapi.json`。
- 状态：✅ 已实现　🚧 已设计未实现

## 错误码
| 状态码 | 含义 |
|---|---|
| 401 | 缺少或无效 token |
| 404 | 资源不存在**或不属于当前用户**（不区分，避免暴露存在性） |
| 422 | 参数校验失败 / 请求体含未定义字段 |
| 429 | 🚧 超过用户限流或每日配额 |
| 5xx | 服务异常；调用方按降级策略处理（见 architecture.md 5.3） |

## 健康检查 ✅
- `GET /healthz` → `200 {"status":"ok"}`：进程存活
- `GET /readyz` → `200 {"status":"ready"}`：数据库可连，可接流量

## 写入记忆 ✅　`POST /v1/memories`
- 头：`Idempotency-Key: <调用方生成，建议 session_id + 轮次>`（必填）
- **幂等**：同一用户 + 同一键重复提交返回同一个 `job_id`，`duplicate=true`，不会重复写入。
```json
// 请求
{"session_id": "s-001", "messages": [{"msg_id": "m-1", "role": "user", "content": "我平时用 Python 写后端"}]}
// 202
{"job_id": "9f2c...", "duplicate": false}
```
约束：`messages` 1–50 条，单条 ≤ 8000 字，`role` 只能是 `user`/`assistant`。接口只落原文和任务，抽取由 Worker 异步完成。

## 召回 ✅　`POST /v1/memories/recall`
- 只返回当前用户、当前有效（`valid_to IS NULL`）的事实与偏好。**幂等**（只读，但会写一条审计）。
```json
// 请求
{"query": "我用什么语言", "k": 8, "token_budget": 1500}
// 200
{"preferences": {"language": "zh"},
 "facts": [{"fact_id": "a1b2...", "subject": "lang", "content": "用 Python 写后端", "score": 0.42, "reason": "keyword"}]}
```
`k` 1–50，`token_budget` 100–8000。当前打分为关键词占位，🚧 向量 + BM25 + RRF。

## 遗忘一条 ✅　`DELETE /v1/me/memories/{fact_id}`
- `204`：删除该事实**同一 subject 的所有历史版本**（含向量），id 记入 `forgotten_id`。
- `404`：不存在或不属于当前用户。
- **幂等**：重复删除返回 `404`，无副作用。
- 🚧 级联：重生成受影响的会话摘要、删除来源原文。

## 授权同意与总开关 ✅　`GET/PUT /v1/me/settings`
- **默认关闭**：用户没同意之前，`POST /v1/memories` 返回 `202 {"job_id": null, "skipped": true}`，**什么都不存**（连原始消息都不存）；召回返回空。调用方（tiancode）照常继续，不用特殊处理。
- 开启：`PUT {"memory_enabled": true, "consent_version": "2026-10-v1"}`。版本必须等于 `GET` 返回的 `current_consent_version`，否则 `409 consent_required`，前端重新展示授权页。隐私说明改版时升版本号，老用户需要重新同意。
- 关闭：`PUT {"memory_enabled": false}`，立即生效：不写入、不召回，**已有数据保留**（要删请用清空）。再次开启同一版本无需重新同意。

## 查看我的记忆 ✅　`GET /v1/me/memories?kind=fact|preference&offset=0&limit=50`
- v1 先全量查出再在内存里分页（单用户条数有上限，见架构文档）；数据量上来后改成 SQL 分页。
- 只列当前有效版本，按时间倒序；每条带 `kind`、`subject`、`version`、`valid_from`、`source_session_id`（来源会话）。

## 纠正一条 ✅　`PATCH /v1/me/memories/{id}`　`{"content": "..."}`
- 事实或偏好都可以。旧版本 `valid_to=now` 失效，新建 `version+1`；召回只会看到新版本。
- `404`：不存在、已经失效、或不属于当前用户。

## 全部清空 ✅　`DELETE /v1/me/memories`
- `204`：删除我的事实（含历史版本和向量）、偏好、摘要、原始消息、写入任务；id 记入 `forgotten_id`，备份恢复后据此重删。设置（是否同意）保留。v1 同步执行。
- 前端必须二次确认。

## 导出 ✅　`GET /v1/me/memories/export`
- 返回 JSON：设置、全部事实（含历史版本）、偏好；不含向量。写一条审计。

## 写入任务状态 ✅　`GET /v1/me/jobs?limit=20`
- 最近的写入任务：`pending / running / done / dead`、重试次数、最后错误。

## 🚧 待实现（v1 范围内）
| 接口 | 说明 |
|---|---|
| `POST /v1/sessions/{id}/summarize` | 增量滚动摘要 |

## 迁移规矩
- 已经部署过的迁移文件**不准再改**，任何调整都新开一个迁移。（#5 收窄 0001 的建表范围是唯一一次例外：已有库不会重跑 0001，影响限于新建库。）

## 已知边界
- 行级权限防的是「代码漏写用户条件」。`app.user_id` 由服务按 JWT 的 `sub` 设置，应用账号本身仍能改这个变量，所以**谁是当前用户由 JWT 校验 + 服务代码保证**，不是由数据库保证。Worker 用独立 `BYPASSRLS` 账号，应用账号无法绕过。
- `deploy/initdb` 只在数据库卷为空时执行；已有数据库需要手动建 `memsvc_app` / `memsvc_worker`（见 runbook）。
