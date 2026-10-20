# 评测（v1 门禁）

目标：证明记忆服务「记得住、记得对、会过期、不串人」。任一失败即阻断合并与发布（Jenkins `Lint & Test` 阶段）。

## 怎么跑
```bash
. .venv/bin/activate
pytest -q                 # 全部
pytest -q -k cross_user   # 只跑串用户
```
默认跑 SQLite（快速反馈）；**门禁以 Jenkins `Test on Postgres` 阶段结果为准**：设置 `TEST_DATABASE_URL` 后在真实 Postgres + pgvector 上跑，迁移走 Alembic，服务用非超级用户 `memsvc_app` 账号，行级权限真实生效；该阶段必须有用例且 0 skip。

## 四类评测
| 类别 | 测什么 | 判定 | 当前用例 |
|---|---|---|---|
| 记住了吗 | 写入的事实之后能被召回 | 期望事实出现在 top-k | 🚧 需抽取管线，待补 |
| 记错了吗 | 召回内容与原话一致，不编造 | 召回内容可追溯到 `source_msg_id` | 🚧 待补 |
| 过期不召回 | 事实更新后旧值不再出现 | 只返回最新版本 | ✅ `test_invalidated_fact_not_recalled` |
| 不串用户 | A 召回不到 B；A 删不了 B；数据库层兜底 | 召回结果无 B 内容；删 B 返回 404；漏写条件也查不到 | ✅ `test_no_cross_user_recall`、`test_forget_deletes_all_versions_and_hides_others`、PG：`test_rls_hides_other_users_even_without_where`、`test_rls_blocks_writing_rows_for_other_user`、`test_api_cross_user_recall_and_forget_via_service_account` |

## 其它保护性用例
- `test_body_cannot_carry_identity`：请求体带 `user_id` → 422
- `test_write_is_idempotent`：同键写两次 → 同一 job
- `test_requires_token`：无 token → 401
- `test_claim_and_dead_letter`：Worker 失败 3 次进死信
- PG：`test_skip_locked_two_workers_get_different_jobs`：两个 Worker 并发领到不同任务
- PG：`test_migration_artifacts`、`test_service_account_is_not_superuser`
- `test_refuses_default_jwt_secret_outside_dev`：非开发环境用默认密钥拒绝启动

## 🚧 契约测试
`LocalMemoryProvider`（tiancode 本地）与 `RemoteMemoryProvider`（本服务）跑同一套用例，结果一致才算通过。
