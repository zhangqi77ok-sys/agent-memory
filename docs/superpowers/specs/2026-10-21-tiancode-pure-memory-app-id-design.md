# 天code 陪伴记忆：纯记忆服务 + 去租户（app_id）设计

**日期：** 2026-10-21（Asia/Shanghai）  
**仓库：** `zhangqi77ok-sys/agent-memory`  
**状态：** 设计已口头批准，待实现计划  

## 1. 背景与目标

当前实现里隔离维度以 `user_id` 为主，同时保留 `tenant_id`（默认 `tiancode`），鉴权走 JWT（`sub` + claim `app`）。产品定位是 **天code 陪伴类** 的记忆后端：不需要多租户产品语义，也不应做成带登录体系的「账号服务」。

**目标：**

1. 记忆服务是 **纯记忆能力**（存 / 召回 / 纠正 / 删除 / 清空 / 导出 / consent / 抽取任务），**不做 JWT、不做登录 / 刷新 / 登出**。
2. 身份由 **业务侧传入原数据**（至少 `user_id`）；服务用配置里的固定 `app_id`（默认 `tiancode`）补齐应用维度。
3. 库表与代码中的 `tenant_id` **改名为 `app_id`**，对外文案一律称「应用」，禁止再出现「租户」。
4. 练习机已有数据采用 **两 PR 双写过渡**，不改已部署的旧 migration 文件。

**非目标：**

- 多应用切换 UI、租户管理后台。
- 在记忆服务内实现用户注册 / SSO / JWT 签发。
- 蓝绿部署（仍由 Docker运维主导，本设计不覆盖）。

## 2. 已定决策摘要

| 项 | 选择 |
| --- | --- |
| 隔离 | `user_id` + 固定 `app_id` |
| 身份 | 业务传 `user_id` 等原数据；服务不验用户 JWT |
| `app_id` 来源 | 服务配置固定（默认 `tiancode`），不由调用方切换多应用 |
| 对外命名 | 列 / 字段 `app_id`，文案「应用」 |
| 数据迁移 | 双写：PR1 加列回填双写，PR2 删 `tenant_id` |
| 方案形态 | 两 PR 渐进（Approach A） |

## 3. 架构

```
天code 业务服务                    agent-memory（纯记忆）
─────────────────                 ─────────────────────
已验完自己的用户会话        →      API：user_id（必填）
调用记忆 HTTP API                 配置：APP_ID=tiancode
                                  维度：app_id + user_id
                                  能力：fact/pref/job/recall/…
```

- 记忆服务信任调用方提供的 `user_id`（练习机可无共享密钥；生产前可选 `X-Service-Key` / mTLS，只挡外网误调，不替代业务传身份）。
- FORCE RLS（若已按 `user_id`）保持；`app_id` 仅作行内维度，不另开「租户级」策略。

## 4. 数据模型与迁移

### 4.1 涉及对象

凡带 `tenant_id` 的表与约束，包括但不限于：

- 表：`memory_fact`、`memory_preference`、`memory_job`、`session_message`、`audit_log`、`user_setting`
- 索引 / 约束：如 `ix_fact_user_subject`、`ix_fact_active`、`uq_job_idem`、`uq_user_setting`

### 4.2 PR1（双写）

1. 新 Alembic revision（**不修改**已部署的 `0001` / `0003` 等旧文件）。
2. 各表增加 `app_id`（先可空）→ `UPDATE … SET app_id = tenant_id` → `NOT NULL`，默认 `'tiancode'`。
3. 按 `app_id` 建立并行索引 / 唯一约束；旧 `tenant_*` 暂留。
4. ORM / 仓储：写入时 `app_id` 与 `tenant_id` 同值双写；读取与过滤以 `app_id` 为准（回填完成后不再依赖 `tenant_id` 读路径）。
5. 代码符号：`UserContext(app_id, user_id)`；删除 JWT 解析路径。

### 4.3 PR2（收尾）

1. 确认无代码路径读写 `tenant_id`。
2. 删除 `tenant_id` 列及旧索引 / 约束，只保留 `app_id`。
3. 文档与错误文案清扫「租户」残留。

## 5. API 与调用约定

- 保留现有记忆能力路由语义（consent、写入会话触发抽取、recall、list、correct、delete、clear、export、settings / jobs）。
- **删除** 登录、刷新、登出及一切 JWT 依赖。
- 每个需隔离的请求由业务在 **JSON body（或与现有 schema 一致的字段）提供 `user_id`**；缺省 → `422`。
- 服务端写入时补上配置 `APP_ID`（默认 `tiancode`）。
- 响应与错误中不出现 `tenant`；必要时只说「应用」。
- 演示 UI（`/memory/ui/`）：用输入框填写 `user_id` 调用同一 API，**不提供登录页**。

## 6. 读写与抽取数据流

1. **写入：** 业务带 `user_id` + 会话消息 → `MemoryJob(app_id, user_id)` → worker 脱敏 → LLM 产出 ADD / UPDATE / NOOP → 落 fact / preference（含 `valid_to`）。双写阶段两列同值。
2. **召回：** 过滤 `app_id` + `user_id` 现行事实；关键词 / bigram（向量可空）；不跨用户。
3. **纠正 / 删除 / 清空 / 导出：** 同一维度；审计日志记 `app_id` + `user_id`。
4. **Consent / UserSetting：** 唯一键 `(app_id, user_id)`；consent 版本逻辑不变。
5. **失败：** 缺 `user_id` → 422；幂等冲突保持既有行为；LLM / worker 失败 → job 失败态，不脏写未校验事实；**不引入** token 过期类错误码。

## 7. 安全

| 环境 | 要求 |
| --- | --- |
| 练习机 | 可无服务密钥；依赖内网 / 网关暴露面控制 |
| 生产前 | 可选 `X-Service-Key`（或 mTLS）；密钥仅放 Jenkins Credentials / 环境变量，**禁止**进公开仓库 |
| 责任边界 | 记忆服务不防「伪造 user_id」；由业务在调用前保证身份正确 |

## 8. 测试与发布门禁

- 单测 / API 测：改为传 `user_id`，去掉 JWT fixture；覆盖回填后读写、consent 唯一键、job 幂等维度。
- PR1 / PR2 各自：pytest 绿 → 开 PR → New Bot 审 → 作者合并 → Jenkins 部署 → 网关 `/readyz` 200 + 冒烟通过 → 向 maxzq 汇报构建号。
- UI：演示填 `user_id`；不测登录流。
- 文档：更新 `docs/architecture.md`、`docs/api.md`；`docs/lessons/` 增一小节说明「应用 vs 原租户」「为何无 JWT」。

## 9. 文档与对外用语

- 代码、OpenAPI、日志、教学文：`app_id` / 「应用」。
- 禁止：`tenant`、`tenant_id`、「租户」（PR2 完成后仓库内检索应为空，除本设计归档与 migration 历史文件名中的既成事实外；历史 migration 内容不改写）。

## 10. 实现顺序（本设计之后）

1. 本 spec 经用户审阅通过。
2. `writing-plans`：拆 PR1 / PR2 实现计划。
3. 按计划开分支实现；不在未批准计划前改业务代码。

## 11. 成功标准

- 记忆服务无 JWT / 登录相关代码路径与接口。
- 业务仅凭 `user_id`（+ 服务固定 `app_id`）即可完成存取改删与抽取。
- PR1 部署后旧数据可读；PR2 后库中无 `tenant_id` 列。
- 公开文档与 API 文案符合「应用」用语；练习机 `/readyz` 与冒烟通过。
