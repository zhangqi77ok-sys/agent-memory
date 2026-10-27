# BrainWeave Memory

> 一句话：**主流记忆系统让 agent 记住你是谁；这个系统让 agent 记住活是怎么干的——并且能证明下一次干得更省。**

## 一、项目介绍

`BrainWeave Memory` 是一个自托管的远程记忆服务（Python / FastAPI / Postgres + pgvector），给多个 AI 编程工具提供同一份可插拔的记忆后端。

它的原料不是闲聊，而是**任务轨迹**：目标、步骤、出错点、重试与成本整份入库，提炼成可复用的经验；新任务开工前以**情境简报**递回。失败的任务同样是原料——栽过的跟头会被结构化地存下来，下次主动递到手边。

记忆分四类：

| 类别 | 内容 | 例子 |
|---|---|---|
| 事实（fact） | 长期有效的事实陈述，带版本与失效时间 | 用 Python 写后端、部署端口是 8080 |
| 偏好（preference） | 稳定的个人偏好，按 key 存放 | 语言偏好、代码风格偏好 |
| 经验（insight） | 从任务过程提炼的教训 / 策略 / 避坑 / 省钱招，带适用情境 | 某类报错先查端口占用再重启 |
| 轨迹（episode） | 任务全过程原文，提炼完只留骨架 | 一次排障的完整步骤与结果 |

服务形态：`api`（REST）+ `worker`（异步抽取与提炼）+ `web`（「我的记忆」面板），另有 MCP 门面供 AI 工具直连。应用由配置 `APP_ID` 固定（默认 `tiancode`），调用方不能切换。

## 二、功能清单

### 写入与提炼

- **对话写入**：`POST /v1/memories`，带 `Idempotency-Key` 幂等键，同一用户 + 同一键重复提交返回同一个 `job_id`，不重复写。
- **抽取管线**：Worker 异步处理——脱敏（手机号、邮箱、身份证号、银行卡号先换成占位符）→ LLM 抽取 → 代码裁决 ADD / UPDATE / NOOP → 旧行 `valid_to=now()` 失效、新行 `version+1`。模型连不上重试最多 3 次后进死信，坏输出整批丢弃、不入库。
- **任务过程提交**：`POST /v1/episodes`，任务结束后提交全过程（目标、步骤、结果、成本），Worker 复盘提炼为经验条目；提炼出的每条经验必须附轨迹原文引文，核对不上的标记未验证并降权。Codex / Grok / Gemini 三个 CLI 可装 SessionEnd 钩子自动采集（`scripts/capture_cli.py install`，默认关闭；hook 只写 marker、解析与提交离线到 harvester，脱敏与同意门、墓碑禁写全穿透）。截图 / 语音复盘以文本层入库为媒体备忘（`POST /v1/media`，`scripts/media_cli.py submit`；服务端不解析像素、不转录音频、不存原文件字节，文本层经提炼后进简报）。
- **语义去重**：主题措辞不同但意思相同（向量 ≥0.95）的事实与偏好不再并存；经验合并时败者的表述以附录留痕，不静默丢弃。

### 召回与简报

- **事实召回**：`POST /v1/memories/recall`，只返回当前用户、当前有效（`valid_to IS NULL`）的行。混合打分 0.7 向量 + 0.3 关键词；入选门槛：关键词命中（kw ≥ 0.10）或余弦 ≥ 0.66；零关键词佐证的纯向量行需余弦 ≥ 0.78（修复第 9 轮）；无向量时退化为纯关键词。简报与召回的预算装包是跳过而非截断——超预算的长条目让位给后面更小的条目。
- **情境简报**：`POST /v1/brief`，新任务开始前按情境捞经验（`k` 默认 5），打分与事实召回同构；简报自带降级信号（无嵌入器 / 嵌入失败时明说这是纯关键词结果）。
- **前瞻提醒**：`memory_remind` 武装「当 X 发生时提醒我做 Y」，新任务开简报时自动撞触发；提醒触发线与召回线已拆分，可独立重标；纯向量灰区命中（余弦 < 0.80）只展示、不消耗一次性提醒，意图不丢失。传 `session_id` 的简报还会结合会话上下文选触发时机：灰区命中在无关会话里暂缓、相关时展示，简短跟进任务可由会话上下文救援展示（救援永不消耗一次性提醒）。提醒可设可选截止时间 `expires_at`（NULL = 永不过期），到期即 `expired` 终态；每次展示 / 落幕 / 救援 / 暂缓 / 过期记一笔投递账（每提醒上限 200 条）。
- **织网与扩散**：事实与经验落库即织网——互相佐证、细化、泛化、相关、矛盾，都连成带类型与出身（规则推出 / 语义判定）的边；简报沿边扩散激活，捞到一条能带出字面不像但强相关的另一条；互相矛盾的经验在同一份简报里会被明确标出，不再并排沉默；巡检会把跨项目反复出现的同构经验抽象成通用原则。写时织网走倒排锚点（同桶、有界 200 peer、失败不拖垮写入），Markdown 导回与纠正的新行同样落库即织网。
- **睡眠巩固与遗忘曲线**：巡检定期做共用强化、死边清理、事实合并、聚类抽象；闲置且从未被用过的经验按遗忘曲线衰减、关行退出召回——会真遗忘的记忆，才值得信任。
- **类比召回**：简报第三臂，按“动作序列”找结构相同的活（失败→定位→切换→重建→验证的骨架），不看领域名词和工具名——上次修 Jenkins 流水线的套路，这次修 GitHub Actions 时能想起来；表面词重叠低但骨架相同的行也能进简报，原因标为 `analogical`。
- **工作记忆**：简报传 `session_id` 时启用会话缓冲（每会话 20 条）：上轮递出的经验还热着，名额未满时以 `working_memory` 原因补位，复用提升热度、容量满时沉降；缓冲只存 id 不存正文，存量行变化后不端旧内容，遗忘与开关全穿透。
- **奖励加权巩固**：好结果的记忆在睡眠巩固时优先——共用建边与簇抽象两条带上限的队列按 outcome 奖励分（人工反馈 1、任务成功 1、自动回流 0.5，上限 ±10）排序，不再只按曝光量排；衰减复述计数补上任务成功；奖励分是派生分、不写置信度，负奖励只降权不删除，自生成行出生奖励为 0。
- **跨项目矛盾对决**：两个项目各持一条主题相同、内容冲突的经验时，巡检不再越桶退役输家行——裁决循环只处理同桶对；跨项目对产生可审计的胜负记录（置信度 + 奖励分 + 新近性），败方只在跨项目简报里降权 0.7，本方项目简报与置顶行不受影响；情境不同的对走分化（关矛盾边、改连 related 边、可选合成条件原则），不对决；对决记录可经 `GET /v1/me/duels` 查询。

### 信任、删除与审计

- **曝光与应用分开记账**：简报递出只是「曝光」，不再给经验续命；后续任务的成败自动结回被递出的经验（半权计入置信度）。事实与偏好有佐证置信度，被独立来源反复证实的记得更牢、排得更前。
- **同意门**：默认关闭——用户没同意之前写入返回 `skipped=true`，什么都不存（连原始消息都不存），召回返回空；隐私说明改版时升版本号，老用户需要重新同意。同意门覆盖全部同步写入口（纠正、Markdown 导回、经验 PATCH 在未同意 / 已暂停 / 同意过期时一律 404）；删除与开关本身不受门限制。
- **遗忘是真的**：单条遗忘删同一 subject / key 的所有历史版本（含向量），id 记入 `forgotten_id`，内容规范化哈希记入 `forgotten_content`——此后同一内容以任何新 id 重发、重提炼、经 Markdown 导回或纠正改回时一律禁写；创建于删除之前的在途任务整单丢弃；备份恢复后按墓碑重删五类数据（事实 / 偏好 / 经验 / 轨迹 / 提醒），已遗忘的数据不复活。
- **导出与清空**：`GET /v1/me/memories/export` 返回设置、全部事实（含历史版本）与偏好（不含向量），写一条审计；全部清空删事实（含历史版本和向量）、偏好、摘要、原始消息与写入任务，设置（是否同意）保留。
- **管理端**：密钥开启的只读总览，可看所有人的记忆；每次查看写审计，密钥只存哈希；管理端读路径走独立的 BYPASSRLS worker 账号，未配 `WORKER_DATABASE_URL` 时管理端在 PG 上返回 503。

### 面板与星图

- **「我的记忆」面板**（`/memory/ui/`，Vue3 + Vite）：同意、列表、纠正、删除、清空、导出、总开关、任务状态；面板补服务密钥输入框，搜索框与加载态齐全；星图不通时明说未连通、不画假星（演示只认 `?demo=1`）。
- **星图**：星空布局 + 点击激活的脉冲传导——点击节点沿关联边发脉冲，亮度随边强度变化；面板每轮与后端能力同步更新。

### 经验走出记忆库

- **Skills 导出**：高置信经验 → `SKILL.md` / `AGENTS.md`，编程工具直接加载。
- **Markdown 同步**：记忆 → 人可读 `.md`（带 id，可进 git）；编辑器里改过的 `.md` 可导回（删除只报告不执行）。
- **历史导入**：冷启动时历史任务记录可批量导入成轨迹。

## 三、优点：为什么不用现成方案

| 现成方案 | 它强在哪 | 本系统为什么不一样 |
|---|---|---|
| Mem0 一类 | 从对话抽取事实与偏好，做个性化 | 本系统原料是任务轨迹，产出的是“下次怎么做”的经验，不是“你是谁” |
| Zep / Graphiti | 时序知识图谱，实体关系与变更追踪 | 本系统织的是记忆与记忆之间的联想网，不是世界实体知识图谱；实体关系推理请选 Zep |
| Letta | 自带记忆的 agent 框架 | 用它的记忆要先用它的运行时；本系统是纯记忆服务，不替你跑 agent |
| Claude-mem 一类单工具插件 | 开箱即用 | 记忆只属于那一个工具；本系统 MCP 门面是第一公民，多个工具共用一份经验库 |

除定位差异外，还有四条硬优点：

1. **效果可测，而且是门禁**：50 例经验评测是 CI 门禁的一部分；A/B 演示中带简报完成同类任务 token −68%；召回的余弦门槛 0.66 由真实向量实测校准（命中集 0.670–0.785、未命中集 0.488–0.656），每个阈值都有出处，不是拍脑袋填的；向量路误触发（14 探针实测误报率 0.857）已由机制收口——纯向量高线 + 灰区只展示不消耗，而不是调一个新阈值假装分得开。
2. **经验属于人，不属于工具**：MCP 门面与 REST 共用同一条代码路径，Codex / Grok / Gemini 等客户端连同一个库即互通；轨迹入库前先脱敏，删除权与数据导出覆盖事实 / 偏好 / 经验 / 轨迹四类，全链路审计，自托管、数据不出自己的库。
3. **规模有实测，不止于宣称**：单用户 100 万行规模下简报 p50 约 40ms（锚点探针，成本与总行数无关，只与候选上限有关）；第 41 轮以小规模多点实测 + CAP 界定论证收口，千万级仍是同机制外推、不是实测，见 `docs/eval.md`；巡检从全配对改为主题 bigram + 内容 token 锚点候选——同主题判定语义逐字不变，候选对有 10 万预算、超限按小时轮转分轮覆盖，任务处理排在巡检之前，巡检失败不再 2 秒热重试、下一小时再跑，按用户分片提交、PG 会话级咨询锁防双副本同跑，现行边加部分唯一索引且插入幂等，120 秒任务租约由后台线程续租（已过期不续）。
4. **边界主动说清**：不做世界实体知识图谱；不是 agent 框架；定位个人与小团队，没有托管云与 SDK 生态；摘要（`summarize`）是 v1 范围内已设计未实现的接口。

## 四、部署

### 形态一：个人自用（lite，一键版）

```bash
python -m app.lite              # 单进程全套：SQLite + 自动迁移 + 自动生成服务密钥
```

数据在 `~/.agent-memory/`（`AGENT_MEMORY_HOME` 可改），服务密钥存在该目录的 `service_key.txt`，MCP 与脚本用它连接。配上 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`（任意 OpenAI 兼容端点）后自动抽取与提炼开启；配 `GEMINI_API_KEY` 后向量召回开启。都不配也能用：关键词简报、Markdown 同步、Skills 导出照常。多用户 / 高并发请用下面的部署形态。

### 形态二：生产部署（Docker Compose + Jenkins）

```bash
cd deploy && cp .env.example .env   # 按 docs/deploy.md 填好密钥与模型地址
docker compose -p agent-memory up -d
curl -s 127.0.0.1:8090/readyz        # 就绪探针
alembic current                       # 应为 0031_media_ref
```

第 32 轮部署注意：无新迁移（仍到 0025）、无新环境变量，拉代码即生效；写时织网失败不影响写入，漏连的边由巡检补。

第 33 轮部署注意：新迁移 0026（经验动作序列倒排表），拉代码后跑一次 `python3 scripts/rebuild_moves.py` 回填存量行（未回填的旧行类比探针看不见，不报错）；无新环境变量。

第 34 轮部署注意：新迁移 0027（工作记忆缓冲表），无回填——缓冲从空开始，旧会话无热条目是正常态；无新环境变量；不传 `session_id` 的简报行为不变。

第 35 轮部署注意：无新迁移、无新环境变量、无回填——奖励分由已有计数器派生，拉代码即生效；睡眠队列顺序变化只影响巩固优先级，不影响召回排序与提醒触发。

第 36 轮部署注意：新迁移 0028（对决记录表），无回填——旧矛盾对在下一轮巡检对决步里自然建记录；无新环境变量；跨项目简报排序变化只影响败方行在跨项目读时的次序，本方项目简报与准入门槛不变。

第 37 轮部署注意：新迁移 0029（提醒表加 hold_count / last_held_at / last_timing 三列），无回填——旧行默认“从未暂缓、无时机结论”；无新环境变量；不传 `session_id` 的简报与触发检查行为不变，传了才启用会话上下文时机（灰区暂缓与上下文救援）。

第 38 轮部署注意：新迁移 0030（提醒表加 expires_at + 投递账本表 memory_reminder_delivery），无回填——旧提醒默认永不过期、旧展示不补账；无新环境变量；不设 expires_at 的提醒行为不变；巡检新增提醒过期步（只标已到截止时间的行）。

第 39 轮部署注意：无新迁移、无新服务端环境变量——采集是客户端脚本（`scripts/capture_cli.py install` 写三家 CLI 的用户层 SessionEnd hook，`harvest` 提交），拉代码即生效；不跑 install 的机器行为不变；采集提交带 `tombstone_check` 时命中已遗忘内容回 409、不落库；可选环境变量只有 `BRAINWEAVE_CAPTURE_OUTBOX` 与 `BRAINWEAVE_CAPTURE_OFF`（只列名）。

第 40 轮部署注意：新迁移 0031（媒体备忘来源 sidecar 表 memory_media_ref），无回填——旧 episode 没有 sidecar，不补；无新服务端环境变量；不跑 media 提交的机器行为不变；媒体提交默认就查 tombstone，命中已遗忘内容回 409、不落库；服务端不存原文件字节，external_uri 只是调用方提供的指针、服务端永不拉取。

第 41 轮部署注意：无新迁移、无新环境变量、无回填——本轮只是规模证明（小测量 + CAP 界定），不改召回机制；但证明挖出一条运维前提：大批量导入后必须跑一次 `ANALYZE`（或确认 autovacuum 已分析），否则规划器无统计量时锚点探针 stage-2 会走用户主题索引全扫、100k 规模下简报从约 35ms 退化到约 400ms，见 `docs/eval.md` 第 41 轮章节。

拓扑：`deploy/docker-compose.yml`（api + worker + pgvector + web），Postgres 不对外暴露端口；api 启动时先 `alembic upgrade head` 再起服务；worker 2 副本，用独立账号 `memsvc_worker`（BYPASSRLS、非超级用户、非 owner）；api 运行账号 `memsvc_app` 受行级安全策略约束；外层 Nginx 把 `/memory/` 转到 api、`/memory/ui/` 转到 web（模板见 `deploy/nginx/gateway-memory.conf.example`）。

持续部署走 Jenkins（`Jenkinsfile`）：gitleaks 扫密钥 → ruff + pytest（SQLite）→ 真 Postgres 测试 → 构建镜像 → 推 GHCR → SSH 部署到部署机 → `/readyz` 冒烟，失败自动回滚上一个成功 tag；同一提交不重复部署（去重检查 `.last_tag`）；回滚镜像不回滚数据库（先扩后缩），只有回滚到 0006 之前历史版本时才需先 `alembic downgrade 0005_app_id`。部署窗口 `/memory/` 502 约 1–2 分钟是已知现象（迁移 + 重启），别回滚。

关键环境变量（模板见 `deploy/.env.example`，真实值只放部署机 `.env` 与 Jenkins 凭据，绝不进仓库）：

| 变量 | 含义 |
|---|---|
| `APP_ID` | 固定应用标识，默认 `tiancode`，调用方不能切换 |
| `SERVICE_KEYS` | 业务调用方服务密钥，逗号分隔多值以便轮换 |
| `SERVICE_KEY_USERS` | 逗号分隔 `key:user` 对：绑密钥到用户，绑定后该密钥的请求身份钉死（推荐的多用户部署方式） |
| `ALLOW_UNBOUND_ASSERTION` | 默认 `0`：未绑定密钥的远程断言一律 `403`；仅在确认单用户自用的前提下设 `1` |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | Worker 抽取用的 OpenAI 兼容地址（以 `/v1` 结尾）、密钥、模型名；缺任何一项 Worker 启动失败，已入队任务不会被假数据写进记忆 |
| `GEMINI_API_KEY` | 向量召回用的嵌入模型密钥（不配则退化为纯关键词召回） |
| `EMBEDDING_DIM` | 生产固定 1536，千万别设 2（2 是测试假嵌入器用的） |
| `MEMORY_TTL_DAYS` | 记忆按时间自动过期天数，默认 `0`（不自动过期） |
| `RATE_LIMIT_READ_PER_MIN` / `RATE_LIMIT_WRITE_PER_MIN` | 按 `user_id` 每分钟限流阈值，默认 120 / 30（写 / 导出 / 删除更严） |
| `WORKER_DATABASE_URL` | 管理端读路径用的 worker 账号连接串；未配时管理端在 PG 上返回 503 |
| `DOCS_ENABLED` | Swagger UI 开关；prod 默认关闭，演示部署可设 `true` |

### 备份、恢复与回滚

- 备份：`scripts/backup.sh`（pg_dump，表级一致性快照；不含密钥，只备数据），产物 `<日期>.dump` + 同名 `.tombstones.json` 墓碑 sidecar（只含遗忘 id 与内容哈希，不含原文；恢复时合并 sidecar，即使活库损坏墓碑也不丢）。
- 恢复：生产路径 `deploy/restore.sh <dump> [目标库] [sidecar]`，然后 `alembic upgrade head`；流程是 pg_restore → 合并墓碑（线上库 + sidecar + 备份自带三源取并集）→ 按墓碑重删五类数据 → alembic 校验 → readyz；恢复前先停应用（往非 memory 目标库恢复时会 DROP 重建）。
- 演练：恢复演练每季度做一次（恢复到临时库、跑 `/readyz` 与一条简报查询，只备不练等于没备）；可执行的演练脚本 `scripts/restore_drill.sh`（自起自停 scratch 集群，不碰生产库）。
- 回滚：Smoke 失败自动回滚上一个成功 tag；手动回滚走 Jenkins 参数化（传 `ROLLBACK_TAG` 并二次确认）或部署机命令行。

### 健康检查与监控

- `GET /healthz` → 进程存活；`GET /readyz` → 数据库可连、可接流量；`GET /metrics` → Prometheus 文本格式（请求计数、5xx 数量、`memory_job` 各状态堆积与最旧任务年龄、就绪探针状态）。业务接口一律不需要公网开放，仅供内网及业务服务器调用。

## 五、怎么和别的系统对接

对接分四条路，按调用方类型选一条即可，四条路共用同一份数据与同一条代码路径。

### 路一：REST 接口（业务后端，推荐）

Base URL：`http(s)://<host>:8000`，所有业务接口带 `/v1` 前缀；所有 `/v1/*` 业务接口必须在请求头中携带服务密钥 `X-Service-Key`，缺失或错误返回 `401`；健康检查 `/healthz`、`/readyz` 不需要密钥。本服务不做用户登录认证，`user_id` 由业务调用方在上游完成身份校验与鉴权后传入（有 JSON body 的接口放 body，GET / DELETE 用 `?user_id=`）；缺失或空串返回 `422`；body 里出现 `app_id` 等未定义字段一律 `422`。

最小闭环（Python，完整示例与异常处理见 `docs/tiancode-integration.md`）：

```python
import requests

BASE = "http://127.0.0.1:8090"   # 或反代路径 /memory/v1
H = {"X-Service-Key": "<服务密钥>", "Content-Type": "application/json"}

# 1. 开启：用户同意并打开记忆开关（初次或协议升级时）
requests.put(f"{BASE}/v1/me/settings", json={
    "user_id": "alice", "memory_enabled": True,
    "consent_version": "2026-10-v1",   # 必须等于 GET 返回的 current_consent_version，否则 409
}, headers=H)

# 2. 会话开始前：召回并注入提示词
r = requests.post(f"{BASE}/v1/memories/recall", json={
    "user_id": "alice", "query": "我用什么语言", "k": 8, "token_budget": 1500,
}, headers=H)

# 3. 会话结束后：异步提交写入任务（不阻塞业务流程）
r = requests.post(f"{BASE}/v1/memories", json={
    "user_id": "alice", "session_id": "s-001",
    "messages": [{"msg_id": "m-1", "role": "user", "content": "我平时用 Python 写后端"}],
}, headers={**H, "Idempotency-Key": "sess-s-001-1"})

# 4. 任务开始前：情境简报；任务结束后：提交全过程
brief = requests.post(f"{BASE}/v1/brief", json={
    "user_id": "alice", "task": "排查部署端口冲突", "k": 5,
}, headers=H)
requests.post(f"{BASE}/v1/episodes", json={
    "user_id": "alice", "title": "排查部署端口冲突", "outcome": "success",
    "steps": [{"action": "查端口占用", "observation": "8080 被旧进程占用"}],
}, headers=H)
```

常用接口一览（完整接口、错误码与幂等规则见 `docs/api.md`）：

| 接口 | 用途 |
|---|---|
| `POST /v1/memories` | 写入记忆（异步，幂等） |
| `POST /v1/memories/recall` | 事实与偏好召回 |
| `POST /v1/brief` | 任务情境简报（经验召回） |
| `POST /v1/episodes` | 提交任务过程（提炼经验） |
| `GET /v1/me/memories` | 查看我的记忆（分页） |
| `GET /v1/me/insights` | 查看我的经验（分页，含已禁用） |
| `GET /v1/me/episodes/{id}` | 轨迹溯源详情 |
| `PATCH /v1/me/memories/{id}` | 纠正一条 |
| `DELETE /v1/me/memories/{id}` | 遗忘一条 |
| `DELETE /v1/me/memories` | 全部清空（前端必须二次确认） |
| `GET /v1/me/memories/export` | 个人数据全量导出 |
| `GET /v1/me/jobs` | 写入任务状态（pending / running / done / dead） |
| `GET/PUT /v1/me/settings` | 同意与总开关 |
| `GET /v1/me/identity` | 查看当前身份与模式（bound） |

错误码：401 服务密钥缺失或错误；404 资源不存在或不属于当前用户（不区分，避免暴露存在性）；409 隐私协议版本升级需重新同意（`code: consent_required`）或内容命中遗忘墓碑（`code: tombstoned`）；422 参数校验失败；429 超出 `user_id` 限流阈值（带 `Retry-After` 响应头）；5xx 按降级策略处理（见 `docs/architecture.md` §5.3）。

身份绑定（多用户部署推荐）：配置 `SERVICE_KEY_USERS`（逗号分隔 `key:user` 对）后，该密钥的请求即以绑定用户为准，查询与 body 里的 `user_id` 断言全部失效；未配置绑定时，默认不再接受远程断言——未绑定密钥带 `user_id` 调任何业务接口一律 `403`（`unbound_assertion_denied`）。绑定只收窄不放宽，映射里的 key 仍须先过 `SERVICE_KEYS` 校验。

### 路二：MCP 门面（AI 编程工具直连）

`app/mcp_server.py` 是 stdio MCP 服务器，与 REST 共用同一条代码路径；Codex、Gemini CLI 等 stdio 客户端连同一个库即互通，经验属于人、不属于工具。MCP 不受 `SERVICE_KEY_USERS` 绑定影响（工具传参面，见 `docs/architecture.md`）。

| 工具 | 用途 |
|---|---|
| `memory_write` | 写一条记忆（content + role + source + 幂等键） |
| `memory_search` | 关键词 / 向量搜索记忆 |
| `memory_list` | 列出记忆（分页） |
| `memory_episode_submit` | 提交任务过程 |
| `memory_brief` | 任务情境简报 |
| `memory_feedback` / `memory_fact_feedback` / `memory_preference_feedback` | 事实级 / 经验级反馈（正负反馈进置信度） |
| `memory_remind` | 武装前瞻提醒 |
| `memory_trigger_check` | 检查当前任务触发了哪些提醒 |
| `memory_links` | 查看某条经验的关联边 |
| `memory_graph_stats` | 联想网统计 |
| `memory_consolidation_report` | 睡眠巩固报告 |
| `memory_skills_export` | 导出 Skills（高置信经验 → SKILL.md / AGENTS.md） |
| `memory_forget` | 遗忘一条 |

### 路三：面板与导出文件（给人用）

- 面板：`/memory/ui/`，只调 `/v1/me/*`，仅内网可访问；演示填写 `user_id`，也可用 `?user_id=` 预填。
- Skills 导出：`SERVICE_KEY=<密钥> python3 scripts/export_skills.py --user <你> --out <仓库目录>`，高置信经验 → `SKILL.md` / `AGENTS.md`，编程工具直接加载。
- Markdown 同步：`python3 scripts/sync_markdown.py --pull --user <你> --dir <目录>`（记忆 → 人可读 `.md`，带 id，可进 git）；`--push` 把编辑器里改过的 `.md` 导回（删除只报告不执行）。
- 历史导入：`python3 scripts/import_episodes.py --user <你> --file history.jsonl`，冷启动时历史任务记录批量导入成轨迹。
- 管理端：`X-Admin-Key` 开启的只读总览（`/v1/admin/*`），每次查看写审计。

### 路四：脚本与度量（给评审与运维用）

- `scripts/episode_demo.py`：经验 A/B 演示（带简报 vs 不带简报）。
- `scripts/ab_eval.py`：简报 A/B 度量；`scripts/ranking_calibration.py` / `scripts/ranking_sensitivity.py`：排序标定与敏感性分析。
- `scripts/recalibrate_cos.py`：COS_MIN 再校准（只给建议值，不自动改阈值）；`scripts/reminder_precision.py` / `scripts/reminder_vector_precision.py`：提醒精度度量。
- `scripts/backup.sh` / `deploy/restore.sh` / `scripts/restore_drill.sh`：备份、恢复与恢复演练。
- 评测门禁：记住 / 记错 / 失效后不召回旧值 / 不串用户，四类评测见 `docs/eval.md`。

## 六、本地开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q                       # SQLite 单测
# 真 Postgres(+pgvector) 全套：scripts/test-pg.sh 需本机 docker，与 CI 的 Test on Postgres 阶段同口径
# 本机无 docker 时可用划痕集群脚本（见 AGENTS.md 里的 pg_suite_run.sh 口径）
uvicorn app.main:app --reload   # 默认 SQLite
```

交互式文档：非 prod 环境或配置 `DOCS_ENABLED=true` 时可访问 `/docs`（Swagger UI）或 `/openapi.json`；prod 默认关闭（返回 `404`）。

## 七、文档地图

| 文档 | 给谁看 | 内容 |
|---|---|---|
| [architecture.md](docs/architecture.md) | 所有人 | 唯一的架构出处 |
| [api.md](docs/api.md) | 调用方 | 接口、示例、错误码、幂等规则 |
| [tiancode-integration.md](docs/tiancode-integration.md) | 业务研发 | 接入指南、完整交互示例、异常处理 |
| [eval.md](docs/eval.md) | 开发 / 评审 | 四类评测与门禁 |
| [deploy.md](docs/deploy.md) / [runbook.md](docs/runbook.md) | 运维 | 部署、回滚、备份恢复、值班手册 |
| [adr/](docs/adr/) | 面试 / 评审 | 关键取舍 |
| [lessons/](docs/lessons/) | 演示 / 面试 | 面板、应用标识、抽取脱敏等合规演示 |
| [web/](web/) | 前端 | C 端面板（Vue3 + Vite，路径 `/memory/ui/`） |

设计参考：借鉴 Mem0 的抽取 + 多信号检索、Graphiti 的时序失效模型，见 `docs/architecture.md`。

## 八、当前状态与边界

- 已实现：身份（业务传 `user_id`，应用来自 `APP_ID`）、写入入队（幂等）、召回（混合打分）、遗忘（含墓碑禁写）、Worker 领取 / 死信、抽取管线（脱敏 → LLM → 代码判定 → 时间失效）、v1 迁移（Postgres 下含 pgvector / HNSW / GIN / RLS，迁移到 0030）、MCP 门面 15 个工具、经验记忆（提交 → 提炼 → 简报）、前瞻提醒（含会话上下文时机、TTL 与投递账本）、织网与扩散、睡眠巩固、面板与星图、管理端、Skills 与 Markdown 导出。
- 未实现：摘要（`summarize`，v1 范围内已设计未实现）。
- 已验证：Postgres 上的迁移与 Compose 启动、Jenkins 实跑、GHCR 推送、自动回滚、备份恢复 + 遗忘重删；SQLite 全套与 PG 全套在 2026-10-26 的修复第 10 轮收尾时全绿（538 / 560 通过）。
- 数据安全：代码在 GitHub；数据库每日 `pg_dump` 异地备份（见 `deploy/`），机器丢失可重建；服务仅供内网及业务服务器调用，不对公网开放。
