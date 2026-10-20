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

## 🚧 待实现（v1 范围内）
| 接口 | 说明 |
|---|---|
| `POST /v1/sessions/{id}/summarize` | 增量滚动摘要 |
| `GET /v1/me/memories` | 查看我的记忆（分页，含来源） |
| `PATCH /v1/me/memories/{id}` | 纠正，生成新版本 |
| `DELETE /v1/me/memories` | 全部清空（异步） |
| `GET /v1/me/memories/export` | 导出 JSON |
| `PUT /v1/me/settings` | `{memory_enabled}` 暂停/开启记忆 |
