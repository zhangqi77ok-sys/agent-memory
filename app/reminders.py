"""前瞻记忆服务层（第十七轮）：「当 X 发生时，提醒我做 Y」的武装、触发与落幕。

人的记忆有一类不是回忆过去，而是记住将来要做的事（前瞻记忆）。此前系统
只有被动匹配：经验躺在库里等简报按相似度捞。前瞻记忆是主动武装的意图——
触发条件与新任务做匹配（关键词重叠或向量余弦），命中就在简报里单独亮出，
不与经验争排序名额。once 型触发一次即落幕（fired），常驻型记次数继续武装。
"""
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import UserContext
from app.embeddings import GeminiEmbedder, cosine
from app.models import MemoryReminder, _now
from app.recall import COS_MIN
from app.tokenize import tokens_of

log = logging.getLogger(__name__)

TRIGGER_OVERLAP_MIN = 0.5  # 条件 token 被任务覆盖的比例下限（按较小集合计）


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


def _trigger_score(row: MemoryReminder, task_tokens: set[str],
                   qvec: list[float] | None, embedder_model: str | None) -> float | None:
    """触发分数（第十九轮）：返回命中时的强度，未命中返回 None。

    关键词路分数 = 条件 token 被任务覆盖的比例；向量路分数 = 余弦。两路都
    命中取高者。此前 check_triggers 只返回布尔——命中多条时既无先后也无
    去重，同一动作换个说法武装两次就会在简报里出现两次。"""
    cond_tokens = tokens_of(row.condition_text)
    best = None
    if cond_tokens:
        overlap = len(cond_tokens & task_tokens) / len(cond_tokens)
        if overlap >= TRIGGER_OVERLAP_MIN:
            best = overlap
    if (qvec is not None and row.embedding is not None
            and row.embed_model == embedder_model):
        vec = [float(x) for x in row.embedding]
        if len(vec) == len(qvec):
            cos = cosine(qvec, vec)
            if cos >= COS_MIN and (best is None or cos > best):
                best = cos
    return best


def _triggered(row: MemoryReminder, task: str, task_tokens: set[str],
               qvec: list[float] | None, embedder_model: str | None) -> bool:
    """触发判定：关键词路（条件 token 被任务覆盖 ≥50%，按条件集合计——条件是短句，
    分母用它才不会被长任务描述稀释）或向量路（余弦 ≥ COS_MIN，与召回同一标定线）。"""
    return _trigger_score(row, task_tokens, qvec, embedder_model) is not None


def check_triggers(db: Session, u: UserContext, task: str,
                   embedder: GeminiEmbedder | None = None, *,
                   project: str | None = None) -> list[MemoryReminder]:
    """拿新任务去撞所有武装中的前瞻记忆，返回命中的（已按规则记账并 commit）。

    项目相容才触发（提醒带项目且与任务项目明确不同 → 不撞）。once 型命中即
    fired 落幕；常驻型 fire_count+1 继续武装。简报入口与独立触发检查共用此函数。"""
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
    scored: list[tuple[float, MemoryReminder]] = []
    for row in rows:
        if row.project and project and row.project != project:
            continue
        score = _trigger_score(row, task_tokens, qvec, getattr(embedder, "model", None))
        if score is None:
            continue
        scored.append((score, row))
    scored.sort(key=lambda t: t[0], reverse=True)
    seen_actions: set[str] = set()
    fired: list[MemoryReminder] = []
    for _score, row in scored:
        action_key = " ".join((row.action_text or "").split()).lower()
        if action_key in seen_actions:
            continue
        seen_actions.add(action_key)
        row.fire_count = (row.fire_count or 0) + 1
        row.last_fired_at = now
        if row.once:
            row.status = "fired"
        fired.append(row)
    if fired:
        db.commit()
    return fired
