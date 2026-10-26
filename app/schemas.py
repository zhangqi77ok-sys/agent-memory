from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    # 第十五轮：沿关联边扩散带出时，标明是被哪条事实连带想起的（直接命中为空）
    linked_from: str | None = None
    # 第十七轮：佐证置信度（被独立抽到的次数折算），排序已按它加权
    confidence: float = 0.5


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
    # 第十七轮：佐证置信度与佐证次数（被独立来源重复证实的程度）
    confidence: float = 0.5
    support_count: int = 1
    # 第二十三轮：人工好评/差评计数（事实级反馈后才非零）
    feedback_pos: int = 0
    feedback_neg: int = 0


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
    project: str | None = Field(default=None, max_length=128)  # 来源项目/仓库（第十轮作用域）
    title: str = Field(min_length=1, max_length=500)
    outcome: str = Field(default="success", pattern="^(success|failure|partial)$")
    steps: list[EpisodeStep] = Field(default_factory=list, max_length=200)
    cost: dict = Field(default_factory=dict)

    @field_validator("cost")
    @classmethod
    def _cost_bounded(cls, v: dict) -> dict:
        # cost 是轨迹侧最松的入口（第八轮补）：键数与序列化体积设上限，防把任意大 blob 塞进库
        import json as _json
        if len(v) > 20 or len(_json.dumps(v, ensure_ascii=False)) > 4096:
            raise ValueError("cost 过大：最多 20 个键、序列化后不超过 4096 字符")
        return v


class EpisodeAccepted(BaseModel):
    episode_id: str | None
    skipped: bool = False  # 用户未同意或已暂停记忆：不落任何数据
    duplicate: bool = False  # 同一 Idempotency-Key 已提交过：返回的是已有 episode，没有重复落库


class BriefRequest(UserScopedRequest):
    task: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=5, ge=1, le=20)
    token_budget: int = Field(default=1500, ge=100, le=8000)  # 简报正文的 token 上限，与事实召回对齐
    project: str | None = Field(default=None, max_length=128)  # 当前项目（第十轮）：同项目经验加权
    # 会话标识（第 34 轮）：与事实行 source_session_id 同一命名空间；不传则缓冲不启用
    session_id: str | None = Field(default=None, max_length=64)


class FeedbackRequest(UserScopedRequest):
    insight_id: str = Field(min_length=1, max_length=32)
    helpful: bool
    task_outcome: str | None = Field(default=None, pattern="^(success|failure|partial)$")


class FeedbackOut(BaseModel):
    insight_id: str
    confidence: float
    feedback_pos: int
    feedback_neg: int
    success_count: int


class FactFeedbackRequest(UserScopedRequest):
    fact_id: str = Field(min_length=1, max_length=32)
    helpful: bool


class FactFeedbackOut(BaseModel):
    fact_id: str
    confidence: float
    support_count: int
    feedback_pos: int
    feedback_neg: int


class PreferenceFeedbackRequest(UserScopedRequest):
    preference_id: str = Field(min_length=1, max_length=32)
    helpful: bool


class PreferenceFeedbackOut(BaseModel):
    preference_id: str
    confidence: float
    support_count: int
    feedback_pos: int
    feedback_neg: int


class InsightListItem(BaseModel):
    """「我的经验」列表项（第十一轮）：经验库对用户可见的全部状态，含反馈计数与策展开关。"""
    id: str
    kind: str
    subject: str
    content: str
    applies_when: str
    project: str | None = None
    confidence: float = 0.5
    use_count: int = 0
    success_count: int = 0
    feedback_pos: int = 0
    feedback_neg: int = 0
    pinned: bool = False
    muted: bool = False
    version: int = 1
    source_episode_id: str | None = None
    valid_from: datetime
    last_used_at: datetime | None = None
    # 第十六轮：证据校验标记与应用/自动信号计数（面板上分得清曝光与真应用）
    verified: bool = True
    applied_count: int = 0
    auto_pos: int = 0
    auto_neg: int = 0


class InsightPage(BaseModel):
    """经验库分页（第二十五轮，与 MemoryPage 同构）：/v1/me/insights 不再返回
    无界列表，面板按页取，不再静默截断在 100 条。"""
    items: list["InsightListItem"]
    total: int
    offset: int
    limit: int


class DuelOut(BaseModel):
    """跨项目对决记录（第 36 轮）：一对分属不同项目的现行经验行的胜负
    裁决。胜负只活在这里——contradicts 边是对称标记，不承载方向。"""
    a_id: str
    b_id: str
    a_subject: str
    b_subject: str
    a_project: str
    b_project: str
    winner_id: str | None = None
    loser_id: str | None = None
    verdict: str  # win|tie
    scores: dict = {}
    criteria: str = "duel-v1"
    valid_from: datetime


class DuelPage(BaseModel):
    """对决记录分页（第 36 轮，与 InsightPage 同构）：只列现行记录。"""
    items: list[DuelOut]
    total: int
    offset: int
    limit: int


class EpisodeDetail(BaseModel):
    """轨迹详情（第二十五轮溯源落点）：面板点一条经验的“溯源”后看到的来源
    轨迹全文——目标、步骤、结果、提炼状态。用户域读接口，跨用户 404。"""
    id: str
    title: str
    source: str
    outcome: str
    project: str | None = None
    steps: list = []
    cost: dict = {}
    distill_status: str = "pending"
    salience: float = 0.0
    skeletonized: bool = False
    created_at: datetime


class InsightPatch(BaseModel):
    """经验单条编辑（第十一轮）：改内容/适用情境会作废旧向量（匹配面变了），置顶/禁用是策展开关。"""
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    applies_when: str | None = Field(default=None, max_length=2000)
    pinned: bool | None = None
    muted: bool | None = None


class InsightOut(BaseModel):
    insight_id: str
    kind: str  # lesson|strategy|pitfall|cost_saver
    subject: str
    content: str
    applies_when: str
    score: float
    reason: str
    # 第十轮：置信度（反馈回流算出）、所属项目、来源轨迹——简报里每条经验可溯源、可判断可信度
    confidence: float = 0.5
    project: str | None = None
    source_episode_id: str | None = None
    # 第十五轮「织网」：与同份简报里其它经验的矛盾边（对方 id 列表）；沿边扩散带出时
    # linked_from 是把它连带想起的那条经验 id（直接命中为空）
    conflicts_with: list[str] = []
    linked_from: str | None = None
    # 第十六轮 verifier：提炼时的轨迹证据引文核对通过与否；未通过的排序已打折
    verified: bool = True
    # 第十九轮：经验来源 agent 与证据是否链到具体步骤
    agent: str | None = None
    evidence_chained: bool = False


class ReminderOut(BaseModel):
    """前瞻记忆（第十七轮）：触发条件 + 要做的事 + 武装状态。"""
    reminder_id: str
    condition: str
    action: str
    project: str | None = None
    once: bool = True
    status: str = "armed"
    fire_count: int = 0


def reminder_out(row) -> ReminderOut:
    return ReminderOut(reminder_id=row.id, condition=row.condition_text,
                       action=row.action_text, project=row.project,
                       once=bool(row.once), status=row.status,
                       fire_count=row.fire_count or 0)


class ReminderCreateRequest(UserScopedRequest):
    condition: str = Field(min_length=1, max_length=500)
    action: str = Field(min_length=1, max_length=1000)
    project: str | None = Field(default=None, max_length=128)
    once: bool = True


class ReminderDismissRequest(UserScopedRequest):
    pass


class BriefResponse(BaseModel):
    insights: list[InsightOut]
    # 第十七轮：本次任务触发的前瞻记忆（「当 X 时做 Y」），与经验分开列
    reminders: list[ReminderOut] = []
    # 第十八轮：元记忆——简报是否降级（纯关键词），以及降级原因
    degraded: bool = False
    degraded_reason: str | None = None
    # 第二十轮：织网简报——胜出集的诱导子图 {nodes: [{id, subject}],
    # edges: [{link_id, src, dst, relation, strength}]}；每条经验的
    # linked_from 路径已在 InsightOut 上，不在这里重复。
    graph: dict | None = None
