from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    # 禁止多余字段：body 里出现 tenant_id / user_id 直接 422
    model_config = ConfigDict(extra="forbid")


class Message(Strict):
    msg_id: str
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=8000)


class WriteRequest(Strict):
    session_id: str
    messages: list[Message] = Field(min_length=1, max_length=50)


class WriteAccepted(BaseModel):
    job_id: str | None
    duplicate: bool
    skipped: bool = False  # 用户未同意或已暂停记忆：本次没有写入


class RecallRequest(Strict):
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=8, ge=1, le=50)
    token_budget: int = Field(default=1500, ge=100, le=8000)


class FactOut(BaseModel):
    fact_id: str
    subject: str
    content: str
    score: float
    reason: str


class RecallResponse(BaseModel):
    preferences: dict[str, str]
    facts: list[FactOut]


CONSENT_VERSION = "2026-10-v1"


class MemoryItem(BaseModel):
    id: str
    kind: str  # fact | preference
    subject: str  # 事实的主题 / 偏好的 key
    content: str
    version: int
    valid_from: datetime
    source_session_id: str | None = None


class MemoryPage(BaseModel):
    items: list[MemoryItem]
    total: int
    offset: int
    limit: int


class CorrectRequest(Strict):
    content: str = Field(min_length=1, max_length=2000)


class SettingsOut(BaseModel):
    memory_enabled: bool
    consent_version: str | None
    consent_at: datetime | None
    current_consent_version: str = CONSENT_VERSION


class SettingsIn(Strict):
    memory_enabled: bool
    # 首次开启必须带上同意的隐私说明版本；版本不对返回 409，前端重新展示授权页
    consent_version: str | None = None


class JobOut(BaseModel):
    job_id: str
    session_id: str
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
