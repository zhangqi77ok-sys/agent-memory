"""第十八轮：偏好加向量列——此前只有事实与经验有 embedding，偏好没有，
语义去重（跨措辞）在偏好侧无向量可比，只能退回词面。补齐后偏好写入时
同步嵌向量（见 worker._embed_preference），存量行由 backfill 补。

与 0017 同手法：inspect 先查再补，重复跑不报错；RLS 策略与表授权已覆盖
整表，加列无需另授。"""
import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401
from app.models import Embedding

revision = "0018_preference_embedding"
down_revision = "0017_prospective_salience"


def upgrade():
    insp = sa.inspect(op.get_bind())
    have = {c["name"] for c in insp.get_columns("memory_preference")}
    with op.batch_alter_table("memory_preference") as batch:
        if "embedding" not in have:
            batch.add_column(sa.Column("embedding", Embedding(), nullable=True))
        if "embed_model" not in have:
            batch.add_column(sa.Column("embed_model", sa.String(64), nullable=True))


def downgrade():
    insp = sa.inspect(op.get_bind())
    have = {c["name"] for c in insp.get_columns("memory_preference")}
    with op.batch_alter_table("memory_preference") as batch:
        if "embedding" in have:
            batch.drop_column("embedding")
        if "embed_model" in have:
            batch.drop_column("embed_model")
