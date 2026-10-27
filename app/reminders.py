"""前瞻记忆服务层（第十七轮）：「当 X 发生时，提醒我做 Y」的武装、触发与落幕。

人的记忆有一类不是回忆过去，而是记住将来要做的事（前瞻记忆）。此前系统
只有被动匹配：经验躺在库里等简报按相似度捞。前瞻记忆是主动武装的意图——
触发条件与新任务做匹配（关键词重叠或向量余弦），命中就在简报里单独亮出，
不与经验争排序名额。once 型确信触发一次即落幕（fired），常驻型记次数继续武装；
第九轮起纯向量灰区命中（低于确信线）只展示、不落幕，见 check_triggers。
"""
import logging
from datetime import timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import working_memory as _wm
from app.auth import UserContext
from app.embeddings import GeminiEmbedder, cosine
from app.models import MemoryInsight, MemoryReminder, MemoryReminderDelivery, MemoryWorkingEntry, _now
from app.recall import COS_MIN_REMINDER
from app.tokenize import tokens_of

log = logging.getLogger(__name__)

TRIGGER_OVERLAP_MIN = 0.5  # 条件 token 被任务覆盖的比例下限（按较小集合计）
SHORT_COND_TOKENS = 2  # 第二十七轮：条件 token 数 ≤ 此值时关键词路必须全中（见 _trigger_score）
# 纯向量“确信线”（第九轮修复，审计 A2 灰区机制）：纯向量命中且 cos ≥ 此线
# 才消耗 once 型提醒；低于此线的纯向量命中只展示、不落幕（见 check_triggers）。
# 临时值：第 31 轮提醒批向量负例最高 0.769 + 裕量（2026-10-26 重跑确认），
# 正式标定需更大探针集。关键词命中（含关键词+向量双中）不受此线影响——
# 关键词佐证本身就是确信证据。
REMINDER_VECTOR_CONFIDENT = 0.80
# 第 38 轮：投递账本保留上限（每提醒，论证值，未按真实提醒量标定）与
# 账本任务串截断（与 BriefDelivery.task 同款“截断存、仅作溯源”口径）。
REMINDER_LEDGER_CAP = 200
LEDGER_TASK_CAP = 500


def create_reminder(db: Session, u: UserContext, *, condition: str, action: str,
                    project: str | None = None, once: bool = True,
                    embedder: GeminiEmbedder | None = None,
                    expires_at=None) -> MemoryReminder:
    """武装一条前瞻记忆。条件文本顺手补向量（失败留空，触发时退化为关键词匹配）。

    第 38 轮：expires_at 可选截止时间；None = 永不过期（旧调用点一律 None，
    行为不变）。过去时间也接受——建完即过期，下次检查时惰性落幕，不在
    创建时拒绝（调用方时钟与服务端时钟差几秒就 422 是误伤）。"""
    row = MemoryReminder(app_id=u.app_id, user_id=u.user_id,
                         condition_text=condition.strip(), action_text=action.strip(),
                         project=project, once=bool(once), expires_at=expires_at)
    db.add(row)
    db.flush()
    if embedder is not None:
        try:
            vec = embedder.embed([row.condition_text], task_type="RETRIEVAL_DOCUMENT")[0]
            if vec:
                row.embedding = vec
                row.embed_model = embedder.model
        except Exception as e:
            log.warning("reminder embed failed id=%s: %s", row.id, e)
    db.commit()
    db.refresh(row)
    return row


def list_reminders(db: Session, u: UserContext, *, include_closed: bool = False) -> list[MemoryReminder]:
    q = select(MemoryReminder).where(
        MemoryReminder.app_id == u.app_id, MemoryReminder.user_id == u.user_id)
    if not include_closed:
        q = q.where(MemoryReminder.status == "armed")
    return list(db.scalars(q.order_by(MemoryReminder.created_at)))


def dismiss_reminder(db: Session, u: UserContext, reminder_id: str) -> MemoryReminder:
    """解除一条前瞻记忆（本人名下才行；不存在/他人的一律 ValueError → 404）。"""
    row = db.scalar(select(MemoryReminder).where(
        MemoryReminder.id == reminder_id, MemoryReminder.app_id == u.app_id,
        MemoryReminder.user_id == u.user_id))
    if row is None:
        raise ValueError("reminder not found")
    row.status = "dismissed"
    db.commit()
    return row


def _record_delivery(db: Session, app_id: str, user_id: str, reminder_id: str, *,
                     event: str, task: str, timing: str | None, kind: str | None,
                     score: float | None, now) -> None:
    """记一笔投递账（第 38 轮）：调用方负责 commit，本函数只加行 + 裁剪。

    裁剪：同一提醒账本超 REMINDER_LEDGER_CAP 条时删最旧的多余行（按
    (created_at, id) 排序；同 check 内同一提醒至多一事件，created_at 并列
    只可能跨调用时钟回拨，id tie-break 只为裁剪确定）。"""
    db.add(MemoryReminderDelivery(
        app_id=app_id, user_id=user_id, reminder_id=reminder_id, event=event,
        task=(task or "")[:LEDGER_TASK_CAP], timing=timing, kind=kind,
        score=score, created_at=now))
    db.flush()
    ids = [r[0] for r in db.execute(
        select(MemoryReminderDelivery.id).where(
            MemoryReminderDelivery.reminder_id == reminder_id)
        .order_by(MemoryReminderDelivery.created_at, MemoryReminderDelivery.id)).all()]
    if len(ids) > REMINDER_LEDGER_CAP:
        db.execute(delete(MemoryReminderDelivery).where(
            MemoryReminderDelivery.id.in_(ids[:len(ids) - REMINDER_LEDGER_CAP])))


def _expire_due_rows(db: Session, rows: list[MemoryReminder], task: str, now) -> list[MemoryReminder]:
    """把已到截止时间的 armed 行标 expired（第 38 轮读时惰性过期）。

    返回过期行。调用方负责 commit。过期是终态：过期行永不触发、永不救援、
    不占同动作去重名额——调用方须在评分前排除这些行。"""
    expired = []
    for row in rows:
        exp = _aware(row.expires_at)
        if row.status == "armed" and exp is not None and exp <= now:
            row.status = "expired"
            row.last_timing = "expired"
            _record_delivery(db, row.app_id, row.user_id, row.id, event="expired",
                             task=task, timing="expired", kind=None, score=None, now=now)
            expired.append(row)
    return expired


def expire_reminders(db: Session, now=None) -> dict:
    """巡检过期步（第 38 轮）：把已到截止时间的 armed 提醒标 expired。

    不依赖 ttl_days（截止是显式的，不是保留期）；显式关掉记忆的用户按
    fix4 §5 口径跳过（switched_off_users）。返回 {"expired": n} 进
    patrol stats。读时惰性过期（check_triggers 内）是另一执行点，两处
    共用 _expire_due_rows 的语义，巡检步只是批量补齐。"""
    from app.api.me import switched_off_users

    now = now or _now()
    off = switched_off_users(db)
    rows = [r for r in db.scalars(select(MemoryReminder).where(
        MemoryReminder.status == "armed",
        MemoryReminder.expires_at.is_not(None))).all()
        if (r.app_id, r.user_id) not in off]
    expired = _expire_due_rows(db, rows, task="", now=now)
    if expired:
        db.commit()
    return {"expired": len(expired)}


def list_reminder_deliveries(db: Session, u: UserContext, reminder_id: str,
                             limit: int = 100) -> list[MemoryReminderDelivery]:
    """查一条提醒的投递账本（第 38 轮）：先确权提醒行归属（他人/不存在
    → ValueError → 404，与 dismiss 同形），再按时间倒序取账本。"""
    row = db.scalar(select(MemoryReminder).where(
        MemoryReminder.id == reminder_id, MemoryReminder.app_id == u.app_id,
        MemoryReminder.user_id == u.user_id))
    if row is None:
        raise ValueError("reminder not found")
    limit = max(1, min(int(limit), 100))
    return list(db.scalars(select(MemoryReminderDelivery).where(
        MemoryReminderDelivery.app_id == u.app_id,
        MemoryReminderDelivery.user_id == u.user_id,
        MemoryReminderDelivery.reminder_id == reminder_id)
        .order_by(MemoryReminderDelivery.created_at.desc(),
                  MemoryReminderDelivery.id.desc()).limit(limit)))


def _trigger_detail(row: MemoryReminder, task_tokens: set[str],
                    qvec: list[float] | None, embedder_model: str | None
                    ) -> tuple[float, str, float | None] | None:
    """触发明细（第九轮修复）：返回 (分数, 来源, 向量余弦)，未命中返回 None。

    来源 kind ∈ {keyword, vector, both}：两路都命中取高者并记 both——
    check_triggers 的灰区规则只看“纯向量命中”（kind == "vector"），有
    关键词佐证的命中（keyword/both）一律按确信处理。向量路触发线是
    COS_MIN_REMINDER（第九轮起与召回线拆分，初始同值 0.66）。"""
    cond_tokens = tokens_of(row.condition_text)
    best = None
    kind = None
    cos_hit = None
    if cond_tokens:
        overlap = len(cond_tokens & task_tokens) / len(cond_tokens)
        # 第二十七轮短条件全匹配：条件只有 ≤2 个 token 时，单个共享 bigram
        # 证据太弱（“上线”满大街都是，0.5 的半中全是误触），必须全中才算
        # 关键词命中；向量路不受影响。平抬阈值不是解法——0.75 会误杀 0.50
        # 的真阳性（如“再部署一个服务”）。
        required = 1.0 if len(cond_tokens) <= SHORT_COND_TOKENS else TRIGGER_OVERLAP_MIN
        if overlap >= required:
            best = overlap
            kind = "keyword"
    if (qvec is not None and row.embedding is not None
            and row.embed_model == embedder_model):
        vec = [float(x) for x in row.embedding]
        if len(vec) == len(qvec):
            cos = cosine(qvec, vec)
            if cos >= COS_MIN_REMINDER:
                cos_hit = cos
                if best is None:
                    best = cos
                    kind = "vector"
                elif cos > best:
                    best = cos
                    kind = "both"
                else:
                    kind = "both"
    if best is None:
        return None
    return best, kind, cos_hit


def _trigger_score(row: MemoryReminder, task_tokens: set[str],
                   qvec: list[float] | None, embedder_model: str | None) -> float | None:
    """触发分数（第十九轮）：返回命中时的强度，未命中返回 None。

    关键词路分数 = 条件 token 被任务覆盖的比例；向量路分数 = 余弦。两路都
    命中取高者。此前 check_triggers 只返回布尔——命中多条时既无先后也无
    去重，同一动作换个说法武装两次就会在简报里出现两次。
    第九轮起实现为 _trigger_detail 的薄包装（只取分数），判定口径不变。"""
    detail = _trigger_detail(row, task_tokens, qvec, embedder_model)
    return detail[0] if detail is not None else None


def _triggered(row: MemoryReminder, task: str, task_tokens: set[str],
               qvec: list[float] | None, embedder_model: str | None) -> bool:
    """触发判定：关键词路（条件 token 被任务覆盖 ≥50%，按条件集合计——条件是短句，
    分母用它才不会被长任务描述稀释）或向量路（余弦 ≥ 提醒触发线
    COS_MIN_REMINDER，第九轮起与召回线拆分，初始同值 0.66）。"""
    return _trigger_score(row, task_tokens, qvec, embedder_model) is not None


def _aware(dt):
    """SQLite 读回的 datetime 是 naive，统一转 aware 再比较（与 working_memory 同口径）。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def session_context_tokens(db: Session, u: UserContext, session_id, project: str | None) -> set[str] | None:
    """第 37 轮：会话上下文 token 集（只读，不碰缓冲写）。

    经 working_memory.buffer_enabled 归一化：非法 session_id、开关关闭用户
    → None = 无上下文。取本会话条目，回表取现行行，跳过不存在/已关闭/
    被禁/版本变化/失活（24h TTL）的行——只跳过、不删条目，清理是下次简报
    fill_candidates 的事。项目口径与 fill_candidates 一致（条目项目为空
    或等于本次简报项目）。返回 None 表示“上下文缺席”（调用方走旧行为）；
    返回空集表示“会话在场但无可用行”（灰区按不相关暂缓，救援不触发）。
    成本 O(WORKING_CAP)，与存量总行数无关。"""
    from datetime import timedelta

    sid = _wm.buffer_enabled(db, u.app_id, u.user_id, session_id)
    if sid is None:
        return None
    entries = list(
        db.scalars(
            select(MemoryWorkingEntry).where(
                MemoryWorkingEntry.app_id == u.app_id,
                MemoryWorkingEntry.user_id == u.user_id,
                MemoryWorkingEntry.session_id == sid,
            )
        )
    )
    if not entries:
        return None
    now = _now()
    cutoff = now - timedelta(hours=_wm.WORKING_TTL_HOURS)
    wanted = [
        e
        for e in entries
        if (e.project is None or project is None or e.project == project)
        and (_aware(e.last_touched) is None or _aware(e.last_touched) >= cutoff)
    ]
    if not wanted:
        return None
    rows = {
        r.id: r
        for r in db.scalars(
            select(MemoryInsight).where(
                MemoryInsight.app_id == u.app_id,
                MemoryInsight.user_id == u.user_id,
                MemoryInsight.id.in_([e.insight_id for e in wanted]),
            )
        )
    }
    tokens: set[str] = set()
    valid = False
    for e in wanted:
        r = rows.get(e.insight_id)
        if r is None or r.valid_to is not None or (r.muted is not None and r.muted) or (r.version or 1) != (e.version or 1):
            continue
        valid = True
        tokens |= tokens_of(r.subject or "")
        tokens |= tokens_of(r.content or "")
    if not valid:
        return None
    return tokens


def context_relevant(cond_tokens: set[str], ctx_tokens: set[str] | None) -> bool:
    """第 37 轮：条件与会话上下文是否相关。

    复用关键词路证据线：overlap ≥ TRIGGER_OVERLAP_MIN（0.5）；短条件
    （≤ SHORT_COND_TOKENS 个 token）要求全中——与第 27 轮短条件规则逐字
    一致。上下文缺席（None）或条件 token 为空 → 不相关。"""
    if ctx_tokens is None or not cond_tokens:
        return False
    overlap = len(cond_tokens & ctx_tokens) / len(cond_tokens)
    required = 1.0 if len(cond_tokens) <= SHORT_COND_TOKENS else TRIGGER_OVERLAP_MIN
    return overlap >= required


def check_triggers(db: Session, u: UserContext, task: str,
                   embedder: GeminiEmbedder | None = None, *,
                   project: str | None = None, session_id=None) -> list[MemoryReminder]:
    """拿新任务去撞所有武装中的前瞻记忆，返回展示的（已按规则记账并 commit）。

    项目相容才触发（提醒带项目且与任务项目明确不同 → 不撞）。
    第 37 轮时机层（内容判定之后叠加，内容阈值一律不动）：
    - 强命中（关键词/both、确信向量）→ 立即触发，上下文不拦；
    - 灰区命中（纯向量 < 确信线）→ 上下文缺席或相关时照旧展示，上下文
      在场且不相关时暂缓（不展示、不记 fire_count、不消费，hold_count+1）；
    - 无内容命中但上下文相关 → 救援展示（fire_count+1，once 型永不因此落幕）。
    session_id=None（旧调用点）→ 上下文缺席，行为与第 37 轮之前逐字一致。
    第 38 轮账本与过期：评分前已到截止时间的 armed 行先标 expired 并记账
    （惰性过期，巡检过期步是另一执行点）；每种最终结论落地时记一笔投递
    账（shown / consumed / rescued / held），被去重掉的行与未命中的行不记账；
    无事件时零写（无命中检查不 commit 的旧规则不变）。
    第九轮起返回列表的语义是“展示的提醒”，不再等同“已消耗的提醒”：
    - 关键词命中（含关键词+向量双中）→ 旧规则：once 型 fired 落幕；
    - 纯向量命中且 cos ≥ REMINDER_VECTOR_CONFIDENT（确信线）→ 旧规则；
    - 纯向量命中且 cos < 确信线（灰区）→ 返回展示、fire_count+1、
      last_fired_at 照记，但 once 型 status 保持 armed、不落幕——
      第 31 轮证明短文本向量正负不可分（重叠区 0.733–0.769），一次
      灰区误触不应永久丢失用户意图；下次相似任务再展示，直至确信
      触发或关键词命中才落幕。简报序列化（reminder_out）本来就带
      status 与 fire_count，消费方可凭 status 区分 fired 与仍 armed
      的灰区提醒，无需改 schema。
    常驻型提醒本来就不落幕，灰区与否只影响展示，fire_count 照记。
    简报入口与独立触发检查共用此函数。"""
    rows = list_reminders(db, u)
    if not rows:
        return []
    task_tokens = tokens_of(task)
    qvec = None
    if embedder is not None:
        try:
            qvec = embedder.embed([task], task_type="RETRIEVAL_QUERY")[0]
        except Exception as e:
            log.warning("trigger query embed failed: %s", e)
            qvec = None
    now = _now()
    # 第 38 轮：读时惰性过期——评分前先把已到截止时间的行标 expired 并记账，
    # 排除出候选池（过期行永不触发、永不救援、不占去重名额）。巡检过期步
    # 是另一执行点，两处语义一致。
    expired = _expire_due_rows(db, rows, task, now)
    rows = [r for r in rows if r.status == "armed"]
    # 第 37 轮：会话上下文只算一次（无 session_id 时不查，旧行为零成本）。
    ctx_tokens = session_context_tokens(db, u, session_id, project) if session_id is not None else None
    # 第十九轮：命中按分数降序、同一动作去重（动作规范化后相同只留分数最高者）。
    # 记账只对最终返回的做——被去重掉的那条不记 fire_count，否则简报里没出现
    # 的提醒也在账上“触发过”，误触发率就永远量不准。
    # 第 37 轮：scored 元组末位是时机结论（content/context），暂缓行先出池，
    # 不占同动作去重名额、不记 fire_count。
    scored: list[tuple[float, str, float | None, MemoryReminder, str]] = []
    held: list[MemoryReminder] = []
    for row in rows:
        if row.project and project and row.project != project:
            continue
        detail = _trigger_detail(row, task_tokens, qvec, getattr(embedder, "model", None))
        cond_tokens = tokens_of(row.condition_text)
        if detail is None:
            # 救援：内容两路未命中，但会话上下文达到关键词路同一条证据线。
            if ctx_tokens is not None and context_relevant(cond_tokens, ctx_tokens):
                overlap = len(cond_tokens & ctx_tokens) / len(cond_tokens)
                scored.append((overlap, "context", None, row, "context"))
            continue
        score, kind, cos = detail
        gray = kind == "vector" and cos is not None and cos < REMINDER_VECTOR_CONFIDENT
        if gray and ctx_tokens is not None and not context_relevant(cond_tokens, ctx_tokens):
            # 暂缓：弱猜测不在无关时刻展示；不记 fire_count、不消费，
            # 下次上下文变相关或缺席时照常展示。
            row.hold_count = (row.hold_count or 0) + 1
            row.last_held_at = now
            row.last_timing = "held"
            _record_delivery(db, row.app_id, row.user_id, row.id, event="held",
                             task=task, timing="held", kind="vector", score=cos, now=now)
            held.append(row)
            continue
        scored.append((score, kind, cos, row, "content"))
    scored.sort(key=lambda t: t[0], reverse=True)
    seen_actions: set[str] = set()
    fired: list[MemoryReminder] = []
    for _score, kind, cos, row, timing in scored:
        action_key = " ".join((row.action_text or "").split()).lower()
        if action_key in seen_actions:
            continue
        seen_actions.add(action_key)
        row.fire_count = (row.fire_count or 0) + 1
        row.last_fired_at = now
        row.last_timing = timing
        # 灰区：纯向量命中且低于确信线时，once 型不落幕（只展示）。
        # 救援（kind == "context"）同样永不落幕——启发式证据不配消费一次性意图。
        gray = kind == "vector" and cos is not None and cos < REMINDER_VECTOR_CONFIDENT
        if row.once and not gray and kind != "context":
            row.status = "fired"
            event = "consumed"  # once 强命中：展示 + 落幕一次记一条，不 double-count
        elif kind == "context":
            event = "rescued"
        else:
            event = "shown"
        _record_delivery(db, row.app_id, row.user_id, row.id, event=event,
                         task=task, timing=timing, kind=kind, score=_score, now=now)
        fired.append(row)
    if fired or held or expired:
        db.commit()
    return fired
