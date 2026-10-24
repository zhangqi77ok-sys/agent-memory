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

## 简报延迟基准（第十二轮建，第十四轮复测，第十四轮补完再测）
`scripts/bench_brief.py`，SQLite + 纯关键词路径（无嵌入服务），环境为 2 vCPU 容器，仅作量级参考。语料分两种：典型语料 = 10 个主题各有独立词汇（真实提炼出的经验按主题天然分散）；最坏语料 = 第十二轮的原始造数（全行共用同一套模板正文，固定查询与每行都共享 bigram，全员过关键词门槛）。

典型语料：

| 经验条数 | p50 | p95 | max |
|---|---|---|---|
| 100 | 4.4ms | 5.0ms | 13.6ms |
| 1000 | 6.8ms | 7.3ms | 9.4ms |
| 5000 | 19.4ms | 21.5ms | 69.6ms |

最坏语料：

| 经验条数 | p50 | p95 | max |
|---|---|---|---|
| 100 | 5.2ms | 5.6ms | 13.4ms |
| 1000 | 19.1ms | 21.8ms | 59.9ms |
| 5000 | 82.4ms | 132.7ms | 147.1ms |

历史数字：第十二轮（全表打分、无通道）5000 条 185.6ms（语料即现在的「最坏」）；第十四轮第一版（关键词门槛下推 SQL 的 LIKE 计数）典型 5000 条 78.2ms、最坏 150.4ms。

读法（补完）：第一版的 78.2ms 曾被判为「无文本索引时的地板」——复查时逐段剖析推翻了这个判断：纯作用域扫描只要 4ms，真正的地板是逐行 × 逐 token 的子串匹配（约 14µs/行），换 instr、先拼文本、子查询物化都在 62–90ms 徘徊。补完改倒排索引：每行的 token 集合落表（`memory_insight_token` / `memory_fact_token`，由 ORM 事件与父行同事务维护，迁移 0014 回填存量），召回探针两段式取数——先只在倒排表内按 (token, id) 索引数出过门 id，再按 id 回父表取列（一段式 JOIN 的计划不可控，SQLite 实测会选成按用户扫父表逐行回探，58ms）。倒排存的正是打分文本的 token 集合，COUNT 出的命中数与逐行 kw_score 的分子严格相等（等价性有性质测试守着），探针行不必再逐行打分。结果：5000 条典型语料 p50 **19.4ms**，第十二轮立的「<30ms」目标达成；1000 条以内约 7ms，规模敏感性基本消除。最坏语料的 82.4ms 是另一回事：全员真命中时，任何精确实现都要把 5000 条真候选搬出来逐条算排序分（实测取数约 33ms + 打分约 26ms），那是 O(命中数) 的地板，不是索引失效——真实语料不会长这样，保留这个数字是为了不只报好看的。写入侧代价：每行多维护约 50 条倒排记录（同事务 executemany），单行写入亚毫秒级；简报记账这类非文本更新只多一次索引比对、不重写。Postgres 侧向量分支维持「距离过线 + 按距离排序 + LIMIT 200」走 HNSW 的形态（第十四轮 EXPLAIN 结论不变）：真库实测走 ix_insight_hnsw / ix_fact_hnsw 的 Index Scan；过线行超过 200 时更远的会被截掉，关键词与置顶分支不受影响，SQLite 路径保持全量精确。
