"""v1 数据模型，对应 docs/architecture.md 第 4 节。"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.config import settings
from app.db import Base


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Embedding(TypeDecorator):
    """Postgres 上用 pgvector；SQLite（单测）退化为 JSON。"""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector(settings.embedding_dim))
        return dialect.type_descriptor(JSON())


class UserScoped:
    app_id: Mapped[str] = mapped_column(String(64), default=lambda: settings.app_id, server_default="tiancode")
    user_id: Mapped[str] = mapped_column(String(64), index=True)


class SessionMessage(UserScoped, Base):
    __tablename__ = "session_message"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    msg_id: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MemoryFact(UserScoped, Base):
    __tablename__ = "memory_fact"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    subject: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    embedding = mapped_column(Embedding, nullable=True)
    embed_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (
        Index("ix_fact_app_user_subject", "app_id", "user_id", "subject"),
    )


class MemoryPreference(UserScoped, Base):
    __tablename__ = "memory_preference"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[str] = mapped_column(Text)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class SessionSummary(UserScoped, Base):
    __tablename__ = "session_summary"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(Text)
    upto_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TaskEpisode(UserScoped, Base):
    """一次任务的完整过程：目标、步骤轨迹、结果、成本。

    事实抽取看不见这些，而经验提炼（distill）正是以它为原料：
    系统考的不是记住事实，是能不能把过程变成让后面任务更好/更省的经验。
    """
    __tablename__ = "task_episode"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    source: Mapped[str] = mapped_column(String(32), default="mcp")  # 提交方（codex / gemini-cli / api…）
    title: Mapped[str] = mapped_column(Text)  # 任务目标
    outcome: Mapped[str] = mapped_column(String(16), default="success")  # success|failure|partial
    steps: Mapped[list] = mapped_column(JSON, default=list)  # [{action, observation?, error?}]
    cost: Mapped[dict] = mapped_column(JSON, default=dict)  # {tokens, duration_s, retries...}
    project: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)  # 来源项目/仓库（第十轮）：防跨项目串扰；为空=通用
    distill_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/running/done/failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 领取锁定期；过期可被其他 Worker 重领
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 客户端重试防重；为空表示不防重
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("app_id", "user_id", "idempotency_key", name="uq_episode_idem_app"),
    )


class MemoryInsight(UserScoped, Base):
    """从任务过程提炼出的经验条目：不是「用户是什么」，而是「这类任务怎么做更好/更省」。

    与事实同构：时间失效 + 版本递增 + same_subject 合并 + 向量语义召回，
    靠来源 episode 溯源，靠 decide() 裁决去重与更新。
    """
    __tablename__ = "memory_insight"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    kind: Mapped[str] = mapped_column(String(16))  # lesson|strategy|pitfall|cost_saver
    subject: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)  # 可直接照着做的指导
    applies_when: Mapped[str] = mapped_column(Text, default="")  # 适用情境，情境召回的主要匹配面
    embedding = mapped_column(Embedding, nullable=True)
    embed_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_episode_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    project: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)  # 继承来源轨迹的项目（第十轮）；为空=通用经验
    # 反馈闭环（第十轮）：被简报递出记 use_count，任务结果经 memory_feedback 回流成置信度。
    # confidence 由反馈按拉普拉斯平滑算出 (1+pos)/(2+pos+neg)，无反馈时 0.5（中性先验）；
    # 它是第十一轮衰减与简报三要素排序的输入，不在这里拍脑袋赋值。
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    feedback_pos: Mapped[int] = mapped_column(Integer, default=0)
    feedback_neg: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 用户策展（第十一轮）：置顶=简报必达（仍受条数与 token 预算约束），禁用=永不召回。
    # 记忆越自动越要给人留开关——这两列就是经验库对用户的「可见可编辑」承诺的落点。
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        Index("ix_insight_app_user_subject", "app_id", "user_id", "subject"),
    )


class MemoryJob(UserScoped, Base):
    __tablename__ = "memory_job"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    session_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/running/done/dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("app_id", "user_id", "idempotency_key", name="uq_job_idem_app"),
    )


class ForgottenId(Base):
    """只存被遗忘的 id，不存原文；从备份恢复后先按此表重删。"""

    __tablename__ = "forgotten_id"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    forgotten_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    app_id: Mapped[str] = mapped_column(String(64), default=lambda: settings.app_id, server_default="tiancode")
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(16))
    fact_ids: Mapped[list] = mapped_column(JSON, default=list)
    decision: Mapped[str] = mapped_column(String(16), default="allow")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UserSetting(UserScoped, Base):
    """每个用户一行：是否同意并开启记忆。没有这一行 = 未同意 = 不写入、不召回（个保法：先同意后处理）。"""
    __tablename__ = "user_setting"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (
        UniqueConstraint("app_id", "user_id", name="uq_user_setting_app"),
    )
