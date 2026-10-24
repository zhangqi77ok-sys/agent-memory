"""v1 数据模型，对应 docs/architecture.md 第 4 节。"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text, UniqueConstraint, delete, event, insert, select, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.config import settings
from app.db import Base
from app.tokenize import MAX_TOKEN_LEN, tokens_of


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
    # 佐证置信度（第十七轮）：同一事实被独立会话重复抽到即 +1 佐证，
    # confidence = support/(support+1)（1 次 0.5、2 次 0.67、3 次 0.75…）。
    # 此前事实/偏好没有置信度概念——被说过一次和被证实五次排得一样靠前。
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    support_count: Mapped[int] = mapped_column(Integer, default=1)
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
    confidence: Mapped[float] = mapped_column(Float, default=0.5)  # 与事实同口径（第十七轮）
    support_count: Mapped[int] = mapped_column(Integer, default=1)
    embedding = mapped_column(Embedding, nullable=True)  # 第十八轮：偏好语义去重用
    embed_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class SessionSummary(UserScoped, Base):
    """会话摘要——预留表，当前未启用（第十二轮处置结论）。

    全仓没有任何生成或读取它的代码路径（只有 clear_all 会连带清它）：会话的
    长期价值已由事实抽取与经验提炼两条管线承接，再加一条摘要管线是重复建设。
    表结构保留待将来真有需求时启用；README 的范围说明已同步纠正，不再宣称
    summarize 是现行能力。"""
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
    # 编码显著性（第十七轮）：提交时按结果与成本算出（失败 > 部分 > 成功，成本/重试加权），
    # 提炼队列按它领取——重要的经历先复盘，不再纯 FIFO 让琐碎任务排在翻车前面。
    salience: Mapped[float] = mapped_column(Float, default=0.0)
    # 轨迹骨架化（第十七轮）：提炼完成且超过保留期的轨迹，中间步只留动作骨架
    # （观察细节丢弃、出错步与首尾步保全），细节让位给提炼出的经验，与人脑
    # 「只记要点、忘掉流水账」同构。True 表示已骨架化。
    skeletonized: Mapped[bool] = mapped_column(Boolean, default=False)
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
    # 信任三件套（第十六轮）：use_count 是「曝光」（简报递出），applied_count 才是
    # 「真被后续任务用上」（结果回流时记）——两者分开，马太效应才断得开：曝光不再
    # 刷新 last_used_at，只有应用与人工反馈才算「用过」。auto_pos/auto_neg 是回流
    # 产生的系统信号，进置信度时按半权计（任务成败不全是经验的功劳/过错）。
    applied_count: Mapped[int] = mapped_column(Integer, default=0)
    auto_pos: Mapped[int] = mapped_column(Integer, default=0)
    auto_neg: Mapped[int] = mapped_column(Integer, default=0)
    # 提炼证据校验（第十六轮 verifier）：提炼时附的轨迹原文引文验得过才为 True；
    # 验不过的经验不删，但简报排序打折、面板可见——假经验进不了门还不至于，
    # 先让它带标上岗、排在后面。
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    # 用户策展（第十一轮）：置顶=简报必达（仍受条数与 token 预算约束），禁用=永不召回。
    # 记忆越自动越要给人留开关——这两列就是经验库对用户的「可见可编辑」承诺的落点。
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        Index("ix_insight_app_user_subject", "app_id", "user_id", "subject"),
        # 置顶部分索引（第十四轮补完）：简报的置顶臂只捞 pinned 行，没有它要按
        # 用户全量扫一遍才知道谁置顶；置顶行永远是少数，部分索引只收它们。
        Index("ix_insight_pinned_scope", "app_id", "user_id",
              sqlite_where=text("pinned = 1"), postgresql_where=text("pinned")),
    )


class MemoryReminder(UserScoped, Base):
    """前瞻记忆（第十七轮）：「当 X 发生时，提醒我做 Y」。

    此前 applies_when 只是被动文本，要等简报按主题相似度捞经验时顺带命中；
    前瞻记忆是独立武装的意图：触发条件（condition）与当前任务做关键词/向量
    匹配，命中即在简报里单独亮出来。once=True 触发一次即 fired 落幕；
    once=False 常驻，记 fire_count/last_fired_at。status: armed|fired|dismissed。
    """

    __tablename__ = "memory_reminder"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    condition_text: Mapped[str] = mapped_column(Text)  # 触发条件，如「部署到生产环境时」
    action_text: Mapped[str] = mapped_column(Text)     # 要做的事，如「先跑数据库备份」
    project: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    once: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="armed", index=True)
    fire_count: Mapped[int] = mapped_column(Integer, default=0)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedding = mapped_column(Embedding, nullable=True)
    embed_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BriefDelivery(UserScoped, Base):
    """简报交付记录（第十六轮）：每次 brief 递出了哪些经验，落一条。

    结果回流的挂钩点：后续 episode 提交时，按用户+项目+时间窗找到未消费的
    交付，把任务成败记回这些经验（applied/auto 信号）。consumed_at 非空表示
    已回流过一次——一份简报只结一次账，防重复记功。
    """

    __tablename__ = "brief_delivery"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    project: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    task: Mapped[str] = mapped_column(Text, default="")  # 任务描述（截断存），仅作溯源
    insight_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryLink(UserScoped, Base):
    """记忆之间的关联边（第十五轮「织网」）：信息是点，这张表存线。

    只连记忆↔记忆（fact/insight 两类节点），不做世界实体图谱。relation：
    supports 佐证 / contradicts 矛盾 / refines 细化 / generalizes 泛化 /
    related 相关；前三者中的 supports、contradicts、related 语义对称，
    服务层按端点 id 序规范化存储，避免同一条边存两行。origin 标注边的
    出身：rule=确定性规则推出（结构提示）、llm=判定器语义判断、
    manual=用户手动连。边跟随端点生死：端点关行（版本滚动）时由
    migrate_links 迁移或关边，端点硬删时入口显式清边。
    """

    __tablename__ = "memory_link"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    src_kind: Mapped[str] = mapped_column(String(16))  # fact|insight
    src_id: Mapped[str] = mapped_column(String(32))
    dst_kind: Mapped[str] = mapped_column(String(16))
    dst_id: Mapped[str] = mapped_column(String(32))
    relation: Mapped[str] = mapped_column(String(16))
    strength: Mapped[float] = mapped_column(Float, default=0.5)  # 0..1，扩散激活按它打折
    origin: Mapped[str] = mapped_column(String(16), default="rule")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index("ix_link_src", "src_kind", "src_id"),
        Index("ix_link_dst", "dst_kind", "dst_id"),
    )


class MemoryFactToken(Base):
    """事实的关键词倒排索引（第十四轮补完）：一行一个 (事实, token)。

    内容与父行同事务维护（下方 ORM 事件），token 集合 = tokens_of(打分文本
    f"{subject} {content}")，所以召回探针在索引侧 COUNT 出的命中数与逐行
    kw_score 的分子严格相等——索引给的是精确计数，不是近似预筛。
    app_id/user_id 冗余存一份，让 PG 的 RLS 隔离策略与父表同形。
    硬删除走 Core delete 的路径（forget / clear_all）不触发 ORM 事件，
    在各自入口显式清本表；万一有残留，探针是 JOIN 父表取数，孤儿行
    永远不会被召回命中，只是占空间（scripts/rebuild_postings.py 可重整）。
    """

    __tablename__ = "memory_fact_token"
    fact_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    token: Mapped[str] = mapped_column(String(MAX_TOKEN_LEN), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        Index("ix_fact_token_token", "token", "fact_id"),
    )


class MemoryInsightToken(Base):
    """经验的关键词倒排索引，与 MemoryFactToken 同构（打分文本多一段 applies_when）。"""

    __tablename__ = "memory_insight_token"
    insight_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    token: Mapped[str] = mapped_column(String(MAX_TOKEN_LEN), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        Index("ix_insight_token_token", "token", "insight_id"),
    )


def _posting_sync(connection, table, id_key: str, target, text_value: str) -> None:
    """把某父行在倒排表里的 token 集合同步为 tokens_of(打分文本)。

    更新路径先比对现存集合：没变（简报记账、反馈计数这类非文本更新占绝大多数）
    只花一次索引查询，不写。超长 token 按 MAX_TOKEN_LEN 口径不入索引。
    """
    id_col = table.c[id_key]
    tokens = {t for t in tokens_of(text_value) if len(t) <= MAX_TOKEN_LEN}
    existing = {r[0] for r in connection.execute(select(table.c.token).where(id_col == target.id))}
    if existing == tokens:
        return
    if existing:
        connection.execute(delete(table).where(id_col == target.id))
    if tokens:
        connection.execute(insert(table), [
            {id_key: target.id, "token": t, "app_id": target.app_id, "user_id": target.user_id}
            for t in tokens])


def _register_posting_events(model, table, id_key: str, text_of) -> None:
    """父表 ORM 增删改 → 倒排同事务同步。全仓写路径都走 ORM（提炼、抽取、
    面板编辑、Markdown 导回无一例外），事件这一层兜住全部入口；
    Core delete 的硬删除路径在入口处显式清理（见 MemoryFactToken docstring）。"""

    @event.listens_for(model, "after_insert")
    def _ins(mapper, connection, target):
        _posting_sync(connection, table, id_key, target, text_of(target))

    @event.listens_for(model, "after_update")
    def _upd(mapper, connection, target):
        _posting_sync(connection, table, id_key, target, text_of(target))

    @event.listens_for(model, "after_delete")
    def _del(mapper, connection, target):
        connection.execute(delete(table).where(table.c[id_key] == target.id))


# 打分文本的拼法必须与召回侧逐字一致（recall_facts / brief_for_task 里的
# f-string）：倒排存的是这个文本的 token 集合，拼法漂移一格计数就对不上。
_register_posting_events(MemoryFact, MemoryFactToken.__table__, "fact_id",
                         lambda t: f"{t.subject} {t.content}")
_register_posting_events(MemoryInsight, MemoryInsightToken.__table__, "insight_id",
                         lambda t: f"{t.subject} {t.content} {t.applies_when}")


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
