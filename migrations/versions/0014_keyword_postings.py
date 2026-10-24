"""第十四轮补完：关键词倒排索引 memory_fact_token / memory_insight_token。

背景：索引化召回第一版把关键词门槛做成 SQL 的 LIKE 计数门，真机剖析地板
~60ms/5000 行（逐行 × 逐 token 子串扫描）。补完改为倒排：每行的 token 集合
落表，召回探针按 (token, id) 索引数命中数。token 口径与 app.tokenize 一致，
存量行在本迁移里回填；此后由 models 里的 ORM 事件同事务维护。

与 0008 同一手法：只建本迁移自己的表；PG 下开 RLS（策略与父表同形，倒排表
冗余存了 app_id/user_id 就是为了这一点）并给 worker/app 角色授权。
"""
from alembic import op
from sqlalchemy import select

import app.models  # noqa: F401
from app.db import Base
from app.tokenize import MAX_TOKEN_LEN, tokens_of

revision = "0014_keyword_postings"
down_revision = "0013_insight_hnsw"

TABLES = ("memory_fact_token", "memory_insight_token")


def _backfill(bind, parent_name: str, token_name: str, id_key: str, text_of) -> None:
    parent = Base.metadata.tables[parent_name]
    tokens_t = Base.metadata.tables[token_name]
    rows = bind.execute(select(parent.c.id, parent.c.app_id, parent.c.user_id,
                               parent.c.subject, parent.c.content,
                               *( [parent.c.applies_when] if parent_name == "memory_insight" else []))).fetchall()
    payload = []
    for r in rows:
        text = text_of(r)
        payload += [{id_key: r.id, "token": t, "app_id": r.app_id, "user_id": r.user_id}
                    for t in tokens_of(text) if len(t) <= MAX_TOKEN_LEN]
    if payload:
        bind.execute(tokens_t.insert(), payload)


def upgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    Base.metadata.create_all(bind, tables=[Base.metadata.tables[t] for t in TABLES])
    # 置顶部分索引：与 models 里 MemoryInsight.__table_args__ 同名同义。用
    # IF NOT EXISTS 是因为全新安装时 0008 按「当前 models」建 memory_insight
    # 会顺带把这个索引建出来（create_all 带 __table_args__ 的索引），到 0014
    # 时已存在；存量库则靠这一句补上。两个方言都支持 IF NOT EXISTS。
    if is_pg:
        op.execute("CREATE INDEX IF NOT EXISTS ix_insight_pinned_scope "
                   "ON memory_insight (app_id, user_id) WHERE pinned")
    else:
        op.execute("CREATE INDEX IF NOT EXISTS ix_insight_pinned_scope "
                   "ON memory_insight (app_id, user_id) WHERE pinned = 1")
    if is_pg:
        for t in TABLES:
            op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY {t}_user_isolation ON {t} USING ("
                f"current_setting('app.user_id', true) IS NULL OR user_id = current_setting('app.user_id', true))"
            )
            for role in ("memsvc_app", "memsvc_worker"):
                op.execute(f"""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO {role}; END IF; END $$;""")
    # 回填的拼法必须与 models 里事件维护、召回打分用的 f-string 逐字一致
    _backfill(bind, "memory_fact", "memory_fact_token", "fact_id",
              lambda r: f"{r.subject} {r.content}")
    _backfill(bind, "memory_insight", "memory_insight_token", "insight_id",
              lambda r: f"{r.subject} {r.content} {r.applies_when}")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_insight_pinned_scope")
    Base.metadata.drop_all(op.get_bind(), tables=[Base.metadata.tables[t] for t in TABLES])
