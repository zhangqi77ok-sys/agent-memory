"""第二十轮：简报扩散交付边 + 记忆边用户复合索引。

- brief_delivery_edge：简报经哪条边带出了哪条经验。回流/反馈结账时
  顺着它找到边、改边权——边从此从使用中学习，不再创建时 frozen。
  applied 表示这条交付后来被证实/否决过（边级只结一次账）；边若已
  关闭（版本滚动迁边），只记 applied，不改已关闭边的强度。
- ix_link_app_user：memory_link 此前只有 (src)/(dst) 索引与 user_id 单
  索引；按用户扫边（网结构统计、扩散记边）在千万级下要走复合索引。

与 0018/0019 同手法：inspect 先查再补，重复跑不报错；RLS 与授权只在
PG 上办（admin_audit 同款）。
"""
import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401
from app.models import _id

revision = "0020_delivery_edge"
down_revision = "0019_agent_evidence_audit"


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "brief_delivery_edge" not in insp.get_table_names():
        op.create_table(
            "brief_delivery_edge",
            sa.Column("id", sa.String(32), primary_key=True, default=_id),
            sa.Column("app_id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("delivery_id", sa.String(32), nullable=False),
            sa.Column("link_id", sa.String(32), nullable=False),
            sa.Column("insight_id", sa.String(32), nullable=False),
            sa.Column("applied", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_delivery_edge_delivery", "brief_delivery_edge",
                        ["delivery_id"])
        op.create_index("ix_delivery_edge_link", "brief_delivery_edge",
                        ["link_id"])
        if op.get_bind().dialect.name == "postgresql":
            op.execute("ALTER TABLE brief_delivery_edge ENABLE ROW LEVEL SECURITY")
            # 0016 brief_delivery 同款：只开 RLS 不建 POLICY 时应用账号
            # 任何写入都会被拦（RLS 无策略 = 全拒），简报经 API 记交付边
            # 时报 InsufficientPrivilege——本轮 PG 全套即因此挂 1 例。
            # 注意：0020 尚未部署到用户生产库（用户侧停在 0019），故直接
            # 在本迁移内补策略；若已有库跑过缺策略的 0020，需另起修复迁移。
            op.execute(
                "CREATE POLICY brief_delivery_edge_user_isolation "
                "ON brief_delivery_edge USING ("
                "current_setting('app.user_id', true) IS NULL OR "
                "user_id = current_setting('app.user_id', true))")
            op.execute(
                "DO $$ BEGIN "
                "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_app') "
                "THEN GRANT SELECT, INSERT, UPDATE, DELETE ON brief_delivery_edge "
                "TO memsvc_app; END IF; "
                "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memsvc_worker') "
                "THEN GRANT SELECT, INSERT, UPDATE, DELETE ON brief_delivery_edge "
                "TO memsvc_worker; END IF; "
                "END $$")
    link_indexes = {ix["name"] for ix in insp.get_indexes("memory_link")}
    if "ix_link_app_user" not in link_indexes:
        op.create_index("ix_link_app_user", "memory_link", ["app_id", "user_id"])
    # decayed_until：闲置衰减的结清锚点。没有它，巡检每次都会把同一段
    # 闲置重复算（0.95^月数按当前强度再乘一次）；有了它，下次只算新增
    # 的闲置月数，月度巡检即使被每小时的 weave 巡检顺带调用也不会多扣。
    link_cols = {c["name"] for c in insp.get_columns("memory_link")}
    if "decayed_until" not in link_cols:
        with op.batch_alter_table("memory_link") as batch:
            batch.add_column(sa.Column("decayed_until",
                                       sa.DateTime(timezone=True), nullable=True))


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "brief_delivery_edge" in insp.get_table_names():
        op.drop_table("brief_delivery_edge")
    link_indexes = {ix["name"] for ix in insp.get_indexes("memory_link")}
    if "ix_link_app_user" in link_indexes:
        op.drop_index("ix_link_app_user", table_name="memory_link")
    link_cols = {c["name"] for c in insp.get_columns("memory_link")}
    if "decayed_until" in link_cols:
        with op.batch_alter_table("memory_link") as batch:
            batch.drop_column("decayed_until")
