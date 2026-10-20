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
    job_id: str
    duplicate: bool


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
