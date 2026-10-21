# 评测（v1 门禁）

目标：证明记忆服务「记得住、记得对、会过期、不串人」。任一失败即阻断合并与发布（Jenkins `Lint & Test` 阶段）。

## 怎么跑
```bash
. .venv/bin/activate
pytest -q                 # 全部
pytest -q -k cross_user   # 只跑串用户
```
默认跑 SQLite（快速反馈）；**门禁以 Jenkins `Test on Postgres` 阶段结果为准**：设置 `TEST_DATABASE_URL` 后在真实 Postgres + pgvector 上跑，迁移走 Alembic，服务用非超级用户 `memsvc_app` 账号，行级权限真实生效；该阶段必须有用例且 0 skip。

## 五类评测
| 类别 | 测什么 | 判定 | 当前用例 |
|---|---|---|---|
| 记住了吗 | 同意后写入，Worker 抽出的事实能在列表、导出和召回里看到 | 列表 `total≥1`，导出含该事实 | ✅ `test_consented_write_is_extracted_into_list_and_export` |
| 记错了吗 | 入库内容来自抽取结果，不改写成别的事实 | 事实内容与候选一致，并带 `source_msg_id` | ✅ 同上（`source_msg_id` / `content`） |
| 过期不召回 | 事实更新后旧值不再出现 | 只返回最新版本 | ✅ `test_invalidated_fact_not_recalled`、`test_coffee_contradiction_invalidates_old_version` |
| 不串用户 | A 召回不到 B；A 删不了 B；数据库层兜底 | 召回结果无 B 内容；删 B 返回 404；漏写条件也查不到 | ✅ `test_no_cross_user_recall`、`test_forget_deletes_all_versions_and_hides_others`、PG：`test_rls_hides_other_users_even_without_where`、`test_rls_blocks_writing_rows_for_other_user`、`test_api_cross_user_recall_and_forget_via_service_account` |
| 不误召回 | 问不相关的内容，召回为空；召回了不该召回的比没召回更危险 | 查「咖啡」不返回「猫」那条；查「咖啡」不返回「咖喱」（中文按相邻两字切）；分数低于临时阈值 0.2 不返回，接入向量 + BM25 后用评测集重新校准 | ✅ `test_irrelevant_facts_not_recalled` |

## 其它保护性用例
- `test_body_cannot_carry_identity`：请求体带 `user_id` → 422
- `test_write_is_idempotent`：同键写两次 → 同一 job
- `test_requires_token`：无 token → 401
- `test_claim_and_dead_letter`、`test_extractor_errors_retry_then_dead`：Worker 失败 3 次进死信，`last_error` 留下原因
- `test_pii_is_redacted_before_extractor_and_before_insert`：手机号/邮箱/身份证/银行卡在调用抽取器和入库前都被换成占位符
- `test_consent_off_at_processing_skips_write`：处理时总开关已关，不调用模型，任务完成且不入库
- `test_reprocess_job_is_idempotent`：同一任务再跑一次不会多出一行
- `test_decide_ignores_model_proposal`：模型建议 ADD/UPDATE/NOOP 不影响代码判定
- PG：`test_skip_locked_two_workers_get_different_jobs`：两个 Worker 并发领到不同任务
- PG：`test_migration_artifacts`、`test_service_account_is_not_superuser`
- `test_refuses_default_jwt_secret_outside_dev`：非开发环境用默认密钥拒绝启动

## 🚧 契约测试
`LocalMemoryProvider`（tiancode 本地）与 `RemoteMemoryProvider`（本服务）跑同一套用例，结果一致才算通过。
