# 纯记忆服务 + app_id 去租户 实现计划（PR1 双写 / PR2 收尾）

> **给执行者：** 按任务顺序逐步执行，每步打勾（`- [ ]` → `- [x]`）。每个任务先写失败测试 → 跑红 → 最小实现 → 跑绿 → 提交。不要跳步，不要合并任务。

**Goal：** 把 agent-memory 改成纯记忆服务：删除 JWT 解析，业务侧传 `user_id`，服务用配置 `APP_ID`（默认 `tiancode`）补应用维度；库表 `tenant_id` 经两次发布改名为 `app_id`。

**Architecture：** PR1 新增 Alembic revision `0005_app_id`：加 `app_id`（server_default `'tiancode'`）→ 回填 → NOT NULL → 并行索引/约束；ORM 双写两列、读只用 `app_id`；`UserContext(app_id, user_id)` 由请求里的 `user_id` + `settings.app_id` 构造。PR2 新增 `0006_drop_tenant_id`：删旧约束/索引/列，模型去掉 `tenant_id`，清扫「租户 / tenant / JWT」文案。

**Tech Stack：** Python 3.12、FastAPI、SQLAlchemy 2、Alembic、pydantic-settings、Postgres 16 + pgvector（FORCE RLS）、SQLite（默认单测）、pytest；前端 Vue 3 + Vite + vitest + Playwright。

**Global Constraints：**
- 不修改已部署的 `migrations/versions/0001_init.py` ~ `0004_session_message_job_id.py`（例外见 PR2 Task 2.1，需 maxzq 明确批准）。
- 不提交未跟踪的 `app/extraction.py`；`git add` 一律写具体路径，禁止 `git add -A` / `git add .`。
- 对外文案只说「应用」；错误码不引入 token 过期类。
- 每个 PR：`pytest -q` 绿（SQLite）+ 有条件时 `TEST_DATABASE_URL=... pytest -q tests/test_postgres.py` 绿 → 开 PR → New Bot 审 → 作者合并 → Jenkins → 网关 `/readyz` 200 + 冒烟 → 向 maxzq 汇报构建号。
- 密钥不进仓库。

---

## 代码现状（已核对，2026-10-21 main@3433e4c）

| 位置 | 现状 |
| --- | --- |
| `app/auth.py:9-25` | `UserContext(tenant_id, user_id)`；`current_user(authorization=Header(""))` 用 PyJWT 解 `sub`/`app`，缺失→401 |
| `app/config.py:9-10,24-26` | `jwt_secret` / `jwt_alg`；`check_settings` 非 dev/test 且 `jwt_secret` 以 `dev-only-` 开头时拒绝启动 |
| `app/db.py:28-34` | `after_begin` 钩子：`session.info["user_id"]` → `set_config('app.user_id', …, true)`（RLS 用） |
| `app/models.py:35-37` | `UserScoped.tenant_id default="tiancode"`；`AuditLog.tenant_id`（112 行，非 UserScoped，无默认值） |
| `app/models.py:63,96,128` | `ix_fact_user_subject(tenant_id,user_id,subject)`、`uq_job_idem(tenant_id,user_id,idempotency_key)`、`uq_user_setting(tenant_id,user_id)` |
| `migrations/versions/0001_init.py:25` | Postgres 专有：`ix_fact_active ON memory_fact (tenant_id, user_id) WHERE valid_to IS NULL`（原生 SQL）；0001 用**当前模型** `create_all` |
| `0002_force_rls.py:14` | RLS 策略只看 `user_id = NULLIF(current_setting('app.user_id', true), '')`；不涉及 tenant_id |
| `0003_user_setting.py:15,21` | 显式建 `tenant_id` 列与 `uq_user_setting` |
| `app/schemas.py:6-8` | `Strict(extra="forbid")`，注释即「body 里出现 user_id 直接 422」 |
| `app/api/memories.py:15-21` | `get_db` 依赖 `current_user`，把 `user_id` 放进 `session.info` |
| `app/api/me.py:19-20` | `_mine()` 返回 `(tenant_id==, user_id==)` |
| `app/worker.py:57,63,83,102,115,125,138` | Worker 全程用 `job.tenant_id` |
| `tests/conftest.py:18,71-73` | `token()` 用 PyJWT 签名；`consent()` 走 `headers=token(u)` |
| `web/src/auth.ts`、`api.ts:17-22,87-90`、`App.vue:15,22,158-169,180` | `?token=` → localStorage → `Authorization: Bearer` |

---

## 文件结构图

| 文件 | PR1 | PR2 |
| --- | --- | --- |
| `app/config.py` | 删 `jwt_secret`/`jwt_alg`，加 `app_id: str = "tiancode"`；`check_settings` 改校验 `app_id` | — |
| `app/auth.py` | 重写：`UserContext(app_id,user_id)`、`user_from_query`、`bind_user` | 无变化 |
| `app/schemas.py` | `UserScopedRequest` 基类（`user_id` 必填）；`WriteRequest`/`RecallRequest`/`CorrectRequest`/`SettingsIn` 继承 | — |
| `app/models.py` | `UserScoped` 加 `app_id`；`AuditLog` 加 `app_id`；双写 `before_flush`；并行索引/约束 | 删 `tenant_id`、旧索引、双写钩子 |
| `app/db.py` | `get_db()` 不再依赖用户 | — |
| `app/api/memories.py`、`app/api/me.py`、`app/recall.py`、`app/worker.py` | 过滤/写入全换 `app_id` | — |
| `migrations/versions/0005_app_id.py` | 新建 | — |
| `migrations/versions/0006_drop_tenant_id.py` | — | 新建 |
| `tests/conftest.py` + 各测试 | 去 JWT，`uid()` / `consent()` 改传 `user_id` | 加「无 tenant_id 列」断言 |
| `tests/test_app_id.py` | 新建（迁移 / 双写 / 身份） | 追加 PR2 断言 |
| `pyproject.toml`、`.env.example`、`deploy/.env.example` | 删 `pyjwt`、`JWT_SECRET`，加 `APP_ID` | — |
| `web/src/auth.ts`→`web/src/user.ts`、`api.ts`、`App.vue`、`__tests__/`、`e2e/` | 输入框填 `user_id` | — |
| `docs/api.md`、`docs/architecture.md`、`docs/eval.md`、`docs/deploy.md`、`docs/lessons/me-ui.md`、`README.md`、`web/README.md` | 身份与 API 约定 | 清扫「租户」 |
| `docs/lessons/app-id-pure-memory.md` | 新建教学课 | 补 PR2 一节 |

**身份传递约定（本计划定稿）：** 有 JSON body 的接口（`POST /v1/memories`、`POST /v1/memories/recall`、`PATCH /v1/me/memories/{id}`、`PUT /v1/me/settings`）`user_id` 放 body；无 body 的接口（`GET /v1/me/memories`、`GET /v1/me/memories/export`、`GET /v1/me/settings`、`GET /v1/me/jobs`、`DELETE /v1/me/memories`、`DELETE /v1/me/memories/{id}`）用查询参数 `?user_id=`。缺失或空串一律 422。`app_id` 不接受调用方传入（body 里带 `app_id` 仍因 `extra="forbid"` → 422）。

---

# Part 1 — PR1：双写 + 去 JWT

分支：`feat/app-id-dual-write`（从最新 `main` 切）。

### Task 1.1：配置 `APP_ID`，删 JWT 配置

**Files：** `app/config.py`、`tests/test_config.py`、`tests/test_docs_flag.py`、`tests/test_extraction.py:78`、`pyproject.toml`、`.env.example`、`deploy/.env.example`

**Interfaces：** `Settings.app_id: str = "tiancode"`；`check_settings(s)`：`app_id` 为空或含非 `[a-z0-9_-]` 字符 → `RuntimeError`。

- [ ] **Step 1：写失败测试**，替换 `tests/test_config.py` 全文：

```python
import pytest

from app.config import Settings, check_settings


def test_app_id_defaults_to_tiancode():
    assert Settings().app_id == "tiancode"


def test_prod_starts_without_any_jwt_secret():
    check_settings(Settings(app_env="prod"))  # 不再需要 JWT_SECRET


@pytest.mark.parametrize("bad", ["", "Tian Code", "租"])
def test_rejects_bad_app_id(bad):
    with pytest.raises(RuntimeError):
        check_settings(Settings(app_id=bad))


def test_jwt_settings_are_gone():
    assert not hasattr(Settings(), "jwt_secret")
```

- [ ] **Step 2：跑红** `pytest -q tests/test_config.py` → 预期 `AttributeError: app_id` / `jwt_secret` 仍存在。
- [ ] **Step 3：实现** `app/config.py`：删第 9-10 行，加 `app_id: str = "tiancode"  # 固定应用标识，不由调用方切换`；`check_settings` 改为：

```python
import re

def check_settings(s: Settings = settings) -> None:
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", s.app_id or ""):
        raise RuntimeError("APP_ID 只能是 1-64 位小写字母、数字、- 或 _")
```

- [ ] **Step 4：同步其它测试**：`tests/test_docs_flag.py:9,13` 与 `tests/test_extraction.py:78` 删除 `jwt_secret=...` 实参（pydantic-settings 默认 `extra="forbid"`，留着会报错）。
- [ ] **Step 5：依赖与样例**：`pyproject.toml` 删 `"pyjwt>=2.8", `；`.env.example:4`、`deploy/.env.example:7` 删 `JWT_SECRET=…`，加 `APP_ID=tiancode`。
- [ ] **Step 6：跑绿** `pytest -q tests/test_config.py tests/test_docs_flag.py` → PASS（其余测试此时仍依赖 JWT，Task 1.4 前会红，属预期；本任务只跑这两个文件）。
- [ ] **Step 7：提交**
```bash
git add app/config.py tests/test_config.py tests/test_docs_flag.py tests/test_extraction.py pyproject.toml .env.example deploy/.env.example
git commit -m "feat(config): 固定 APP_ID，移除 JWT 配置"
```

### Task 1.2：模型加 `app_id` + 双写钩子 + 并行约束

**Files：** `app/models.py`、`tests/test_app_id.py`（新建）

**Interfaces：** `UserScoped.app_id: Mapped[str]`（`default=settings.app_id`, `server_default="tiancode"`）；`AuditLog.app_id` 同上；`before_flush` 监听：新对象若 `app_id` 为空则取 `settings.app_id`，并令 `tenant_id = app_id`。新约束名：`ix_fact_app_user_subject`、`uq_job_idem_app`、`uq_user_setting_app`。

- [ ] **Step 1：写失败测试** `tests/test_app_id.py`：

```python
from app import models as m


def test_new_rows_dual_write_app_id_and_tenant_id(db):
    f = m.MemoryFact(user_id="alice", subject="s", content="c")
    a = m.AuditLog(user_id="alice", action="recall", fact_ids=[])
    db.add_all([f, a])
    db.commit()
    db.refresh(f)
    db.refresh(a)
    assert (f.app_id, f.tenant_id) == ("tiancode", "tiancode")
    assert (a.app_id, a.tenant_id) == ("tiancode", "tiancode")


def test_parallel_constraints_exist():
    names = {c.name for c in m.MemoryJob.__table__.constraints} | {c.name for c in m.UserSetting.__table__.constraints}
    assert {"uq_job_idem", "uq_job_idem_app", "uq_user_setting", "uq_user_setting_app"} <= names
    assert {"ix_fact_user_subject", "ix_fact_app_user_subject"} <= {i.name for i in m.MemoryFact.__table__.indexes}
```

- [ ] **Step 2：跑红** `pytest -q tests/test_app_id.py` → `AttributeError: app_id`。
- [ ] **Step 3：实现** `app/models.py`：

```python
from sqlalchemy import event
from sqlalchemy.orm import Session

class UserScoped:
    app_id: Mapped[str] = mapped_column(String(64), default=lambda: settings.app_id, server_default="tiancode")
    tenant_id: Mapped[str] = mapped_column(String(64), default="tiancode")  # PR2 删除；过渡期与 app_id 同值
    user_id: Mapped[str] = mapped_column(String(64), index=True)
```

`MemoryFact.__table_args__` 改为 `(Index("ix_fact_user_subject", "tenant_id", "user_id", "subject"), Index("ix_fact_app_user_subject", "app_id", "user_id", "subject"))`；`MemoryJob` 加 `UniqueConstraint("app_id", "user_id", "idempotency_key", name="uq_job_idem_app")`；`UserSetting` 加 `UniqueConstraint("app_id", "user_id", name="uq_user_setting_app")`；`AuditLog` 加与 `UserScoped` 相同的 `app_id` 列，`tenant_id` 加 `default="tiancode"`。文件末尾：

```python
_DUAL = (SessionMessage, MemoryFact, MemoryPreference, SessionSummary, MemoryJob, AuditLog, UserSetting)


@event.listens_for(Session, "before_flush")
def _dual_write_tenant_id(session, flush_context, instances):
    """过渡期双写：tenant_id 永远跟 app_id 同值。PR2 连同 tenant_id 一起删掉。"""
    for obj in session.new:
        if isinstance(obj, _DUAL):
            obj.app_id = obj.app_id or settings.app_id
            obj.tenant_id = obj.app_id
```

- [ ] **Step 4：跑绿** `pytest -q tests/test_app_id.py` → 2 passed。
- [ ] **Step 5：提交** `git add app/models.py tests/test_app_id.py && git commit -m "feat(models): app_id 列与双写 tenant_id"`

### Task 1.3：Alembic `0005_app_id`

**Files：** `migrations/versions/0005_app_id.py`（新建）、`tests/test_postgres.py`、`tests/test_app_id.py`

**Interfaces：** `revision = "0005_app_id"`，`down_revision = "0004_session_message_job_id"`。表清单 `TABLES = ("session_message", "memory_fact", "memory_preference", "session_summary", "memory_job", "audit_log", "user_setting")`。

> 注意：0001 用当前模型 `create_all`，**全新库**跑到 0005 时 `app_id` 列和 `ix_fact_app_user_subject`、`uq_job_idem_app` 可能已由 0001 建好（`user_setting` 由 0003 显式建，不会有）。所以 0005 和 0004 一样按存在性判断，幂等。

- [ ] **Step 1：写迁移测试**（Postgres 专用，追加到 `tests/test_postgres.py`；文件顶部已有 `pytestmark` 跳过无 PG 的情况）：

```python
def test_0005_backfills_app_id_from_tenant_id():
    from alembic import command
    from alembic.config import Config
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0004_session_message_job_id")
    with admin_engine.begin() as c:
        c.execute(text("INSERT INTO memory_fact (id, tenant_id, user_id, subject, content, valid_from, version) "
                       "VALUES ('f1', 'legacy', 'alice', 's', 'c', now(), 1)"))
        c.execute(text("INSERT INTO user_setting (id, tenant_id, user_id, memory_enabled, updated_at) "
                       "VALUES ('u1', 'tiancode', 'alice', true, now())"))
    command.upgrade(cfg, "head")
    with admin_engine.begin() as c:
        assert c.execute(text("SELECT app_id FROM memory_fact WHERE id='f1'")).scalar() == "legacy"
        assert c.execute(text("SELECT app_id FROM user_setting WHERE id='u1'")).scalar() == "tiancode"
        nullable = c.execute(text("SELECT is_nullable FROM information_schema.columns "
                                  "WHERE table_name='memory_job' AND column_name='app_id'")).scalar()
        assert nullable == "NO"
        idx = {r[0] for r in c.execute(text("SELECT indexname FROM pg_indexes WHERE tablename='memory_fact'"))}
        assert {"ix_fact_active", "ix_fact_active_app", "ix_fact_app_user_subject"} <= idx
```

（需要 `from tests.conftest import admin_engine`；`0005` 的 downgrade 必须可用，见 Step 3。）同时在 `test_migration_artifacts`（第 19 行）集合里加 `"ix_fact_active_app"`。

- [ ] **Step 2：跑红** `TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/memtest pytest -q tests/test_postgres.py -k "0005 or artifacts"` → 失败（无 0005）。无 PG 环境时跳过此步，改在 Step 4 用 `alembic upgrade head` 对 SQLite 冒烟。
- [ ] **Step 3：实现** `migrations/versions/0005_app_id.py`：

```python
"""app_id：tenant_id 的新名字（双写过渡第一步）。

加列（server_default='tiancode'，旧镜像回滚后 INSERT 不带 app_id 也能写）→ 回填 → NOT NULL → 并行索引/约束。
旧 tenant_id 与旧索引保留，0006 再删。0001 用当前模型 create_all，新库上部分对象已在，所以全部按存在性判断。
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_app_id"
down_revision = "0004_session_message_job_id"

TABLES = ("session_message", "memory_fact", "memory_preference", "session_summary", "memory_job",
          "audit_log", "user_setting")
UNIQUES = {"memory_job": ("uq_job_idem_app", ["app_id", "user_id", "idempotency_key"]),
           "user_setting": ("uq_user_setting_app", ["app_id", "user_id"])}


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for t in TABLES:
        if "app_id" not in {c["name"] for c in insp.get_columns(t)}:
            op.add_column(t, sa.Column("app_id", sa.String(64), nullable=True, server_default="tiancode"))
        op.execute(f"UPDATE {t} SET app_id = tenant_id WHERE tenant_id IS NOT NULL")
        with op.batch_alter_table(t) as b:  # batch：SQLite 也能 ALTER NOT NULL
            b.alter_column("app_id", existing_type=sa.String(64), nullable=False, server_default="tiancode")
    if "ix_fact_app_user_subject" not in {i["name"] for i in insp.get_indexes("memory_fact")}:
        op.create_index("ix_fact_app_user_subject", "memory_fact", ["app_id", "user_id", "subject"])
    for t, (name, cols) in UNIQUES.items():
        if name not in {u["name"] for u in insp.get_unique_constraints(t)}:
            with op.batch_alter_table(t) as b:
                b.create_unique_constraint(name, cols)
    if bind.dialect.name == "postgresql":
        op.execute("CREATE INDEX IF NOT EXISTS ix_fact_active_app ON memory_fact (app_id, user_id) "
                   "WHERE valid_to IS NULL")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_fact_active_app")
    for t, (name, _) in UNIQUES.items():
        with op.batch_alter_table(t) as b:
            b.drop_constraint(name, type_="unique")
    op.drop_index("ix_fact_app_user_subject", table_name="memory_fact")
    for t in TABLES:
        with op.batch_alter_table(t) as b:
            b.drop_column("app_id")
```

RLS：不改策略（只看 `user_id`）。`audit_log` 本就没有 RLS；授权沿用 0002 的 `ALTER DEFAULT PRIVILEGES`，加列不需要重新 GRANT。

- [ ] **Step 4：跑绿** 有 PG：同 Step 2 命令 → PASS。无 PG：`rm -f /tmp/mig.db && DATABASE_URL=sqlite:////tmp/mig.db alembic upgrade head && DATABASE_URL=sqlite:////tmp/mig.db alembic downgrade 0004_session_message_job_id && DATABASE_URL=sqlite:////tmp/mig.db alembic upgrade head` → 无报错。
- [ ] **Step 5：提交** `git add migrations/versions/0005_app_id.py tests/test_postgres.py && git commit -m "feat(migrations): 0005 加 app_id 并回填"`

### Task 1.4：去 JWT —— `UserContext(app_id,user_id)` + `user_id` 入参

**Files：** `app/auth.py`、`app/schemas.py`、`app/api/memories.py`、`app/api/me.py`、`tests/conftest.py`、`tests/test_api.py`、`tests/test_me.py`、`tests/test_extraction.py`、`tests/test_postgres.py:72-90`、`tests/test_app_id.py`

**Interfaces：**

```python
# app/auth.py
@dataclass(frozen=True)
class UserContext:
    app_id: str
    user_id: str

def user_ctx(user_id: str) -> UserContext            # app_id 取 settings.app_id
def user_from_query(user_id: str = Query(..., min_length=1, max_length=64)) -> UserContext
def bind_user(db: Session, u: UserContext) -> UserContext   # 写 db.info["user_id"]，供 RLS 钩子
# app/schemas.py
class UserScopedRequest(Strict):
    user_id: str = Field(min_length=1, max_length=64)
```

`get_db()`（`app/api/memories.py`）不再依赖 `current_user`，只开 `SessionLocal()`；各路由在**第一次查询前**调用 `bind_user(db, ...)`——`after_begin` 在首条 SQL 时才触发，读取的是那一刻的 `session.info`，所以 RLS 依旧生效。

- [ ] **Step 1：改测试夹具** `tests/conftest.py`：删 `import jwt`（第 18 行）和 `from app.config import settings`（若无他用），把 `token()` 换成：

```python
def uid(user: str) -> dict:
    """查询参数形式的身份（GET/DELETE 用）。业务侧自己验完会话后直接传 user_id。"""
    return {"user_id": user}


def consent(client, *users: str) -> None:
    from app.schemas import CONSENT_VERSION
    for u in users:
        r = client.put("/v1/me/settings",
                       json={"user_id": u, "memory_enabled": True, "consent_version": CONSENT_VERSION})
        assert r.status_code == 200, r.text
```

- [ ] **Step 2：改调用点**（机械替换，逐文件）：
  - body 接口：`headers=token("alice"), json={...}` → `json={"user_id": "alice", ...}`；写入接口保留 `headers={"Idempotency-Key": key}`。
  - 无 body 接口：`headers=token("alice")` → `params=uid("alice")`。
  - `tests/test_api.py:17-18` `test_requires_token` 改名并改为：

```python
def test_missing_user_id_is_422(client):
    assert client.post("/v1/memories/recall", json={"query": "x"}).status_code == 422
    assert client.post("/v1/memories/recall", json={"query": "x", "user_id": ""}).status_code == 422
```

  - `tests/test_api.py:28-30` `test_body_cannot_carry_identity` 改为「不能指定应用」：

```python
def test_body_cannot_pick_app(client):
    r = client.post("/v1/memories/recall", json={"user_id": "alice", "query": "x", "app_id": "other"})
    assert r.status_code == 422
```

  - `tests/test_me.py:118-121` `test_me_endpoints_require_token` → `test_me_endpoints_require_user_id`，断言值 `401` → `422`。
  - `tests/test_extraction.py` 第 34、177、180、208、219、224、280、342 行；`tests/test_me.py` 第 8、18、22、34-41、51、55、64-77、90、104、114、130 行；`tests/test_postgres.py` 第 72-87 行（`from tests.conftest import consent, token` → `consent, uid`）。
  - `tests/test_extraction.py:99,122` 断言的是 LLM 接口的 `Authorization: Bearer test-key`，**不要动**。
- [ ] **Step 3：加身份测试** 追加到 `tests/test_app_id.py`：

```python
def test_rows_written_via_api_carry_config_app_id(client, db):
    from tests.conftest import consent
    consent(client, "alice")
    r = client.post("/v1/memories", headers={"Idempotency-Key": "k"},
                    json={"user_id": "alice", "session_id": "s", "messages": [{"msg_id": "1", "role": "user", "content": "hi"}]})
    assert r.status_code == 202
    job = db.query(m.MemoryJob).one()
    assert (job.app_id, job.tenant_id, job.user_id) == ("tiancode", "tiancode", "alice")
    assert db.query(m.UserSetting).filter_by(user_id="alice").one().app_id == "tiancode"


def test_bearer_header_is_ignored(client):
    r = client.get("/v1/me/settings", headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 422  # 只认 user_id，不解析任何 token


def test_other_app_rows_are_invisible(client, db):
    from tests.conftest import consent, uid
    consent(client, "alice")
    db.add(m.MemoryFact(app_id="other", user_id="alice", subject="s", content="别的应用的记忆"))
    db.commit()
    assert client.get("/v1/me/memories", params=uid("alice")).json()["total"] == 0
```

- [ ] **Step 4：跑红** `pytest -q` → 大量 422/401 不符、`ImportError: uid`。
- [ ] **Step 5：实现 `app/auth.py`**（全文替换）：

```python
"""身份由业务侧传入：记忆服务不解析 JWT、不做登录。app_id 来自配置，调用方不能切换。"""
from dataclasses import dataclass

from fastapi import Query
from sqlalchemy.orm import Session

from app.config import settings


@dataclass(frozen=True)
class UserContext:
    app_id: str
    user_id: str


def user_ctx(user_id: str) -> UserContext:
    return UserContext(app_id=settings.app_id, user_id=user_id)


def user_from_query(user_id: str = Query(..., min_length=1, max_length=64)) -> UserContext:
    return user_ctx(user_id)


def bind_user(db: Session, u: UserContext) -> UserContext:
    """必须在本会话第一条 SQL 之前调用：after_begin 钩子据此设置 app.user_id（RLS）。"""
    db.info["user_id"] = u.user_id
    return u
```

- [ ] **Step 6：实现 `app/schemas.py`**：第 7 行注释改为「禁止多余字段：body 里出现 app_id 等未定义字段直接 422」；新增 `UserScopedRequest`；`WriteRequest`、`RecallRequest`、`CorrectRequest`、`SettingsIn` 的基类从 `Strict` 改为 `UserScopedRequest`。
- [ ] **Step 7：实现 `app/api/memories.py`**：

```python
from app.auth import UserContext, bind_user, user_ctx, user_from_query

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _audit(db, u, action, ids):
    db.add(m.AuditLog(app_id=u.app_id, user_id=u.user_id, action=action, fact_ids=ids))

@router.post("/memories", status_code=202, response_model=WriteAccepted)
def write(req: WriteRequest, response: Response, idempotency_key: str = Header(...),
          db: Session = Depends(get_db)):
    u = bind_user(db, user_ctx(req.user_id))
    ...  # 原逻辑，tenant_id → app_id

@router.post("/memories/recall", response_model=RecallResponse)
def recall(req: RecallRequest, db: Session = Depends(get_db)):
    u = bind_user(db, user_ctx(req.user_id))
    ...

@router.delete("/me/memories/{item_id}", status_code=204)
def forget(item_id: str, u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    bind_user(db, u)
    ...
```

文件内所有 `m.X.tenant_id == u.tenant_id` → `m.X.app_id == u.app_id`（第 36、52、64、77-78、89-91 行），构造函数 `tenant_id=u.tenant_id` → `app_id=u.app_id`（第 40、45 行；`tenant_id` 由 Task 1.2 的钩子补）。`fact.tenant_id == u.tenant_id` → `fact.app_id == u.app_id`。
- [ ] **Step 8：实现 `app/api/me.py`**：`from app.auth import UserContext, bind_user, user_ctx, user_from_query`；`_mine` 改 `(model.app_id == u.app_id, model.user_id == u.user_id)`；GET/DELETE 路由 `Depends(current_user)` → `Depends(user_from_query)` 并在函数首行 `bind_user(db, u)`；`correct(item_id, req, db)` 与 `write_settings(req, db)` 首行 `u = bind_user(db, user_ctx(req.user_id))`；第 64、74 行 `old.tenant_id == u.tenant_id` → `old.app_id == u.app_id`；第 66、76、143 行构造参数换 `app_id=u.app_id`。
- [ ] **Step 9：跑绿** `pytest -q` → 全绿（worker 相关测试若仍引用 `tenant_id` 失败，留到 Task 1.5）。确认：`grep -rn "current_user\|jwt" app tests` 无结果（`test_extraction.py` 的 LLM `Bearer test-key` 除外）。
- [ ] **Step 10：提交**
```bash
git add app/auth.py app/schemas.py app/api/memories.py app/api/me.py tests/conftest.py tests/test_api.py tests/test_me.py tests/test_extraction.py tests/test_postgres.py tests/test_app_id.py
git commit -m "feat(api): 去掉 JWT，业务传 user_id，UserContext(app_id,user_id)"
```

### Task 1.5：召回与 Worker 改用 `app_id`

**Files：** `app/recall.py:37`、`app/worker.py:57,63,83,102,115,125,138`、`tests/test_worker.py`、`tests/test_app_id.py`

- [ ] **Step 1：写失败测试**（`tests/test_app_id.py`）：

```python
def test_worker_scopes_by_app_id(db, monkeypatch):
    from app import worker
    db.add(m.UserSetting(user_id="alice", memory_enabled=True, consent_version=__import__("app.schemas", fromlist=["x"]).CONSENT_VERSION))
    job = m.MemoryJob(user_id="alice", idempotency_key="k", session_id="s")
    db.add(job)
    db.flush()
    db.add(m.SessionMessage(user_id="alice", session_id="s", job_id=job.id, msg_id="1", role="user", content="我住在杭州"))
    db.add(m.SessionMessage(app_id="other", user_id="alice", session_id="s", job_id=job.id, msg_id="2", role="user", content="别的应用"))
    db.commit()
    assert [x.content for x in worker._messages(db, job)] == ["我住在杭州"]
```

- [ ] **Step 2：跑红** `pytest -q tests/test_app_id.py -k worker` → 返回两条（仍按 tenant_id，其他应用行的 tenant_id 被钩子写成 `other`，看似过滤……如果意外变绿，说明钩子已同步两列；保留测试，继续 Step 3 完成重命名以免依赖 tenant_id）。
- [ ] **Step 3：实现**：`app/recall.py:37` → `m.MemoryFact.app_id == u.app_id`；`app/worker.py:57` → `UserContext(app_id=job.app_id, user_id=job.user_id)`；第 63、102、125 行 `.tenant_id == job.tenant_id` → `.app_id == job.app_id`；第 83、115、138 行 `tenant_id=job.tenant_id` → `app_id=job.app_id`。
- [ ] **Step 4：跑绿** `pytest -q` → 全绿；`grep -n "tenant_id" app/` 只剩 `app/models.py`（双写列与旧约束）。
- [ ] **Step 5：提交** `git add app/recall.py app/worker.py tests/test_app_id.py && git commit -m "refactor(worker,recall): 读写维度改为 app_id"`

### Task 1.6：演示 UI 改为填写 `user_id`

**Files：** `web/src/auth.ts` → 删除，新建 `web/src/user.ts`；`web/src/api.ts`；`web/src/App.vue`；`web/src/__tests__/auth.spec.ts` → `user.spec.ts`；`web/src/__tests__/api.spec.ts`；`web/e2e/memory-panel.spec.ts:124,152`

**Interfaces：** `getUserId(): string | null`、`setUserId(id: string): void`、`clearUserId(): void`（localStorage key `memsvc_user_id`；支持 `?user_id=` 预填）。`api.ts` 所有请求：GET/DELETE 追加 `user_id` 查询参数，PUT/PATCH body 合并 `user_id`。

- [ ] **Step 1：写失败测试** `web/src/__tests__/user.spec.ts`：

```ts
import { beforeEach, describe, expect, it } from 'vitest'
import { clearUserId, getUserId, setUserId } from '../user'

describe('user id', () => {
  beforeEach(() => {
    localStorage.clear()
    window.history.replaceState({}, '', '/memory/ui/')
  })
  it('prefills from ?user_id=', () => {
    window.history.replaceState({}, '', '/memory/ui/?user_id=alice')
    expect(getUserId()).toBe('alice')
  })
  it('persists and clears', () => {
    setUserId('bob')
    expect(getUserId()).toBe('bob')
    clearUserId()
    expect(getUserId()).toBeNull()
  })
})
```

`api.spec.ts` 追加：

```ts
it('sends user_id as query on GET and in body on PUT, never Authorization', async () => {
  setUserId('alice')
  const calls: [string, RequestInit][] = []
  vi.stubGlobal('fetch', vi.fn(async (u: string, i: RequestInit) => { calls.push([u, i]); return new Response('{}') }))
  await getSettings()
  await updateSettings({ memory_enabled: false })
  expect(calls[0][0]).toContain('/v1/me/settings?user_id=alice')
  expect(new Headers(calls[0][1].headers).has('Authorization')).toBe(false)
  expect(JSON.parse(String(calls[1][1].body))).toMatchObject({ user_id: 'alice', memory_enabled: false })
})
```

- [ ] **Step 2：跑红** `cd web && npm test` → 找不到 `../user`。
- [ ] **Step 3：实现** `web/src/user.ts`：

```ts
const KEY = 'memsvc_user_id'

export function getUserId(): string | null {
  if (typeof window === 'undefined') return null
  const q = new URLSearchParams(window.location.search).get('user_id')
  if (q) localStorage.setItem(KEY, q)
  return q || localStorage.getItem(KEY)
}

export function setUserId(id: string): void {
  localStorage.setItem(KEY, id.trim())
}

export function clearUserId(): void {
  localStorage.removeItem(KEY)
}
```

`api.ts`：删 `resolveToken` 导入与第 17-22 行，`request()` 开头：

```ts
const userId = getUserId()
if (!userId) throw new ApiError(422, null, '请先填写 user_id')
const method = (init.method || 'GET').toUpperCase()
let url = `${API_BASE}${path}`
let body = init.body
if (method === 'GET' || method === 'DELETE') {
  url += `${path.includes('?') ? '&' : '?'}user_id=${encodeURIComponent(userId)}`
} else {
  body = JSON.stringify({ ...(body ? JSON.parse(String(body)) : {}), user_id: userId })
}
```

`exportMemories()` 同理改为 `?user_id=`。`App.vue`：`token` ref → `userId` ref；顶部加 `<input data-testid="user-id" v-model="draft" placeholder="user_id">` + 「使用」按钮（`setUserId(draft); userId = draft; loadAll()`）；「退出」按钮改「切换用户」调用 `clearUserId()`；第 163、169 行提示改为「请填写 user_id（演示用；正式由天code 业务调用）」。删除 `web/src/auth.ts` 与 `auth.spec.ts`。e2e 第 124、152 行 `page.goto('?token=test.jwt')` → `page.goto('?user_id=alice')`，mock 路由断言 URL 带 `user_id=alice`。
- [ ] **Step 4：跑绿** `cd web && npm test && npm run build`；有浏览器时 `npm run test:e2e`。
- [ ] **Step 5：提交**
```bash
git rm web/src/auth.ts web/src/__tests__/auth.spec.ts
git add web/src/user.ts web/src/api.ts web/src/App.vue web/src/__tests__/user.spec.ts web/src/__tests__/api.spec.ts web/e2e/memory-panel.spec.ts
git commit -m "feat(web): 演示页填写 user_id，去掉 token 登录"
```

### Task 1.7：文档与教学课（PR1 部分）

**Files：** `docs/api.md`、`docs/architecture.md`、`docs/eval.md`、`docs/deploy.md`、`docs/lessons/me-ui.md`、`README.md`、`web/README.md`、`docs/lessons/app-id-pure-memory.md`（新建）、`tests/test_docs_flag.py` 不动

- [ ] **Step 1：写文档守卫测试** `tests/test_docs_wording.py`：

```python
import pathlib

DOCS = ["docs/api.md", "docs/architecture.md", "docs/lessons/me-ui.md", "README.md", "web/README.md"]


def test_public_docs_have_no_jwt_login():
    for p in DOCS:
        t = pathlib.Path(p).read_text(encoding="utf-8")
        assert "JWT" not in t and "?token=" not in t, p
```

- [ ] **Step 2：跑红** `pytest -q tests/test_docs_wording.py`。
- [ ] **Step 3：改文档**：
  - `docs/api.md:4,6,12`：认证段改为「业务侧传 `user_id`（body 或 `?user_id=`），缺失 422；`app_id` 由服务配置 `APP_ID` 固定」；删 401 行；第 90 行改为「`app.user_id` 由服务按请求里的 `user_id` 设置；谁是当前用户由业务调用方保证」。每个接口示例补 `user_id`。
  - `docs/architecture.md:12,29-30,46-47,50,55-62,86-87,109,139,152`：`tenant_id` → `app_id`，「鉴权: JWT→TenantContext」→「身份: user_id + APP_ID → UserContext」，「租户」→「应用」，删「透传用户 JWT」。
  - `docs/eval.md:25,33`：改为 `test_missing_user_id_is_422`、`test_rejects_bad_app_id`。
  - `docs/deploy.md:51,55`：删 `JWT_SECRET` 行，加 `APP_ID`（默认 `tiancode`）。
  - `README.md:7,48`、`web/README.md:9,15`、`docs/lessons/me-ui.md:3`：改为 `?user_id=` / 输入框。
  - 新建 `docs/lessons/app-id-pure-memory.md`（中文）：①为什么记忆服务不验 JWT（身份是业务的事，记忆服务只做能力；责任边界）；②「应用」vs 原租户（固定 `APP_ID`，不做切换）；③双写迁移为什么分两次发布（回滚安全、`server_default` 让旧镜像能写）；④练习题：在练习机 `psql` 里 `SELECT count(*) FROM memory_fact WHERE app_id IS DISTINCT FROM tenant_id;` 应为 0。
- [ ] **Step 4：跑绿** `pytest -q` 全绿。
- [ ] **Step 5：提交** `git add docs/api.md docs/architecture.md docs/eval.md docs/deploy.md docs/lessons/me-ui.md docs/lessons/app-id-pure-memory.md README.md web/README.md tests/test_docs_wording.py && git commit -m "docs: 纯记忆服务身份约定与 app_id 教学课"`

### Task 1.8：PR1 发布

- [ ] `pytest -q` 绿；有 PG 时 `TEST_DATABASE_URL=… pytest -q` 绿；`cd web && npm test && npm run build` 绿。
- [ ] `git push -u origin feat/app-id-dual-write`，开 PR「PR1：纯记忆服务 + app_id 双写」，描述里写明：部署前从 Jenkins 凭据 `deploy-env` 删 `JWT_SECRET`、加 `APP_ID=tiancode`（pydantic-settings 忽略未知环境变量，留着不会启动失败，但应清理）。
- [ ] New Bot 审 → 作者合并 → Jenkins 构建 → 网关 `curl -fsS https://<网关>/memory/readyz` 200 → 冒烟：`PUT /v1/me/settings {user_id, memory_enabled:true, consent_version}` → `POST /v1/memories` → `POST /v1/memories/recall`。
- [ ] 练习机核对回填：`SELECT count(*) FROM memory_fact WHERE app_id <> tenant_id;` = 0（每张表）。向 maxzq 汇报构建号。

---

# Part 2 — PR2：删 `tenant_id` + 清扫用语

前置：PR1 已在练习机稳定运行（至少一次完整冒烟），且确认没有旧镜像需要回滚。分支 `feat/drop-tenant-id`。

### Task 2.1：处理 0001 对 `tenant_id` 的硬编码（需批准）

**问题：** `0001_init.py:25` 在 Postgres 上执行 `CREATE INDEX … ix_fact_active ON memory_fact (tenant_id, user_id)`，而 0001 的表来自**当前模型** `create_all`。PR2 模型删掉 `tenant_id` 后，**全新库**（含 `tests/conftest.py` 的 PG 路径、新环境部署）跑 0001 会因列不存在直接失败。已部署库不会重跑 0001。

**方案（推荐，需 maxzq 批准改旧文件）：** 只改这一行为存在性保护，不改已部署库的行为：

```python
op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='memory_fact' "
           "AND column_name='tenant_id') THEN CREATE INDEX IF NOT EXISTS ix_fact_active ON memory_fact "
           "(tenant_id, user_id) WHERE valid_to IS NULL; END IF; END $$")
```

**备选（不改旧文件）：** 0001 里冻结一份不含 `app_id` 的 V1 表定义——同样要改 0001，不可行；或模型永久保留一个不使用的 `tenant_id` 列——违背 spec §11。故只能二选一：批准上面这处改动，或接受「全新库只能从 PR1 之前的镜像建库」。未获批准前**不要开始 Task 2.2**。

- [ ] **Step 1：失败测试**（`tests/test_postgres.py`）：`test_fresh_db_migrates_to_head` —— `fresh_db` 夹具本身就会在 PG 上从零 `upgrade head`，模型删列后整个 PG 测试套件即红；先在本任务只改 0001，跑 `TEST_DATABASE_URL=… pytest -q tests/test_postgres.py` 确认仍绿（此时 tenant_id 还在，保护分支走 IF 真）。
- [ ] **Step 2：提交** `git add migrations/versions/0001_init.py && git commit -m "fix(migrations): 0001 的 ix_fact_active 仅在 tenant_id 存在时创建"`

### Task 2.2：Alembic `0006_drop_tenant_id`

**Files：** `migrations/versions/0006_drop_tenant_id.py`、`tests/test_postgres.py`

**Interfaces：** `revision = "0006_drop_tenant_id"`，`down_revision = "0005_app_id"`。

- [ ] **Step 1：写失败测试**：

```python
def test_0006_drops_tenant_id_everywhere(db):
    c = db.connection()
    left = c.execute(text("SELECT table_name FROM information_schema.columns WHERE column_name='tenant_id' "
                          "AND table_schema='public'")).all()
    assert left == []
    idx = {r[0] for r in c.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname='public'"))}
    assert not {"ix_fact_active", "ix_fact_user_subject", "uq_job_idem", "uq_user_setting"} & idx
    assert {"ix_fact_active_app", "ix_fact_app_user_subject", "uq_job_idem_app", "uq_user_setting_app"} <= idx
```

并把 `test_migration_artifacts` 里的 `"ix_fact_active"` 去掉（只留 `_app`）。SQLite 版（`tests/test_app_id.py`）：

```python
def test_models_have_no_tenant_id():
    for t in m.Base.metadata.tables.values():
        assert "tenant_id" not in t.c, t.name
```

（`from app.db import Base` 若 `m.Base` 不可用。）

- [ ] **Step 2：跑红** `pytest -q tests/test_app_id.py -k tenant` 与 PG 版。
- [ ] **Step 3：实现迁移**：

```python
"""删除 tenant_id（双写过渡第二步）：旧索引/约束/列全部移除，只留 app_id。

downgrade 会把列加回并从 app_id 回填，便于回滚到 PR1 镜像。
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_drop_tenant_id"
down_revision = "0005_app_id"

TABLES = ("session_message", "memory_fact", "memory_preference", "session_summary", "memory_job",
          "audit_log", "user_setting")
OLD_UNIQUES = {"memory_job": ("uq_job_idem", ["tenant_id", "user_id", "idempotency_key"]),
               "user_setting": ("uq_user_setting", ["tenant_id", "user_id"])}


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_fact_active")
    if "ix_fact_user_subject" in {i["name"] for i in insp.get_indexes("memory_fact")}:
        op.drop_index("ix_fact_user_subject", table_name="memory_fact")
    for t, (name, _) in OLD_UNIQUES.items():
        if name in {u["name"] for u in insp.get_unique_constraints(t)}:
            with op.batch_alter_table(t) as b:
                b.drop_constraint(name, type_="unique")
    for t in TABLES:
        if "tenant_id" in {c["name"] for c in insp.get_columns(t)}:
            with op.batch_alter_table(t) as b:
                b.drop_column("tenant_id")


def downgrade():
    for t in TABLES:
        op.add_column(t, sa.Column("tenant_id", sa.String(64), nullable=True))
        op.execute(f"UPDATE {t} SET tenant_id = app_id")
    op.create_index("ix_fact_user_subject", "memory_fact", ["tenant_id", "user_id", "subject"])
    for t, (name, cols) in OLD_UNIQUES.items():
        with op.batch_alter_table(t) as b:
            b.create_unique_constraint(name, cols)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE INDEX IF NOT EXISTS ix_fact_active ON memory_fact (tenant_id, user_id) WHERE valid_to IS NULL")
```

- [ ] **Step 4：提交**（与 Task 2.3 一起跑绿，因为模型仍带 tenant_id 时 SQLite 测试先红）`git add migrations/versions/0006_drop_tenant_id.py tests/test_postgres.py tests/test_app_id.py && git commit -m "feat(migrations): 0006 删除 tenant_id"`

### Task 2.3：模型去 `tenant_id` 与双写钩子

**Files：** `app/models.py`、`tests/test_app_id.py`

- [ ] **Step 1：实现**：删 `UserScoped.tenant_id`、`AuditLog.tenant_id`、`Index("ix_fact_user_subject", …)`、`UniqueConstraint(… name="uq_job_idem")`、`UniqueConstraint(… name="uq_user_setting")`、`_DUAL` 与 `_dual_write_tenant_id`；`event`/`Session` 导入若无他用一并删。
- [ ] **Step 2：改测试**：`tests/test_app_id.py` 里 `test_new_rows_dual_write_app_id_and_tenant_id` 改为只断言 `app_id == "tiancode"` 并改名 `test_new_rows_default_app_id`；`test_parallel_constraints_exist` 改为只断言 `_app` 版本存在、旧名不存在；`test_rows_written_via_api_carry_config_app_id` 去掉 `job.tenant_id`。
- [ ] **Step 3：跑绿** `pytest -q` 全绿；`TEST_DATABASE_URL=… pytest -q` 全绿（验证 Task 2.1 的全新库路径）；`DATABASE_URL=sqlite:////tmp/mig2.db alembic upgrade head && … alembic downgrade 0005_app_id && … alembic upgrade head` 无报错。
- [ ] **Step 4：提交** `git add app/models.py tests/test_app_id.py && git commit -m "refactor(models): 移除 tenant_id 与双写"`

### Task 2.4：清扫「租户 / tenant」用语

**Files：** `app/schemas.py`、`docs/*.md`、`docs/adr/0001-build-vs-buy.md`、`README.md`、`tests/test_docs_wording.py`、`docs/lessons/app-id-pure-memory.md`

- [ ] **Step 1：加强守卫测试**（`tests/test_docs_wording.py` 追加）：

```python
import subprocess

ALLOWED = ("migrations/versions/0001_init.py", "migrations/versions/0003_user_setting.py",
           "migrations/versions/0005_app_id.py", "migrations/versions/0006_drop_tenant_id.py",
           "docs/superpowers/", "tests/test_docs_wording.py", "tests/test_postgres.py", "tests/test_app_id.py")


def test_no_tenant_wording_outside_history():
    out = subprocess.run(["git", "grep", "-il", "-e", "tenant", "-e", "租户"], capture_output=True, text=True).stdout
    bad = [p for p in out.split() if not p.startswith(ALLOWED)]
    assert bad == [], bad
```

- [ ] **Step 2：跑红** `pytest -q tests/test_docs_wording.py` → 列出残留文件。
- [ ] **Step 3：逐个改**为「应用」/`app_id`（含 `docs/adr/0001-build-vs-buy.md`、`docs/architecture.md` 残留、`README.md:7`）；教学课追加「PR2：删列与回滚」一节（`downgrade` 会从 `app_id` 回填 `tenant_id`）。
- [ ] **Step 4：跑绿** `pytest -q`。
- [ ] **Step 5：提交** `git add app/schemas.py docs README.md tests/test_docs_wording.py && git commit -m "docs: 清扫租户用语"`（`docs` 目录下只有已跟踪文档；先 `git status --short docs` 确认无意外文件）。

### Task 2.5：PR2 发布

- [ ] 全量测试绿（SQLite + PG + web）。
- [ ] 推分支、开 PR「PR2：删除 tenant_id」，描述写明：**合并后旧镜像不能再回滚**，除非先 `alembic downgrade 0005_app_id`。
- [ ] New Bot 审 → 合并 → Jenkins → `/readyz` 200 → 冒烟同 1.8 → 练习机 `\d memory_fact` 无 `tenant_id` → 汇报构建号。

---

## 自审（对照 spec）

| spec 条目 | 计划位置 |
| --- | --- |
| §1/§5 无 JWT、无登录/刷新/登出 | Task 1.1、1.4（服务端本来就没有 login/refresh/logout 路由，只需删 JWT 解析与配置）；1.6 删前端 token 登录 |
| §2 业务传 `user_id`、缺省 422 | Task 1.4（body + 无 body 接口的 `?user_id=`）|
| §2 `app_id` 固定来自配置 | Task 1.1、1.4 `user_ctx` |
| §4.2 新 revision、回填、NOT NULL 默认 tiancode、并行索引 | Task 1.3 |
| §4.2 双写、读用 `app_id` | Task 1.2 钩子、1.4/1.5 过滤 |
| §4.3 删列、删旧约束、清文案 | Task 2.2–2.4 |
| §6 consent 唯一键 `(app_id,user_id)`、job 幂等维度 | 1.2 约束、1.3 迁移、1.4 测试 |
| §6 不引入 token 过期错误码 | 1.4（401 全部消失）|
| §7 可选 `X-Service-Key` | **未纳入**：spec 标为「生产前可选」，本计划不实现，留作后续 |
| §8 测试门禁、UI、文档、教学课 | 1.6、1.7、1.8、2.4、2.5 |
| §3 FORCE RLS 保持 | 0005/0006 不动策略；`bind_user` 保证 `app.user_id` 仍在首条 SQL 前设置 |

**spec 未覆盖、计划中补上的点：** `session_summary` 也带 `tenant_id`（spec §4.1 未列，已纳入）；`ix_fact_active` 是 0001 里的原生 SQL 且仅 Postgres；GET/DELETE 无 body，需用 `?user_id=`；`Strict(extra="forbid")` 原本专门拒绝 body 里的 `user_id`，相应测试反转；0001 `create_all` 依赖当前模型导致 PR2 全新库失败（Task 2.1，需批准）。
