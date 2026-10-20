# 架构与选型（v1）

tiancode `MemoryProvider` → HTTP `/v1` + JWT → FastAPI → 写入管线（抽取→去重→冲突判定→脱敏→入库）/ 召回（租户过滤→向量+BM25→RRF→裁剪）→ Postgres + pgvector；审计表记录每次写入与召回。

降级：召回失败 → 本轮不带长期记忆并告警；写入失败 → 进入待补写发件箱（不参与召回），恢复后按幂等键补写，积压告警。

选型：FastAPI；Postgres 16 + pgvector；embedding 走 API，模型名记入表，可换 CPU 小模型后重建索引；v1 不上图数据库。
