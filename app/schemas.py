from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    # 禁止多余字段：body 里出现 app_id 等未定义字段直接 422
    model_config = ConfigDict(extra="forbid")


class UserScopedRequest(Strict):
    user_id: str = Field(min_length=1, max_length=64)


class Message(Strict):
    msg_id: str
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=8000)


class WriteRequest(UserScopedRequest):
    session_id: str
    messages: list[Message] = Field(min_length=1, max_length=50)


class WriteAccepted(BaseModel):
    job_id: str | None
    duplicate: bool
    skipped: bool = False  # 用户未同意或已暂停记忆：本次没有写入


class RecallRequest(UserScopedRequest):
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


class CorrectRequest(UserScopedRequest):
    content: str = Field(min_length=1, max_length=2000)


class SettingsOut(BaseModel):
    memory_enabled: bool
    consent_version: str | None
    consent_at: datetime | None
    current_consent_version: str = CONSENT_VERSION


class SettingsIn(UserScopedRequest):
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


class EpisodeStep(Strict):
    action: str = Field(min_length=1, max_length=500)
    observation: str | None = Field(default=None, max_length=2000)
    error: str | None = Field(default=None, max_length=2000)


class EpisodeSubmitRequest(UserScopedRequest):
    source: str = Field(default="api", max_length=32)
    title: str = Field(min_length=1, max_length=500)
    outcome: str = Field(default="success", pattern="^(success|failure|partial)$")
    steps: list[EpisodeStep] = Field(default_factory=list, max_length=200)
    cost: dict = Field(default_factory=dict)


class EpisodeAccepted(BaseModel):
    episode_id: str | None
    skipped: bool = False  # 用户未同意或已暂停记忆：不落任何数据
    duplicate: bool = False  # 同一 Idempotency-Key 已提交过：返回的是已有 episode，没有重复落库


class BriefRequest(UserScopedRequest):
    task: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=5, ge=1, le=20)


class InsightOut(BaseModel):
    insight_id: str
    kind: str  # lesson|strategy|pitfall|cost_saver
    subject: str
    content: str
    applies_when: str
    score: float
    reason: str


class BriefResponse(BaseModel):
    insights: list[InsightOut]
