# ADR-0001 自研记忆服务，而非直接接入 Mem0 / Zep / Letta / LangMem

状态：已采纳（2026-10-20）

## 背景
需要：按租户硬隔离、全链路审计回放、与 Jenkins/Compose 部署、面试可讲清原理、无 GPU。

## 选项
- Mem0（Apache-2.0）：抽取+多信号检索成熟；开源 v3 去掉图记忆，租户隔离与审计需自行包装。
- Zep / Graphiti：时序失效模型好；Zep 商用托管、社区版已停维护；Graphiti 需额外运维图数据库。
- Letta：完整 Agent Runtime，会与 tiancode 自己的 Runtime 重叠。
- LangMem（MIT）：绑定 LangGraph。

## 决定
自研薄服务（FastAPI + Postgres/pgvector），借鉴 Mem0 的写入抽取/多信号召回、Graphiti 的 `valid_from/valid_to` 失效模型。

## 后果
+ 隔离、审计、评测完全可控，单库运维。
− 抽取质量需自建评测集持续调；图关系推理留到 v2。
