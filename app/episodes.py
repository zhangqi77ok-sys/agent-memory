"""经验记忆的服务层：过程提交 → 复盘提炼 → 情境召回。

与事实管线平行但原料不同：事实来自对话文本，经验来自任务轨迹。
入库纪律与事实一致：脱敏在前、注入闸门在后、decide() 裁决去重/更新、
时间失效不真删、向量由 worker/提炼侧补齐且失败只留空不阻塞。
"""
import json
import logging
from datetime import timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import UserContext
from app.distill import KINDS
from app.embeddings import GeminiEmbedder, cosine
from app.extract import MAX_SUBJECT_LEN, InvalidExtraction, decide, is_injection, norm, same_subject, strip_injection
from app.models import AuditLog, MemoryInsight, TaskEpisode, _now
from app.recall import COS_MIN, MIN_SCORE, VECTOR_WEIGHT, _query_vector, _score
from app.redact import redact
from app.schemas import InsightOut

log = logging.getLogger(__name__)

MAX_DISTILL_ATTEMPTS = 3
EPISODE_LOCK_SECONDS = 120  # 领取锁定期，与 memory_job 的 LOCK_SECONDS 对齐；过期视为领取者已崩溃，可重领


def _redact_steps(steps: list[dict]) -> list[dict]:
    """轨迹入库前脱敏：轨迹最容易夹带命令输出、令牌、手机号，库里不能存明文。

    旧版只在送 LLM 前脱敏（_render_episode），落库的是原文——查库实测令牌与手机号皆为明文。
    现在入库与送模型用同一套 redact，库里、备份里、导出里都只有占位符。
    """
    out = []
    for st in steps or []:
        clean = {}
        for k, v in st.items():
            clean[k] = redact(v) if isinstance(v, str) else v
        out.append(clean)
    return out


def _redact_cost(cost: dict) -> dict:
    """成本字典同样过脱敏：键名保持（成本要可读），字符串值里的密钥/PII 换成占位符。"""
    return {k: (redact(v) if isinstance(v, str) else v) for k, v in (cost or {}).items()}


def find_episode_by_key(db: Session, app_id: str, user_id: str, idempotency_key: str) -> TaskEpisode | None:
    """按幂等键查已有 episode（提交防重的第一道：查到就复用，不重复落库）。"""
    if not idempotency_key:
        return None
    return db.scalar(select(TaskEpisode).where(
        TaskEpisode.app_id == app_id, TaskEpisode.user_id == user_id,
        TaskEpisode.idempotency_key == idempotency_key))


def submit_episode(db: Session, u: UserContext, *, source: str, title: str,
                   outcome: str, steps: list[dict], cost: dict,
                   idempotency_key: str | None = None) -> TaskEpisode:
    """落一条 episode。带 idempotency_key 时同一键只落一次：客户端（尤其会自己重试的
    工具调用）超时重试不会产生重复轨迹、不会重复烧提炼——语义与 /v1/memories 的写入幂等一致：
    查已有 → 返回已有；并发同键靠唯一约束兜底，撞上后回滚再查。"""
    if idempotency_key:
        existing = find_episode_by_key(db, u.app_id, u.user_id, idempotency_key)
        if existing is not None:
            return existing
    ep = TaskEpisode(app_id=u.app_id, user_id=u.user_id, source=source, title=redact(title),
                     outcome=outcome, steps=_redact_steps(steps), cost=_redact_cost(cost),
                     idempotency_key=idempotency_key)
    db.add(ep)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            existing = find_episode_by_key(db, u.app_id, u.user_id, idempotency_key)
            if existing is not None:
                return existing
        raise
    db.refresh(ep)
    return ep


def _render_episode(ep: TaskEpisode) -> str:
    """把轨迹渲染成给提炼器的文本；整体先过一遍 redact，步骤里夹带的 PII 不进 LLM。"""
    lines = [f"任务：{ep.title}", f"结果：{ep.outcome}",
             f"成本：{json.dumps(ep.cost or {}, ensure_ascii=False)}", "过程："]
    for i, st in enumerate(ep.steps or [], 1):
        line = f"{i}. {st.get('action', '')}"
        if st.get("observation"):
            line += f" → {st['observation']}"
        if st.get("error"):
            line += f"（出错：{st['error']}）"
        lines.append(line)
    return redact("\n".join(lines))


def _matched_insights(db: Session, app_id: str, user_id: str, subject: str) -> list[MemoryInsight]:
    """与 worker 对事实的预筛同构：先轻量取 (id, subject)，same_subject 命中才载整行，
    避免每提炼一条经验就把该用户的全部经验拉进内存。"""
    light = db.execute(
        select(MemoryInsight.id, MemoryInsight.subject).where(
            MemoryInsight.app_id == app_id, MemoryInsight.user_id == user_id,
            MemoryInsight.valid_to.is_(None))).all()
    ids = [row.id for row in light if same_subject(row.subject, subject)]
    if not ids:
        return []
    return list(db.scalars(select(MemoryInsight).where(MemoryInsight.id.in_(ids))))


def _active_content(rows: list[MemoryInsight]) -> str | None:
    """同主题现行内容：优先与新内容规范化相等的行（decide 的 NOOP 判定需要它），
    否则取最新一行。与 worker._active_value 同一套语义，两处各一份避免循环导入。"""
    if not rows:
        return None
    return rows[0].content


def _embed_insight(embedder: GeminiEmbedder | None, row: MemoryInsight) -> None:
    """给经验行补向量：匹配面是「主题 + 指导 + 适用情境」，情境召回才准。
    失败只留空（情境召回自动退化为关键词），与事实补向量同一纪律。"""
    if embedder is None:
        return
    try:
        vec = embedder.embed([f"{row.subject} {row.content} {row.applies_when}"],
                             task_type="RETRIEVAL_DOCUMENT")[0]
    except Exception as e:
        log.warning("insight embed failed id=%s: %s", row.id, e)
        return
    if vec:
        row.embedding = vec
        row.embed_model = embedder.model


def _apply_insight(db: Session, u: UserContext, cand, episode_id: str,
                   embedder: GeminiEmbedder | None) -> tuple[str, str | None]:
    """按 decide() 裁决写入一条经验，返回 (decision, insight_id)。"""
    matched = _matched_insights(db, u.app_id, u.user_id, cand.subject)
    same = next((r for r in matched if norm(r.content) == norm(cand.content)), None)
    existing = same.content if same is not None else _active_content(matched)
    decision = decide(existing, cand.content)
    if decision == "NOOP":
        return "noop", (same.id if same is not None else (matched[0].id if matched else None))
    now_version = max((r.version for r in matched), default=0) + 1
    for row in matched:
        if row is not same:
            row.valid_to = _now()
    row = MemoryInsight(app_id=u.app_id, user_id=u.user_id, kind=cand.kind,
                        subject=cand.subject, content=cand.content,
                        applies_when=cand.applies_when, version=now_version,
                        source_episode_id=episode_id)
    db.add(row)
    db.flush()
    _embed_insight(embedder, row)
    return decision.lower(), row.id


def _lock_user(db: Session, app_id: str, user_id: str) -> None:
    """同一用户的经验写入串行化，与 worker 对事实的 _lock_user 同一把咨询锁（同键）：
    两个 Worker 并发提炼同一用户的不同 episode 时，_apply_insight 的读-改-写不能交错，
    否则会写出双 active 经验。SQLite 单写者天然串行，跳过。"""
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k)::bigint)"),
                   {"k": f"{app_id}:{user_id}"})


def distill_episode(db: Session, ep: TaskEpisode, distiller, embedder: GeminiEmbedder | None = None) -> str:
    """对一个 episode 做复盘提炼并入库，返回聚合裁决 update > add > noop（无产出为 empty）。

    提炼器可以是对象（.distill(text)）或可调用；单条经验被注入闸门拦下只丢弃该条，
    不像事实抽取那样整批作废——轨迹里混入的恶意文本不该让正当经验陪葬，
    闸门拦截本身留痕在日志里方便复盘。
    """
    u = UserContext(ep.app_id, ep.user_id)
    _lock_user(db, ep.app_id, ep.user_id)
    text = strip_injection(_render_episode(ep)).strip()
    fn = distiller.distill if hasattr(distiller, "distill") else distiller
    candidates = fn(text) if text else []
    decisions: list[str] = []
    insight_ids: list[str] = []
    for cand in candidates:
        kind = getattr(cand, "kind", "")
        subject = redact(str(getattr(cand, "subject", "") or "")).strip()
        content = redact(str(getattr(cand, "content", "") or "")).strip()
        applies_when = redact(str(getattr(cand, "applies_when", "") or "")).strip()
        if kind not in KINDS or not subject or not content:
            continue
        if len(subject) > MAX_SUBJECT_LEN:
            continue  # 兜底：绕过解析层直达这里的候选同样不能把整批拖死在 PG 长度校验上
        if is_injection(subject) or is_injection(content) or is_injection(applies_when):
            log.warning("insight dropped by injection gate episode=%s subject=%s", ep.id, subject[:40])
            continue
        decision, iid = _apply_insight(db, u, _Cand(kind, subject, content, applies_when), ep.id, embedder)
        decisions.append(decision)
        if iid:
            insight_ids.append(iid)
    ep.distill_status = "done"
    ep.locked_until = None
    ep.last_error = None
    summary = ("update" if "update" in decisions else
               "add" if "add" in decisions else
               "noop" if decisions else "empty")
    db.add(AuditLog(app_id=ep.app_id, user_id=ep.user_id, action="distill",
                    fact_ids=insight_ids, decision=summary))
    db.commit()
    return summary


class _Cand:
    """_apply_insight 只需要四个字段；用小对象承接清洗后的候选，避免依赖提炼器的具体类型。"""
    __slots__ = ("kind", "subject", "content", "applies_when")

    def __init__(self, kind: str, subject: str, content: str, applies_when: str):
        self.kind, self.subject, self.content, self.applies_when = kind, subject, content, applies_when


def claim_episode(db: Session) -> TaskEpisode | None:
    """领取一条待提炼 episode，与 claim_job 同构：状态先转 running 并设锁定期，
    SKIP LOCKED 只防同一瞬间，状态+锁定才防得住「领了还没处理完又被别人领走」。
    旧版只 attempts+1 不改状态，同一条能被连续领两次（实测可复现），多 Worker 必重复提炼。
    running 且锁已过期的视为领取者崩溃，可重领；attempts 达上限的不再领取。"""
    now = _now()
    ep = db.scalars(
        select(TaskEpisode).where(
            TaskEpisode.attempts < MAX_DISTILL_ATTEMPTS,
            or_(TaskEpisode.distill_status == "pending",
                (TaskEpisode.distill_status == "running") & (TaskEpisode.locked_until < now)))
        .order_by(TaskEpisode.created_at).limit(1)
        .with_for_update(skip_locked=True)).first()
    if ep is None:
        return None
    ep.distill_status = "running"
    ep.attempts += 1
    ep.locked_until = now + timedelta(seconds=EPISODE_LOCK_SECONDS)
    db.commit()
    return ep


def run_episode_once(db: Session, distiller, embedder: GeminiEmbedder | None = None) -> TaskEpisode | None:
    """worker 的一轮经验提炼。与 run_once 同构：坏输出整批丢弃并记 distill=discard；
    瞬时故障退回 pending 重试，超过上限标 failed 并留错误，不无限循环。"""
    ep = claim_episode(db)
    if ep is None:
        return None
    try:
        distill_episode(db, ep, distiller, embedder)
    except InvalidExtraction:
        db.rollback()
        ep = db.get(TaskEpisode, ep.id)
        ep.distill_status = "done"
        ep.locked_until = None
        ep.last_error = "discarded: invalid distiller output"
        db.add(AuditLog(app_id=ep.app_id, user_id=ep.user_id, action="distill",
                        decision="discard"))
        db.commit()
    except Exception as e:  # 瞬时故障：计数后退回重试或标失败，错误留痕
        db.rollback()
        ep = db.get(TaskEpisode, ep.id)
        ep.distill_status = "failed" if ep.attempts >= MAX_DISTILL_ATTEMPTS else "pending"
        ep.locked_until = None
        ep.last_error = str(e)[:2000]
        db.commit()
    return db.get(TaskEpisode, ep.id)


def brief_for_task(db: Session, u: UserContext, task: str, k: int = 5,
                   embedder: GeminiEmbedder | None = None) -> list[InsightOut]:
    """情境召回：新任务开始前，按任务描述捞出适用的历史经验。

    排序与门槛完全沿用事实混合召回（0.7 向量 + 0.3 关键词；入选需关键词命中
    或余弦过 COS_MIN 标定线），匹配面是 主题+指导+适用情境。
    无向量时退化纯关键词，老数据零成本兼容。
    """
    rows = list(db.scalars(select(MemoryInsight).where(
        MemoryInsight.app_id == u.app_id, MemoryInsight.user_id == u.user_id,
        MemoryInsight.valid_to.is_(None))))
    if not rows:
        return []
    qvec = _query_vector(embedder, task)
    scored: list[tuple[float, MemoryInsight, str]] = []
    for row in rows:
        text = f"{row.subject} {row.content} {row.applies_when}"
        kw = _score(task, text)
        cos = None
        if qvec is not None and row.embedding:
            cos = cosine(qvec, [float(x) for x in row.embedding])
        if cos is not None:
            if kw < MIN_SCORE and cos < COS_MIN:
                continue
            final = VECTOR_WEIGHT * cos + (1 - VECTOR_WEIGHT) * kw
            reason = "hybrid"
        else:
            if kw < MIN_SCORE:
                continue
            final, reason = kw, "keyword"
        scored.append((final, row, reason))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [InsightOut(insight_id=row.id, kind=row.kind, subject=row.subject,
                       content=row.content, applies_when=row.applies_when,
                       score=round(score, 4), reason=reason)
            for score, row, reason in scored[:k]]
