# agent-memory

多租户 Agent 记忆服务（Python / FastAPI / Postgres + pgvector）。给 tiancode 提供可插拔的远程记忆后端。

## v1 范围
- 长期事实与偏好 + 会话摘要
- 接口：`write / recall / summarize / forget`（`/v1` 前缀，租户从 JWT 解析，写入带幂等键）
- 事实变更：`valid_from / valid_to` 失效旧值，不物理删除；`forget` 才真删
- 召回：租户硬过滤 → 向量 + BM25（RRF 融合）→ token 预算裁剪
- 评测门禁：记住 / 记错 / 失效后不召回旧值 / 不串租户

## 不在 v1
情景记忆、图谱、后台整合；任务状态与 checkpoint 属于 tiancode Runtime。

## 设计参考
借鉴 Mem0 的抽取+多信号检索、Graphiti 的时序失效模型，见 `docs/architecture.md`。

## 数据安全
代码在 GitHub；数据库每日 `pg_dump` 异地备份（见 `deploy/`），机器丢失可重建。
