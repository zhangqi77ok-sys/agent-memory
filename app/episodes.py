"""经验记忆的服务层：过程提交 → 复盘提炼 → 情境召回。

与事实管线平行但原料不同：事实来自对话文本，经验来自任务轨迹。
入库纪律与事实一致：脱敏在前、注入闸门在后、decide() 裁决去重/更新、
时间失效不真删、向量由 worker/提炼侧补齐且失败只留空不阻塞。
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import UserContext
from app.distill import KINDS
from app.embeddings import GeminiEmbedder, cosine
from app.extract import MAX_SUBJECT_LEN, InvalidExtraction, decide, is_injection, norm, same_subject, strip_injection
from app.links import contradiction_pairs, migrate_links, spread_from, weave_on_write
from app.models import (
    AuditLog,
    BriefDelivery,
    MemoryInsight,
    MemoryInsightToken,
    MemoryLink,
    TaskEpisode,
    _now,
)
from app.recall import (
    _candidates,
    _est_tokens,
    _query_vector,
    _QueryPlan,
    _rank_insights,
    _score_rows,
)
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
    # 结果回流（第十六轮）：这次任务的成败，结给开工前简报递出的那批经验。
    # 回流失败不能拖垮提交本身——episode 已经落库，账记不上只是少一次信号。
    try:
        backflow_for_episode(db, ep)
    except Exception as e:
        db.rollback()
        log.warning("outcome backflow failed episode=%s: %s", ep.id, e)
    return ep


BACKFLOW_WINDOW_DAYS = 7  # 简报→任务→提交的合理间隔；超窗的交付视为没赶上这次任务


def _successor_of(db: Session, row: MemoryInsight) -> MemoryInsight | None:
    """已关行的经验找它的现行版本：同用户同主题、版本号更高的现行行（取最新）。
    回流时交付里记的可能是旧版本 id，功过要记到活着的那一行头上。"""
    return db.scalars(select(MemoryInsight).where(
        MemoryInsight.app_id == row.app_id, MemoryInsight.user_id == row.user_id,
        MemoryInsight.subject == row.subject, MemoryInsight.version > row.version,
        MemoryInsight.valid_to.is_(None)).order_by(MemoryInsight.valid_from.desc())).first()


def backflow_for_episode(db: Session, ep: TaskEpisode) -> dict:
    """自动结果回流（第十六轮）：把 episode 的成败记回此前简报递出的经验。

    挂钩是 brief_delivery：同用户、项目相容（相等或任一为空）、交付在任务提交前
    BACKFLOW_WINDOW_DAYS 内、尚未消费。每份交付只结一次账（consumed_at）。
    记账口径：applied_count +1（这才叫「用上了」，与曝光 use_count 分开）、
    last_used_at 刷新（闲置衰减从此重算）；成败进 auto_pos/auto_neg，
    置信度按半权并入（任务成败不全赖经验）。自己 commit，返回统计。"""
    ep_created = _aware(ep.created_at)
    deliveries = list(db.scalars(select(BriefDelivery).where(
        BriefDelivery.app_id == ep.app_id, BriefDelivery.user_id == ep.user_id,
        BriefDelivery.consumed_at.is_(None),
        BriefDelivery.created_at >= ep_created - timedelta(days=BACKFLOW_WINDOW_DAYS),
        BriefDelivery.created_at <= ep_created + timedelta(hours=1))))
    stats = {"deliveries": 0, "credited": 0}
    now = _now()
    for d in deliveries:
        if d.project and ep.project and d.project != ep.project:
            continue  # 项目明确不同：这份简报不是为这个任务递的（交付留待别的任务结账）
        d.consumed_at = now
        stats["deliveries"] += 1
        for iid in d.insight_ids or []:
            row = db.get(MemoryInsight, iid)
            if row is None or row.app_id != ep.app_id or row.user_id != ep.user_id:
                continue
            if row.valid_to is not None:
                row = _successor_of(db, row)
                if row is None:
                    continue
            row.applied_count = (row.applied_count or 0) + 1
            row.last_used_at = now
            if ep.outcome == "success":
                row.auto_pos = (row.auto_pos or 0) + 1
            elif ep.outcome == "failure":
                row.auto_neg = (row.auto_neg or 0) + 1
            row.confidence = _confidence(row.feedback_pos or 0, row.feedback_neg or 0,
                                          row.auto_pos or 0, row.auto_neg or 0)
            stats["credited"] += 1
    if stats["deliveries"]:
        db.add(AuditLog(app_id=ep.app_id, user_id=ep.user_id, action="backflow",
                        fact_ids=[ep.id], decision=f"credited={stats['credited']}"))
    db.commit()
    return stats


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
    # 第十六轮：自动回流信号与应用计数同属血统，一并继承
    inh_applied = sum(r.applied_count or 0 for r in matched)
    inh_auto_pos = sum(r.auto_pos or 0 for r in matched)
    inh_auto_neg = sum(r.auto_neg or 0 for r in matched)
    if project is None and matched:
        project = matched[0].project
    closed_ids = []
    for row in matched:
        if row is not same:
            row.valid_to = _now()
            closed_ids.append(row.id)
    row = MemoryInsight(app_id=u.app_id, user_id=u.user_id, kind=cand.kind,
                        subject=cand.subject, content=cand.content,
                        applies_when=cand.applies_when, version=now_version,
                        source_episode_id=episode_id, project=project,
                        confidence=_confidence(inh_pos, inh_neg, inh_auto_pos, inh_auto_neg),
                        use_count=inh_use, success_count=inh_succ,
                        feedback_pos=inh_pos, feedback_neg=inh_neg,
                        applied_count=inh_applied, auto_pos=inh_auto_pos,
                        auto_neg=inh_auto_neg,
                        verified=bool(getattr(cand, "verified", True)))
    db.add(row)
    db.flush()
    _embed_insight(embedder, row)
    # 织网（第十五轮）：被关旧版本的关联边迁到新版本（矛盾边在迁移里关掉）
    for old_id in closed_ids:
        migrate_links(db, u.app_id, u.user_id, "insight", old_id, row.id)
    return decision.lower(), row.id


def _lock_user(db: Session, app_id: str, user_id: str) -> None:
    """同一用户的经验写入串行化，与 worker 对事实的 _lock_user 同一把咨询锁（同键）：
    两个 Worker 并发提炼同一用户的不同 episode 时，_apply_insight 的读-改-写不能交错，
    否则会写出双 active 经验。SQLite 单写者天然串行，跳过。"""
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k)::bigint)"),
                   {"k": f"{app_id}:{user_id}"})


def distill_episode(db: Session, ep: TaskEpisode, distiller, embedder: GeminiEmbedder | None = None,
                    linker=None) -> str:
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
    unverified = 0
    for cand in candidates:
        kind = getattr(cand, "kind", "")
        subject = redact(str(getattr(cand, "subject", "") or "")).strip()
        content = redact(str(getattr(cand, "content", "") or "")).strip()
        applies_when = redact(str(getattr(cand, "applies_when", "") or "")).strip()
        evidence = redact(str(getattr(cand, "evidence", "") or "")).strip()
        if kind not in KINDS or not subject or not content:
            continue
        if len(subject) > MAX_SUBJECT_LEN:
            continue  # 兜底：绕过解析层直达这里的候选同样不能把整批拖死在 PG 长度校验上
        if is_injection(subject) or is_injection(content) or is_injection(applies_when):
            dropped_gate += 1
            log.warning("insight dropped by injection gate episode=%s subject=%s", ep.id, subject[:40])
            continue
        # 证据校验（第十六轮 verifier）：提炼声称的经验必须能在轨迹原文里指出处。
        # 验不过不丢弃——标 verified=False 入库，简报排序打折、面板上看得见；
        # 直接丢弃会把「引文摘抄不规范但经验是真的」一并误杀，降权更诚实。
        verified = _evidence_ok(evidence, text)
        if not verified:
            unverified += 1
            log.warning("insight unverified episode=%s subject=%s", ep.id, subject[:40])
        decision, iid = _apply_insight(
            db, u, _Cand(kind, subject, content, applies_when, verified=verified),
            ep.id, embedder, project=ep.project)
        decisions.append(decision)
        if iid:
            insight_ids.append(iid)
            if decision in ("add", "update"):
                # 织网（第十五轮）：新经验落库即连边，与已有经验建立关联
                new_row = db.get(MemoryInsight, iid)
                if new_row is not None:
                    weave_on_write(db, u.app_id, u.user_id, new_row, linker=linker)
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
    __slots__ = ("kind", "subject", "content", "applies_when", "verified")

    def __init__(self, kind: str, subject: str, content: str, applies_when: str,
                 verified: bool = True):
        self.kind, self.subject, self.content, self.applies_when = kind, subject, content, applies_when
        self.verified = verified


MIN_EVIDENCE_LEN = 8  # 引文太短（如「报错」二字）证明不了什么，按没证据处理


def _evidence_ok(evidence: str, rendered: str) -> bool:
    """证据校验：引文规范化（折叠空白、忽略大小写）后必须是轨迹原文的子串。

    核对对象就是提炼器实际看到的那份渲染文本——它声称摘自原文，
    就必须在原文里找得到；模型把经验改写成自己的话再当引文，验不过。"""
    if not evidence:
        return False
    ev = norm(evidence)
    if len(ev) < MIN_EVIDENCE_LEN:
        return False
    return ev in norm(rendered)


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


def run_episode_once(db: Session, distiller, embedder: GeminiEmbedder | None = None,
                     linker=None) -> TaskEpisode | None:
    """worker 的一轮经验提炼。与 run_once 同构：坏输出整批丢弃并记 distill=discard；
    瞬时故障退回 pending 重试，超过上限标 failed 并留错误，不无限循环。"""
    ep = claim_episode(db)
    if ep is None:
        return None
    try:
        distill_episode(db, ep, distiller, embedder, linker=linker)
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


def _confidence(pos: int, neg: int, auto_pos: int = 0, auto_neg: int = 0) -> float:
    """置信度 = 反馈的拉普拉斯平滑胜率 (1+pos)/(2+pos+neg)：无反馈 0.5（中性先验），
    证据越多越往两端走。第十一轮的衰减在此值上做，不改计数本身。
    第十六轮：自动回流信号（auto_*）按半权并入——任务成败受很多因素影响，
    系统推断的功过只值人工反馈的一半。"""
    ap, an = 0.5 * auto_pos, 0.5 * auto_neg
    return (1 + pos + ap) / (2 + pos + neg + ap + an)


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
    # 第十六轮：人工反馈意味着这条经验真被用过——刷新 last_used_at（简报曝光不再刷，
    # 见 brief_for_task），置信度重算时把自动回流信号按半权一并计入。
    row.last_used_at = _now()
    row.confidence = _confidence(row.feedback_pos or 0, row.feedback_neg or 0,
                                  row.auto_pos or 0, row.auto_neg or 0)
    db.add(AuditLog(app_id=u.app_id, user_id=u.user_id, action="feedback",
                    fact_ids=[row.id]))
    db.flush()
    return row


DECAY_GRACE_DAYS = 30      # 闲置宽限：最后活跃 30 天内置信度不动
DECAY_FACTOR = 0.95        # 宽限后每满 30 天置信度乘一次
CONFIDENCE_FLOOR = 0.1     # 衰减下限：再老也不归零（证据还在，只是久未验证）
MERGE_COSINE = 0.95        # 向量近似到这个程度且同主题，视为同一条经验
RETIRE_IDLE_DAYS = 180     # 真遗忘线（第十六轮）：闲置超半年、从未被应用、从未有过正面证据
ADJUDICATE_GAP = 0.30      # 矛盾裁决门槛：置信度差距至少这么大才动手
ADJUDICATE_MIN_EVIDENCE = 2  # 且胜方至少攒了这么多条证据（人工+自动），防先验互相残杀


def _aware(dt):
    """SQLite 读回的 datetime 可能丢 tzinfo；统一补 UTC 再参与运算，免得 aware/naive 相减报错。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _pair_cosine(a: MemoryInsight, b: MemoryInsight) -> float | None:
    if a.embedding is None or b.embedding is None or a.embed_model != b.embed_model:
        return None
    va = [float(x) for x in a.embedding]
    vb = [float(x) for x in b.embedding]
    if len(va) != len(vb):
        return None
    return cosine(va, vb)


def consolidate_insights(db: Session, now: datetime | None = None) -> dict:
    """经验生命周期巡检（第十一轮）：衰减、合并、矛盾标记。Worker 每小时跑一轮。

    - 衰减：以 last_used_at（无则 valid_from）为最后活跃时间，闲置超过宽限期后
      每满 30 天置信度 ×0.95，下限 0.1。只压置信度、不动反馈计数——新反馈进来时
      record_feedback 按计数重算，等于用新证据覆盖衰减，这正是想要的语义。
    - 合并：同用户内 same_subject 且（内容规范化相同，或向量余弦 ≥ 0.95）的现行
      经验对，保留置信度高者（并列取更新的），另一行关行、计数并入保留行、留审计。
    - 矛盾：合并后仍同主题共存、内容不同的现行对只计数上报；有 contradicts 边
      （第十五轮织网）且证据悬殊的对在下方裁决环节处理。
    - 裁决（第十六轮）：被 contradicts 边连着的经验对，置信度差 ≥ 0.30 且胜方
      证据（人工+自动）≥ 2 条时，负方关行、边关行——矛盾不再无限期并存。
      置顶的经验是用户亲手策展的，永不自动裁掉。
    - 真遗忘（第十六轮）：闲置超 180 天、从未被应用（applied_count=0）、从未有
      正面证据（人工好评与自动成功皆无）的经验关行退出召回——衰减下限 0.1 还留
      约 58% 排序权重，那不叫遗忘，叫赖着不走。关行不真删，面板与导出仍可见。
    返回 {"decayed": n, "merged": n, "conflicts": n, "adjudicated": n, "retired": n}，
    自己 commit。"""
    now = now or _now()
    rows = list(db.scalars(select(MemoryInsight).where(MemoryInsight.valid_to.is_(None))))
    decayed = 0
    retired = 0
    for row in rows:
        conf = row.confidence if row.confidence is not None else 0.5
        idle = (now - _aware(row.last_used_at or row.valid_from)).days
        if idle > DECAY_GRACE_DAYS:
            blocks = (idle - 1) // DECAY_GRACE_DAYS
            new_conf = max(CONFIDENCE_FLOOR, conf * (DECAY_FACTOR ** blocks))
            if new_conf < conf - 1e-9:
                row.confidence = round(new_conf, 4)
                decayed += 1
        if (idle > RETIRE_IDLE_DAYS and not row.pinned
                and not (row.applied_count or 0) and not (row.success_count or 0)
                and not (row.feedback_pos or 0) and not (row.auto_pos or 0)):
            row.valid_to = now
            retired += 1
            db.add(AuditLog(app_id=row.app_id, user_id=row.user_id, action="consolidate",
                            fact_ids=[row.id], decision="retire"))
    groups: dict[tuple[str, str], list[MemoryInsight]] = {}
    for row in rows:
        groups.setdefault((row.app_id, row.user_id), []).append(row)
    merged = 0
    conflict_pairs = 0
    for (app_id, user_id), grows in groups.items():
        for i in range(len(grows)):
            for j in range(i + 1, len(grows)):
                a, b = grows[i], grows[j]
                if a.valid_to is not None or b.valid_to is not None:
                    continue
                if not same_subject(a.subject, b.subject):
                    continue
                pc = _pair_cosine(a, b)
                if norm(a.content) == norm(b.content) or (pc is not None and pc >= MERGE_COSINE):
                    keeper, loser = (a, b) if ((a.confidence or 0.5), a.valid_from) >= \
                        ((b.confidence or 0.5), b.valid_from) else (b, a)
                    loser.valid_to = now
                    keeper.use_count = (keeper.use_count or 0) + (loser.use_count or 0)
                    keeper.success_count = (keeper.success_count or 0) + (loser.success_count or 0)
                    keeper.feedback_pos = (keeper.feedback_pos or 0) + (loser.feedback_pos or 0)
                    keeper.feedback_neg = (keeper.feedback_neg or 0) + (loser.feedback_neg or 0)
                    keeper.applied_count = (keeper.applied_count or 0) + (loser.applied_count or 0)
                    keeper.auto_pos = (keeper.auto_pos or 0) + (loser.auto_pos or 0)
                    keeper.auto_neg = (keeper.auto_neg or 0) + (loser.auto_neg or 0)
                    keeper.confidence = _confidence(keeper.feedback_pos, keeper.feedback_neg,
                                                    keeper.auto_pos, keeper.auto_neg)
                    db.add(AuditLog(app_id=app_id, user_id=user_id, action="consolidate",
                                    fact_ids=[keeper.id, loser.id], decision="merge"))
                    merged += 1
                else:
                    conflict_pairs += 1
    # 矛盾裁决（第十六轮）：只认织网留下的 contradicts 边——那是判定器/规则
    # 明确表过态的矛盾；同主题共存但没有边的（见上方 conflicts 计数）仍只上报。
    adjudicated = 0
    by_id = {r.id: r for r in rows}
    links = list(db.scalars(select(MemoryLink).where(
        MemoryLink.relation == "contradicts", MemoryLink.valid_to.is_(None),
        MemoryLink.src_kind == "insight", MemoryLink.dst_kind == "insight")))
    for link in links:
        a, b = by_id.get(link.src_id), by_id.get(link.dst_id)
        if a is None or b is None or a.valid_to is not None or b.valid_to is not None:
            continue
        winner, loser = (a, b) if (a.confidence or 0.5) >= (b.confidence or 0.5) else (b, a)
        gap = abs((a.confidence or 0.5) - (b.confidence or 0.5))
        evidence = ((winner.feedback_pos or 0) + (winner.feedback_neg or 0)
                    + (winner.auto_pos or 0) + (winner.auto_neg or 0))
        if gap < ADJUDICATE_GAP or evidence < ADJUDICATE_MIN_EVIDENCE or loser.pinned:
            continue
        loser.valid_to = now
        link.valid_to = now
        adjudicated += 1
        db.add(AuditLog(app_id=loser.app_id, user_id=loser.user_id, action="consolidate",
                        fact_ids=[winner.id, loser.id], decision="adjudicate"))
    db.commit()
    return {"decayed": decayed, "merged": merged, "conflicts": conflict_pairs,
            "adjudicated": adjudicated, "retired": retired}


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
    递出的经验记 use_count（曝光计数；第十六轮起不再刷新 last_used_at），
    并落一条 brief_delivery 供结果回流结账；「真用了且有效」由回流与反馈闭环记。
    第十一轮补：排序三要素齐了——相关性（门槛+基础分）× 置信度 × 新近性；
    用户置顶的经验必达（垫底分入选、reason='pinned'），禁用的永不召回。
    第十四轮补：候选改走索引化召回（与事实侧同口径，见 app/recall.py）——
    查询只分词一次、逐行改存在性打分、轻量列加载；通道完备时（无向量，或 PG
    向量通道在场）先在 SQL 侧压候选：关键词门槛计数 ∪ 置顶一个分支，向量距离
    单独一个 UNION 分支（混在 OR 里 HNSW 索引用不上），压不了的情形保全量。
    门槛、乘子、排序与记账语义不变，赢家再回表取 ORM 行记账。
    """
    k = max(1, int(k))  # 服务层兜底：负数 k 会让切片变成「除最后 N 条外全返回」
    if not token_budget or token_budget <= 0:
        token_budget = 1500
    plan = _QueryPlan(task)
    qvec = _query_vector(embedder, task)
    # 无查询向量时不搬 embedding 列：SQLite 上它是 JSON，每行都要解码，纯属浪费
    cols = [MemoryInsight.id, MemoryInsight.kind, MemoryInsight.subject,
            MemoryInsight.content, MemoryInsight.applies_when,
            MemoryInsight.confidence, MemoryInsight.project,
            MemoryInsight.pinned, MemoryInsight.valid_from,
            MemoryInsight.last_used_at, MemoryInsight.source_episode_id,
            MemoryInsight.verified]
    if qvec is not None:
        cols += [MemoryInsight.embedding, MemoryInsight.embed_model]
    scope = (MemoryInsight.app_id == u.app_id, MemoryInsight.user_id == u.user_id,
             MemoryInsight.valid_to.is_(None),
             # 第十一轮：用户禁用的经验永不召回（第十轮前建的存量行 muted 可能为 NULL，一并兼容）
             or_(MemoryInsight.muted.is_(False), MemoryInsight.muted.is_(None)))
    # 候选与打分走 recall 的共享实现（与事实侧同一套通道/门槛/融合口径）
    _ch = _candidates(db, MemoryInsight, MemoryInsightToken, "insight_id", cols,
                      scope, plan, qvec, embedder, with_pinned=True)
    rows = _ch[0] if _ch is not None else []
    if not rows:
        db.commit()  # 与旧行为对齐：简报顺带结清会话（见下方曝光计数注释）
        return []
    now = _now()
    _scored = _score_rows(rows, plan, qvec, getattr(embedder, "model", None),
                          pinned_of=lambda r: bool(r.pinned),
                          fields_of=lambda r: f"{r.subject} {r.content} {r.applies_when}")
    scored = _rank_insights(rows, _scored, now, project)
    winners: list[tuple[float, object, str]] = []
    used = 0
    for score, row, reason in scored:
        if len(winners) >= k:
            break
        cost = _est_tokens(row.content)
        if used + cost > token_budget:
            break
        used += cost
        winners.append((score, row, reason))
    # 扩散激活（第十五轮「织网」）：直接命中没占满名额时，沿关联边一跳把强相关
    # 经验带出来（reason='linked'、linked_from 标明来历，分数按边强度打折）——
    # 捞 A 时带出与 A 相连的 B，哪怕 B 与查询字面不像。矛盾边不扩散，只在下方标记。
    linked_from: dict[str, str] = {}
    if len(winners) < k:
        have = {row.id for _, row, _ in winners}
        spread = spread_from(db, u.app_id, u.user_id,
                             [("insight", row.id, score) for score, row, _ in winners],
                             "insight")
        cand_ids = [i for i in spread if i not in have]
        if cand_ids:
            extra = {r.id: r for r in db.scalars(select(MemoryInsight).where(
                MemoryInsight.id.in_(cand_ids),
                MemoryInsight.app_id == u.app_id, MemoryInsight.user_id == u.user_id,
                MemoryInsight.valid_to.is_(None),
                or_(MemoryInsight.muted.is_(False), MemoryInsight.muted.is_(None))))}
            for nid, (dscore, via) in sorted(spread.items(), key=lambda kv: kv[1][0],
                                             reverse=True):
                if len(winners) >= k:
                    break
                r = extra.get(nid)
                if r is None:
                    continue
                cost = _est_tokens(r.content)
                if used + cost > token_budget:
                    continue
                used += cost
                winners.append((dscore, r, "linked"))
                linked_from[nid] = via
    # 矛盾标记（第十五轮）：同份简报里若有经验对被 contradicts 边连着，双方都标出来，
    # 不再像从前那样两条相反指导一声不吭地并排递给 agent。
    conflicts = contradiction_pairs(db, u.app_id, u.user_id,
                                    [row.id for _, row, _ in winners])
    # 记账要 ORM 行：候选阶段只搬了轻量列，赢家（≤k 条）再按 id 回表取行
    orm_by_id = {}
    if winners:
        orm_by_id = {r.id: r for r in db.scalars(select(MemoryInsight).where(
            MemoryInsight.id.in_([row.id for _, row, _ in winners])))}
    out: list[InsightOut] = []
    for score, row, reason in winners:
        orm = orm_by_id.get(row.id)
        if orm is not None:
            # 第十六轮起曝光只记 use_count（曝光数），不再刷新 last_used_at：
            # 递出去 ≠ 用上了，反复曝光反复续命正是审查点名的马太效应回路；
            # last_used_at 只由应用回流与人工反馈刷新，衰减/遗忘才量得到真闲置。
            orm.use_count = (orm.use_count or 0) + 1
        out.append(InsightOut(insight_id=row.id, kind=row.kind, subject=row.subject,
                              content=row.content, applies_when=row.applies_when,
                              score=round(score, 4), reason=reason,
                              confidence=round(row.confidence, 4)
                              if row.confidence is not None else 0.5,
                              project=row.project, source_episode_id=row.source_episode_id,
                              conflicts_with=conflicts.get(row.id, []),
                              linked_from=linked_from.get(row.id),
                              verified=True if row.verified is None else bool(row.verified)))
    if winners:
        # 交付记录（第十六轮）：结果回流靠它把后续任务的成败结给这批经验
        db.add(BriefDelivery(app_id=u.app_id, user_id=u.user_id, project=project,
                             task=task[:500],
                             insight_ids=[row.id for _, row, _ in winners]))
    # 曝光计数当场提交：brief 从第十轮起是「读 + 记账」，若只 flush 不 commit，
    # 调用方会话会一直攥着写事务——REST 与直调在同一进程交替出现时能把库锁死
    # （SQLite 实测 database is locked；PG 上则是行锁干等）。简报的账自己结清，
    # 调用方随后的审计 commit 不受影响。
    db.commit()
    return out
