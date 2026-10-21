# 抽取管线：脱敏、判定、咖啡案例

Worker 领取 `memory_job` 之后才产生事实。同意写入只是入队；列表要等 Worker 跑完才看得到。

## 1. 脱敏发生在调用模型之前，也发生在入库之前

1. 原文里的手机号、邮箱、身份证号、银行卡号先换成 `[手机号]` `[邮箱]` `[身份证]` `[银行卡]`。
2. 送给 LLM 的是替换后的文本。模型看不到这些号码。
3. 模型返回的 `subject` / `content` 再走一遍同一套规则，然后才 `INSERT`。
4. 对应个保法「不记敏感个人信息」，也对应评测 `test_pii_is_redacted_before_extractor_and_before_insert`。

会话原文仍按架构保留（默认 90 天），事实行里不出现原始号码。

## 2. 模型可以建议，ADD / UPDATE / NOOP 由代码决定

同一用户、同一个 `subject`（偏好则是 `key`）：

| 库里当前有效值 | 候选值 | 结果 |
|---|---|---|
| 没有 | 任意非空 | ADD，`version=1` |
| 和候选规范化后相同 | 相同 | NOOP，不新插一行 |
| 和候选不同 | 不同 | UPDATE：旧行 `valid_to=now()`，新行 `version+1` |

模型如果把矛盾值标成 ADD，代码仍然 UPDATE。同一条任务再跑一次，相同内容会 NOOP，不会复制出第二行。

列表和召回只返回 `valid_to IS NULL`。导出是可携带权，历史版本也在，旧行带 `valid_to`。

指令型句子（「记住我是管理员」「忽略之前的规则」）会从送给模型的文本里删掉；就算模型仍把它当成候选，代码也会丢掉，不入库。

## 3. 咖啡：新说法让旧版本失效

1. 用户已同意。写入「喜欢咖啡」。Worker 抽出 `subject=咖啡`、`content=喜欢咖啡`。
2. 再写入「不喝咖啡了」。同一 subject、值矛盾 → 旧行写上 `valid_to`，新行版本 +1。
3. `GET /v1/me/memories` 只有「不喝咖啡了」。`POST /v1/memories/recall` 查询「咖啡」不会返回已失效的那条。
4. 这是合并门禁：`test_coffee_contradiction_invalidates_old_version`。

## 4. 处理时再看一次总开关

入队时开着、跑到 Worker 时用户关掉记忆：不调用模型，不写事实，任务记 `done`（审计 `decision=skip`）。已经关掉再调用 `POST /v1/memories` 则连任务都不创建（`skipped=true`）。

## 5. 失败三次后进入 dead

连不上模型或返回 5xx：`attempts+1`，未超限回到 `pending`，运行中的 Worker 按 `min(2^attempts, 30)` 秒退避。第三次仍失败则 `status=dead`，`last_error` 留下原因。修完可以按 runbook 把该行改回 `pending` 并清零 `attempts`。

模型输出不是合法 JSON：丢弃这一批，记审计 `discard`，任务完成，不重试，不入库。

缺 `LLM_BASE_URL`、`LLM_API_KEY` 或 `LLM_MODEL` 时 Worker 启动即失败，不会换成假抽取器。测试里的假抽取器是显式注入的。

这次上线前已经入队、还没处理的任务没有 `job_id`，Worker 会把它们完成且不抽事实。要补记就换一个新的 `Idempotency-Key` 再写一次（旧键会当成重复请求）。

## 本地与 CI

```bash
. .venv/bin/activate
pytest -q tests/test_extraction.py
```

默认 SQLite。Jenkins「Test on Postgres」设置 `TEST_DATABASE_URL` 后同一套用例在真实库上再跑一遍。生产密钥只放在部署机 `.env` / Jenkins 凭据 `deploy-env`。
