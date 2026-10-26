"""前瞻记忆服务层（第十七轮）：「当 X 发生时，提醒我做 Y」的武装、触发与落幕。

人的记忆有一类不是回忆过去，而是记住将来要做的事（前瞻记忆）。此前系统
只有被动匹配：经验躺在库里等简报按相似度捞。前瞻记忆是主动武装的意图——
触发条件与新任务做匹配（关键词重叠或向量余弦），命中就在简报里单独亮出，
不与经验争排序名额。once 型确信触发一次即落幕（fired），常驻型记次数继续武装；
第九轮起纯向量灰区命中（低于确信线）只展示、不落幕，见 check_triggers。
"""
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import UserContext
from app.embeddings import GeminiEmbedder, cosine
from app.models import MemoryReminder, _now
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


def create_reminder(db: Session, u: UserContext, *, condition: str, action: str,
                    project: str | None = None, once: bool = True,
                    embedder: GeminiEmbedder | None = None) -> MemoryReminder:
    """武装一条前瞻记忆。条件文本顺手补向量（失败留空，触发时退化为关键词匹配）。"""
    row = MemoryReminder(app_id=u.app_id, user_id=u.user_id,
                         condition_text=condition.strip(), action_text=action.strip(),
                         project=project, once=bool(once))
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


def check_triggers(db: Session, u: UserContext, task: str,
                   embedder: GeminiEmbedder | None = None, *,
                   project: str | None = None) -> list[MemoryReminder]:
    """拿新任务去撞所有武装中的前瞻记忆，返回展示的（已按规则记账并 commit）。

    项目相容才触发（提醒带项目且与任务项目明确不同 → 不撞）。
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
    # 第十九轮：命中按分数降序、同一动作去重（动作规范化后相同只留分数最高者）。
    # 记账只对最终返回的做——被去重掉的那条不记 fire_count，否则简报里没出现
    # 的提醒也在账上“触发过”，误触发率就永远量不准。
    scored: list[tuple[float, str, float | None, MemoryReminder]] = []
    for row in rows:
        if row.project and project and row.project != project:
            continue
        detail = _trigger_detail(row, task_tokens, qvec, getattr(embedder, "model", None))
        if detail is None:
            continue
        score, kind, cos = detail
        scored.append((score, kind, cos, row))
    scored.sort(key=lambda t: t[0], reverse=True)
    seen_actions: set[str] = set()
    fired: list[MemoryReminder] = []
    for _score, kind, cos, row in scored:
        action_key = " ".join((row.action_text or "").split()).lower()
        if action_key in seen_actions:
            continue
        seen_actions.add(action_key)
        row.fire_count = (row.fire_count or 0) + 1
        row.last_fired_at = now
        # 灰区：纯向量命中且低于确信线时，once 型不落幕（只展示）。
        gray = kind == "vector" and cos is not None and cos < REMINDER_VECTOR_CONFIDENT
        if row.once and not gray:
            row.status = "fired"
        fired.append(row)
    if fired:
        db.commit()
    return fired
