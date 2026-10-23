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
| 不误召回 | 问不相关的内容，召回为空；召回了不该召回的比没召回更危险 | 查「咖啡」不返回「猫」那条；查「咖啡」不返回「咖喱」（中文按相邻两字切）；分数低于校准阈值 0.10 不返回；由 50 条中文陪伴场景评测集校准验证 | ✅ `test_irrelevant_facts_not_recalled`、`test_companion_recall_quality_benchmark` |

## 中文陪伴场景基准评测（50 条基准集，F1 门禁）
评测用例位于 `tests/test_eval_benchmark.py`，专门覆盖中文陪伴场景真实分布：
1. **用户偏好（Preference）**：饮食、作息、昵称、运动休闲习惯。
2. **事实更新（Fact Update）**：工作变动、宠物更新、居住地址变更，确保新版本覆盖旧版本且旧版本不被召回。
3. **否定与禁忌（Negation）**：过敏源、忌口食材、社交与剧透禁忌。
4. **多轮深层事实（Multi-turn）**：家人纪念日、母校、长远梦想等深层陪伴细节。
5. **过期失效（Expiration）**：短期状态与预约（`valid_to` 不为空），确保已过期事实 0 泄露。

**CI 质量门禁指标**：
- 召回覆盖率（Coverage / Recall） >= 80%（当前实测 100.00%）
- 检索准确率（Precision） >= 80%（当前实测 100.00%）
- 过期数据泄露率 = 0%（当前实测 0%）
任何低于门槛的变动将在 CI 阶段直接阻断构建。

## 经验记忆评测（50 例，E 门禁，第四轮补建）

评测用例位于 `tests/test_eval_experience.py`，与事实基准分工：那边考「事实记得准不准」，这边考「过程 → 经验 → 下一次更便宜」整条闭环。五类各 10 例，另有计数门禁 `test_gate_case_counts` 逐类点名，少一例即红。全部离线确定性运行（假提炼器/假嵌入器），判定口径如下：

| 类别 | 测什么 | 判定 |
|---|---|---|
| A 提炼产出 | 失败/重试/高成本/平凡/多产出轨迹、空内容与非法类型候选、向量与审计落盘 | 经验条数、kind、来源 episode、version、audit 记录与预期一致；平凡轨迹产出为 0 |
| B 安全门 | 轨迹与候选里的密钥：`sk-`、Google key、`ghp_`、eyJ 三段式令牌、Bearer、连接串口令、`password=` 键值对；注入候选与轨迹注入句 | 送提炼器的原文与入库内容中密钥原文 0 出现、占位符在位；注入候选被拦下，轨迹注入触发短语在送模型前被剥离 |
| C 去重与更新 | 同主题同内容、换内容、包含式主题、形近主题、同批重复、三次连续更新、残留重复行、换 kind、跨用户 | NOOP 不新增、UPDATE 版本 = 历史最大版本 + 1 且旧行全失效、形近不误合并、用户间不互串 |
| D 情境召回 | 经 applies_when / 内容 / 向量三路命中；无关任务、正交向量、低于余弦门槛、k 上限、排序、已失效、跨用户 | 该命中的命中（hybrid 需 reason 标注）、不该命中的为空，余弦门槛沿用 COS_MIN 0.66 |
| E 任务表现 | 10 个脚本化场景（端口探测、退避重试、缓存优先、查磁盘、先看日志、设超时、分页、套模板、迁移前备份等）走完整闭环后对比有/无简报 | 有简报时 token 严格更低、失败不更多；E01 与演示脚本同数（400 → 130，省 68%） |

**门禁指标**：50 例全过（当前实测 50/50，另计数门禁 1 例）；B 类密钥原文泄露 = 0。
**已知边界**：密钥脱敏只认已知形状，任意格式的自造密钥仍可能漏过（规则脱敏的固有边界，靠 B 类用例盯住已覆盖的形状）；E 类是脚本化策略，证明管线与记账，不代表真实 LLM 提炼质量，真实收益需接真实 Agent 对照跑。

## 其它保护性用例
- `test_body_cannot_pick_app`：请求体带 `app_id` → 422
- `test_write_is_idempotent`：同键写两次 → 同一 job
- `test_missing_user_id_is_422`：缺 `user_id` 或空串 → 422
- `test_claim_and_dead_letter`、`test_extractor_errors_retry_then_dead`：Worker 失败 3 次进死信，`last_error` 留下原因
- `test_pii_is_redacted_before_extractor_and_before_insert`：手机号/邮箱/身份证/银行卡在调用抽取器和入库前都被换成占位符
- `test_consent_off_at_processing_skips_write`：处理时总开关已关，不调用模型，任务完成且不入库
- `test_reprocess_job_is_idempotent`：同一任务再跑一次不会多出一行
- `test_decide_ignores_model_proposal`：模型建议 ADD/UPDATE/NOOP 不影响代码判定
- PG：`test_skip_locked_two_workers_get_different_jobs`：两个 Worker 并发领到不同任务
- PG：`test_migration_artifacts`、`test_service_account_is_not_superuser`
- `test_rejects_bad_app_id`：`APP_ID` 为空或含非法字符时拒绝启动

## 🚧 契约测试
`LocalMemoryProvider`（tiancode 本地）与 `RemoteMemoryProvider`（本服务）跑同一套用例，结果一致才算通过。

## 轨迹回放集（第十二轮）
`tests/test_replay_scenarios.py`：5 个贴近真实干活的长轨迹场景（部署端口、真库迁移、Git 推送、API 限流、日志脱敏），轨迹 10~13 步、含出错与绕路。每个场景断言两件事：当时的坑/策略进入后续任务的情境简报；简报体量 ≤ 原始轨迹渲染体量的 1/4（经验把「过程」压成「知识」的硬指标）。真实长轨迹步数更多，压缩比只会更高。

## 简报延迟基准（第十二轮，本机实测）
`scripts/bench_brief.py`，SQLite + 纯关键词路径（无嵌入服务），环境为 2 vCPU 容器，仅作量级参考：

| 经验条数 | p50 | p95 | max |
|---|---|---|---|
| 100 | 5.6ms | 7.9ms | 10.4ms |
| 1000 | 36.9ms | 45.3ms | 70.9ms |
| 5000 | 185.6ms | 199.6ms | 209.5ms |

读法：召回目前是全表打分，耗时随经验数近似线性增长。个人/小团队量级（≤1000 条）延迟在几十毫秒内，不构成问题；超过 ~5000 条应做索引化召回（事实侧已有 HNSW 索引未被召回路径使用，经验侧尚无向量索引），这是已知的下一件扩展工作，不要等它慢到影响使用才动。
