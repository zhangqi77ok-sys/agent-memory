"""第 40 轮：媒体备忘来源 sidecar。

新建 memory_media_ref 表：一条媒体备忘的引用元数据（本体是 TaskEpisode，
source=screenshot/voice-memo，steps 即文本层）。只存元数据，不存原文件
字节；external_uri 只是调用方提供的指针。无回填：旧 episode 没有
sidecar，不补。
"""

import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401

revision = "0031_media_ref"
down_revision = "0030_reminder_ttl_ledger"


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "memory_media_ref" not in tables:
        op.create_table(
            "memory_media_ref",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("app_id", sa.String(64), nullable=False, server_default="tiancode"),
            sa.Column("user_id", sa.String(64), nullable=False, index=True),
            sa.Column("episode_id", sa.String(32), nullable=False, index=True),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("origin", sa.String(16), nullable=False),
            sa.Column("filename", sa.String(256), nullable=True),
            sa.Column("mime", sa.String(64), nullable=True),
            sa.Column("byte_size", sa.Integer(), nullable=True),
            sa.Column("sha256", sa.String(64), nullable=True),
            sa.Column("external_uri", sa.String(512), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "memory_media_ref" in tables:
        op.drop_table("memory_media_ref")
