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
from app.embeddings import GeminiEmbedder, cosine, vector_usable
from app.extract import MAX_SUBJECT_LEN, InvalidExtraction, decide, is_injection, norm, same_subject, strip_injection
from app.models import AuditLog, MemoryInsight, TaskEpisode, _now
from app.recall import COS_MIN, MIN_SCORE, VECTOR_WEIGHT, _est_tokens, _query_vector, _score
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
                   idempotency_key: str | None = None,
                   project: str | None = None) -> TaskEpisode:
    """落一条 episode。带 idempotency_key 时同一键只落一次：客户端（尤其会自己重试的
    工具调用）超时重试不会产生重复轨迹、不会重复烧提炼——语义与 /v1/memories 的写入幂等一致：
    查已有 → 返回已有；并发同键靠唯一约束兜底，撞上后回滚再查。"""
    if idempotency_key:
        existing = find_episode_by_key(db, u.app_id, u.user_id, idempotency_key)
        if existing is not None:
            return existing
    ep = TaskEpisode(app_id=u.app_id, user_id=u.user_id, source=source, title=redact(title),
                     outcome=outcome, steps=_redact_steps(steps), cost=_redact_cost(cost),
                     idempotency_key=idempotency_key, project=project)
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


# 提炼输入上限（第九轮）：steps 入口允许 200 步 × 每步约 4.5K 字符，整份渲染可达 90 万字符，
# 全量喂给提炼器既烧钱又容易超时。超限时按「出错步、首尾步优先，其余按原序填充」取舍，
# 并插入省略标记——让模型知道中间被截断过，而不是误以为轨迹完整。
MAX_RENDER_CHARS = 24000
MAX_STEP_LINE_CHARS = 800


def _render_episode(ep: TaskEpisode) -> str:
    """把轨迹渲染成给提炼器的文本；整体先过一遍 redact，步骤里夹带的 PII 不进 LLM。

    渲染结果不超过 MAX_RENDER_CHARS：短轨迹原样输出；长轨迹先把单步压到
    MAX_STEP_LINE_CHARS，再优先保留出错步与首尾各三步，其余按原序装到装满为止，
    被省略的区间用标记行标明步号范围与步数。"""
    header = [f"任务：{ep.title}", f"结果：{ep.outcome}",
              f"成本：{json.dumps(ep.cost or {}, ensure_ascii=False)}", "过程："]
    raw_steps = ep.steps or []
    step_lines = []
    for i, st in enumerate(raw_steps, 1):
        line = f"{i}. {st.get('action', '')}"
        if st.get("observation"):
            line += f" → {st['observation']}"
        if st.get("error"):
            line += f"（出错：{st['error']}）"
        step_lines.append(line)
    full = "\n".join(header + step_lines)
    if len(full) <= MAX_RENDER_CHARS:
        return redact(full)
    # 超限：单步按段压缩——动作/观察/错误分别限长，错误段在行尾绝不能被整行截断切掉
    def _cap(s: str, n: int) -> str:
        return s if len(s) <= n else s[:n] + "…"

    step_lines = []
    for i, st in enumerate(raw_steps, 1):
        line = f"{i}. {_cap(st.get('action', ''), 200)}"
        if st.get("observation"):
            line += f" → {_cap(st['observation'], 400)}"
        if st.get("error"):
            line += f"（出错：{_cap(st['error'], 200)}）"
        step_lines.append(line)
    n = len(step_lines)
    anchors = set(range(min(3, n))) | set(range(max(0, n - 3), n))
    must = anchors | {i for i, st in enumerate(raw_steps) if st.get("error")}

    def render(idxs: list[int]) -> str:
        out = list(header)
        prev = -1
        for i in idxs:
            if prev >= 0 and i > prev + 1:
                out.append(f"……（省略第 {prev + 2}–{i} 步，共 {i - prev - 1} 步）……")
            out.append(step_lines[i])
            prev = i
        if idxs and idxs[0] > 0:
            out.insert(len(header), f"……（省略第 1–{idxs[0]} 步，共 {idxs[0]} 步）……")
        return "\n".join(out)

    kept = sorted(must)
    if len(render(kept)) > MAX_RENDER_CHARS:
        # 连必留步都装不下（大量出错步）：首尾锚点不丢，出错步按原序装到装满为止
        kept = sorted(anchors)
        for i in sorted(must - anchors):
            if len(render(sorted(kept + [i]))) <= MAX_RENDER_CHARS:
                kept.append(i)
        kept.sort()
    else:
        for i in range(n):
            if i not in kept and len(render(sorted(kept + [i]))) <= MAX_RENDER_CHARS:
                kept.append(i)
        kept.sort()
    return redact(render(kept))


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
    # 按生效时间倒序：rows[0] 必须是最新一行（_active_content 的语义依赖于此，旧版无排序是任意序）
    return list(db.scalars(select(MemoryInsight).where(MemoryInsight.id.in_(ids))
                           .order_by(MemoryInsight.valid_from.desc())))


def _active_content(rows: list[MemoryInsight], new_content: str = "") -> str | None:
    """同主题现行内容：优先与新内容规范化相等的行（decide 的 NOOP 判定需要它），
    否则取最新一行（rows 已按 valid_from 倒序）。与 worker._active_value 同一套语义。"""
    if not rows:
        return None
    if new_content:
        for r in rows:
            if norm(r.content) == norm(new_content):
                return r.content
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
                   embedder: GeminiEmbedder | None,
                   project: str | None = None) -> tuple[str, str | None]:
    """按 decide() 裁决写入一条经验，返回 (decision, insight_id)。"""
    matched = _matched_insights(db, u.app_id, u.user_id, cand.subject)
    same = next((r for r in matched if norm(r.content) == norm(cand.content)), None)
    existing = _active_content(matched, cand.content)
    decision = decide(existing, cand.content)
    if decision == "NOOP":
        return "noop", (same.id if same is not None else (matched[0].id if matched else None))
    now_version = max((r.version for r in matched), default=0) + 1
    # 反馈声誉属于这条经验的血统，不属于某一行：新版本继承被关闭各行的计数与置信度，
    # 否则每次内容更新都把攒下的信任清零，反馈闭环就白建了（第十轮）。
    inh_pos = sum(r.feedback_pos or 0 for r in matched)
    inh_neg = sum(r.feedback_neg or 0 for r in matched)
    inh_use = sum(r.use_count or 0 for r in matched)
    inh_succ = sum(r.success_count or 0 for r in matched)
    if project is None and matched:
        project = matched[0].project
    for row in matched:
        if row is not same:
            row.valid_to = _now()
    row = MemoryInsight(app_id=u.app_id, user_id=u.user_id, kind=cand.kind,
                        subject=cand.subject, content=cand.content,
                        applies_when=cand.applies_when, version=now_version,
                        source_episode_id=episode_id, project=project,
                        confidence=_confidence(inh_pos, inh_neg),
                        use_count=inh_use, success_count=inh_succ,
                        feedback_pos=inh_pos, feedback_neg=inh_neg)
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
    dropped_gate = 0
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
            dropped_gate += 1
            log.warning("insight dropped by injection gate episode=%s subject=%s", ep.id, subject[:40])
            continue
        decision, iid = _apply_insight(db, u, _Cand(kind, subject, content, applies_when), ep.id,
                                       embedder, project=ep.project)
        decisions.append(decision)
        if iid:
            insight_ids.append(iid)
    ep.distill_status = "done"
    ep.locked_until = None
    ep.last_error = None
    # 审计口径：候选全被注入闸门拦下记 blocked，与「真无产出」的 empty 分开，复盘时看得出是被拦了
    summary = ("update" if "update" in decisions else
               "add" if "add" in decisions else
               "noop" if decisions else
               "blocked" if dropped_gate else "empty")
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


def _confidence(pos: int, neg: int) -> float:
    """置信度 = 反馈的拉普拉斯平滑胜率 (1+pos)/(2+pos+neg)：无反馈 0.5（中性先验），
    证据越多越往两端走。第十一轮的衰减在此值上做，不改计数本身。"""
    return (1 + pos) / (2 + pos + neg)


def record_feedback(db: Session, u: UserContext, insight_id: str, *,
                    helpful: bool, task_outcome: str | None = None) -> MemoryInsight:
    """反馈闭环的写入端（第十轮）：任务结束后回报「这条经验用了、有没有用、任务成没成」。

    只认本人名下的经验（跨用户/不存在一律 ValueError，REST 转 404，不泄露存在性）；
    task_outcome='success' 时记一次成功关联。置信度当场按 _confidence 重算并落库，
    简报排序直接用它，不必每次现算。调用方负责 commit。"""
    row = db.scalar(select(MemoryInsight).where(
        MemoryInsight.id == insight_id, MemoryInsight.app_id == u.app_id,
        MemoryInsight.user_id == u.user_id))
    if row is None:
        raise ValueError("insight not found")
    if helpful:
        row.feedback_pos = (row.feedback_pos or 0) + 1
    else:
        row.feedback_neg = (row.feedback_neg or 0) + 1
    if task_outcome == "success":
        row.success_count = (row.success_count or 0) + 1
    row.confidence = _confidence(row.feedback_pos or 0, row.feedback_neg or 0)
    db.add(AuditLog(app_id=u.app_id, user_id=u.user_id, action="feedback",
                    fact_ids=[row.id]))
    db.flush()
    return row


def brief_for_task(db: Session, u: UserContext, task: str, k: int = 5,
                   embedder: GeminiEmbedder | None = None, token_budget: int = 1500,
                   project: str | None = None) -> list[InsightOut]:
    """情境召回：新任务开始前，按任务描述捞出适用的历史经验。

    排序与门槛完全沿用事实混合召回（0.7 向量 + 0.3 关键词；入选需关键词命中
    或余弦过 COS_MIN 标定线），匹配面是 主题+指导+适用情境。
    无向量时退化纯关键词，老数据零成本兼容。
    COS_MIN 复校（2026-10-23，经验形文本，真 Gemini 向量）：6 条经验 × 8 个任务，
    命中对余弦 0.670–0.785、未命中对 0.488–0.656，0.66 在经验文本上同样完成分离
    （但间隔只有约 0.014，样本再扩大时可能要再动——结论先沿用事实侧的标定线）。
    第八轮补：token 预算裁剪与事实召回对齐（此前只有条数上限，长经验能把简报撑爆）；
    向量须维度/模型对得上才参与打分（vector_usable），对不上按无向量处理。
    第十轮补：门槛仍只看原始相关性（标定不被污染），排序分再乘两个乘子——
    置信度乘子 0.7+0.6×confidence（反馈攒出的信任，0.7–1.3），项目乘子
    （同项目 1.15、跨项目 0.85、通用经验 1.0；跨项目只是降权不排除，通用经验不该被埋没）。
    递出的经验记 use_count/last_used_at（曝光计数；「真用了且有效」由反馈闭环回流）。
    """
    k = max(1, int(k))  # 服务层兜底：负数 k 会让切片变成「除最后 N 条外全返回」
    if not token_budget or token_budget <= 0:
        token_budget = 1500
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
        if qvec is not None and vector_usable(row.embed_model, row.embedding, qvec,
                                              getattr(embedder, "model", None)):
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
        conf = row.confidence if row.confidence is not None else 0.5
        final *= 0.7 + 0.6 * conf
        if project:
            if row.project == project:
                final *= 1.15
            elif row.project:
                final *= 0.85
        scored.append((final, row, reason))
    scored.sort(key=lambda t: t[0], reverse=True)
    out: list[InsightOut] = []
    used = 0
    for score, row, reason in scored:
        if len(out) >= k:
            break
        cost = _est_tokens(row.content)
        if used + cost > token_budget:
            break
        used += cost
        row.use_count = (row.use_count or 0) + 1
        row.last_used_at = _now()
        out.append(InsightOut(insight_id=row.id, kind=row.kind, subject=row.subject,
                              content=row.content, applies_when=row.applies_when,
                              score=round(score, 4), reason=reason,
                              confidence=round(row.confidence, 4)
                              if row.confidence is not None else 0.5,
                              project=row.project, source_episode_id=row.source_episode_id))
    # 曝光计数当场提交：brief 从第十轮起是「读 + 记账」，若只 flush 不 commit，
    # 调用方会话会一直攥着写事务——REST 与直调在同一进程交替出现时能把库锁死
    # （SQLite 实测 database is locked；PG 上则是行锁干等）。简报的账自己结清，
    # 调用方随后的审计 commit 不受影响。
    db.commit()
    return out
