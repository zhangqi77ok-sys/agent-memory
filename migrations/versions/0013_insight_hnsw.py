"""第十四轮：经验侧补向量索引 ix_insight_hnsw（事实侧 0001 已有 ix_fact_hnsw）。

背景：索引化召回的向量通道靠余弦距离在 SQL 侧压候选——事实表有 HNSW 索引可用，
memory_insight 一直没有，简报在 Postgres 上只能退全量打分。索引建在 embedding 列上
（vector_cosine_ops 与召回的余弦门槛同口径）；SQLite 无 pgvector，本迁移只在 PG 执行。
"""
from alembic import op

revision = "0013_insight_hnsw"
down_revision = "0012_experience_stats_fn"


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE INDEX IF NOT EXISTS ix_insight_hnsw ON memory_insight "
               "USING hnsw (embedding vector_cosine_ops)")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_insight_hnsw")
