"""v1 数据模型，对应 docs/architecture.md 第 4 节。"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
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
