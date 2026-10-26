"""经验记忆的服务层：过程提交 → 复盘提炼 → 情境召回。

与事实管线平行但原料不同：事实来自对话文本，经验来自任务轨迹。
入库纪律与事实一致：脱敏在前、注入闸门在后、分桶裁决去重/更新、
时间失效不真删、向量由 worker/提炼侧补齐且失败只留空不阻塞。
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import UserContext
from app.distill import KINDS
from app.edge_learning import (
    find_spread_edge,
    strength_on_applied,
    strength_on_rejected,
)
from app.embeddings import GeminiEmbedder, cosine
from app.extract import MAX_SUBJECT_LEN, InvalidExtraction, is_injection, norm, same_subject, strip_injection
from app.links import (
    contradiction_pairs,
    create_link,
    migrate_links,
    spread_from,
    view_of,
    weave_on_write,
)
from app.models import (
    AuditLog,
    BriefDelivery,
    BriefDeliveryEdge,
    MemoryFact,
    MemoryInsight,
    MemoryInsightToken,
    MemoryLink,
    MemoryPreference,
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


VALID_OUTCOMES = ("success", "partial", "failure")


def validate_episode(title: str, outcome: str, steps: list[dict], cost: dict | None) -> None:
    """提交完整性校验（第十八轮）：此前服务层不拦，空标题、非法结果、空轨迹、
    无动作的步骤都能直接落库，提炼时才暴露成脏数据。REST 的 pydantic 只管
    HTTP 入口，MCP 与直调服务层走同一道闸——非法一律 ValueError，REST 转 400。"""
    if not title or not str(title).strip():
        raise ValueError("episode title 不能为空")
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"episode outcome 非法：{outcome!r}，只接受 success/partial/failure")
    if not isinstance(steps, list) or not steps:
        raise ValueError("episode steps 不能为空列表")
    for s in steps:
        if not isinstance(s, dict) or not s.get("action") or not str(s["action"]).strip():
            raise ValueError("episode 每个步骤都必须有非空 action")
        for k in ("observation", "error"):
            if s.get(k) is not None and not isinstance(s[k], str):
                raise ValueError(f"episode 步骤的 {k} 必须是字符串")
    if cost is None:
        return
    if not isinstance(cost, dict):
        raise ValueError("episode cost 必须是对象")
    for k in ("tokens", "retries"):
        v = cost.get(k)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"episode cost 的 {k} 必须是非负数")


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
    validate_episode(title, outcome, steps, cost)
    ep = TaskEpisode(app_id=u.app_id, user_id=u.user_id, source=source, title=redact(title),
                     outcome=outcome, steps=_redact_steps(steps), cost=_redact_cost(cost),
                     idempotency_key=idempotency_key, project=project,
                     salience=episode_salience(outcome, cost))
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


def episode_salience(outcome: str, cost: dict | None) -> float:
    """编码显著性（第十七轮）：一次任务在提炼队列里的优先级。

    结果权重为主：失败的经历最值得复盘（3），部分完成次之（2），成功最低（1）；
    成本加权为辅：烧的 token 越多、重试越多，越说明这条路走得贵——贵有贵的教训。
    纯函数、量纲固定，单测可钉死。"""
    base = {"failure": 3.0, "partial": 2.0, "success": 1.0}.get(outcome or "", 1.0)
    cost = cost or {}
    tokens = cost.get("tokens") or 0
    retries = cost.get("retries") or 0
    try:
        tokens = float(tokens)
    except (TypeError, ValueError):
        tokens = 0.0
    try:
        retries = float(retries)
    except (TypeError, ValueError):
        retries = 0.0
    return base + min(tokens / 20000.0, 1.0) + min(retries * 0.25, 0.5)


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
    stats = {"deliveries": 0, "credited": 0, "edges_updated": 0}
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
            # 第二十轮「边学习」：这条经验若是经某条边扩散进简报的，顺着
            # 交付边找到那条边，按任务成败改边权。partial 只记 applied、
            # 不改强度：用上了但成败未分，边权不背这个锅。边已关闭
            # （版本滚动迁边）时只记账，不改旧边。
            for erow in db.scalars(select(BriefDeliveryEdge).where(
                    BriefDeliveryEdge.delivery_id == d.id,
                    BriefDeliveryEdge.insight_id == iid,
                    BriefDeliveryEdge.applied.is_(False))):
                erow.applied = True
                link = db.get(MemoryLink, erow.link_id)
                if link is None or link.valid_to is not None:
                    continue
                if ep.outcome == "success":
                    link.strength = strength_on_applied(link.strength or 0.5)
                    stats["edges_updated"] += 1
                elif ep.outcome == "failure":
                    link.strength = strength_on_rejected(link.strength or 0.5)
                    stats["edges_updated"] += 1
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


def _render_episode_parts(ep: TaskEpisode) -> tuple[str, str, dict[int, str]]:
    """渲染轨迹并同时返回小节结构，供证据校验按节核对（第二十九轮）。

    返回 (全文, 题头文本, {步号: 渲染行})。全文与旧 _render_episode 输出
    字节一致（round9 测试钉住）；渲染行就是全文里的那一行（压行、
    省略感知：被省略的步不在字典里，省略标记行不是证据，不进字典）。
    脱敏模式均为单 token（手机号/邮箱/身份证/密钥），不跨行，因此逐节脱敏
    与整篇脱敏结果一致；校验时各节再各过一次 strip_injection，与提炼器
    看到的剥离后全文同口径。"""
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
        return (redact(full), redact("\n".join(header)),
                {i + 1: redact(line) for i, line in enumerate(step_lines)})
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
    return (redact(render(kept)), redact("\n".join(header)),
            {i + 1: redact(step_lines[i]) for i in kept})


def _render_episode(ep: TaskEpisode) -> str:
    """把轨迹渲染成给提炼器的文本；整体先过一遍 redact，步骤里夹带的 PII 不进 LLM。

    渲染结果不超过 MAX_RENDER_CHARS：短轨迹原样输出；长轨迹先把单步压到
    MAX_STEP_LINE_CHARS，再优先保留出错步与首尾各三步，其余按原序装到装满为止，
    被省略的区间用标记行标明步号范围与步数。"""
    return _render_episode_parts(ep)[0]


def _matched_insights(db: Session, app_id: str, user_id: str, subject: str,
                      project: str | None = None) -> list[MemoryInsight]:
    """与 worker 对事实的预筛同构：先轻量取 (id, subject, project)，same_subject 命中才载整行，
    避免每提炼一条经验就把该用户的全部经验拉进内存。

    修复第 6 轮（审计 Top 6，A-F02）：写路径按项目分桶——project 明确时只见同项目行与
    通用行（project is None），project 为 None 的通用写入只见通用行；跨项目行对写操作
    一律不可见（既不 NOOP 冒领、也不 UPDATE 关行）。默认 None 是 fail-closed：调用方
    忘记传参时匹配面最小。返回行仍按生效时间倒序（新行在前）。"""
    light = db.execute(
        select(MemoryInsight.id, MemoryInsight.subject, MemoryInsight.project).where(
            MemoryInsight.app_id == app_id, MemoryInsight.user_id == user_id,
            MemoryInsight.valid_to.is_(None))).all()
    ids = [row.id for row in light
           if same_subject(row.subject, subject)
           and (row.project is None or (project is not None and row.project == project))]
    if not ids:
        return []
    return list(db.scalars(select(MemoryInsight).where(MemoryInsight.id.in_(ids))
                           .order_by(MemoryInsight.valid_from.desc())))


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
                   project: str | None = None,
                   agent: str | None = None,
                   additive: bool = False) -> tuple[str, str | None]:
    """按分桶裁决写入一条经验，返回 (decision, insight_id)。

    修复第 6 轮（审计 Top 6，A-F02/A-F03）：匹配分两集——可见集 S（project 明确时
    同项目行 + 通用行，project 为 None 时仅通用行）只用于内容 NOOP 去重；
    血统集 L（作用域精确相等）才是关闭/继承/版本递增的集合。kind 不是分桶维：
    同一主题只有一条现行行是既定语义（评测门禁 c09 update_takes_new_kind：
    新 kind 接管旧行、version 递增），kind 是版本的属性、不是血统的维度——
    审计 A-F02 的“跨 kind 互不干扰”与该门禁冲突，本轮不采纳（见设计草稿 §4）。
    跨项目行对写操作一律不可见；项目写入绝不 supersede 通用行（无血统时 fork
    自己的行，不关通用行）；`project is None` 恒等于通用调用，永不从 matched
    行继承项目（A-F03 删除点）。

    additive（修复第 5 轮，审计 Top 5）：只新增不关闭模式，供原则起草
    点（综合/weave 簇抽象/睡眠簇抽象）使用。原则是新增节点，不是 matched
    行的新版本——旧行还活着时继承计数即双重记账，故新行从默认值起步，
    且不关行、不迁边；新建行一律返回 "add"（没有行被更新）。
    提炼写入走默认路径（版本滚动语义）不动，但滚动只在血统集内发生。
    """
    # 修复第 2 轮：内容墓碑检查放在匹配/NOOP 之前——旧任务重提炼已遗忘
    # 经验时，若只拦 ADD/UPDATE 不拦 NOOP，support 会被刷到重建的行上。
    # sleep/links/consolidate 的综合调用点走同一入口，一并收口。
    # 注意：墓碑 kind 按行类型（insight），不用候选 kind（lesson/pitfall/
    # strategy/principle）——同一内容换个 kind 重贴同样是复活。
    from app.tombstone import block_if_tombstoned

    if block_if_tombstoned(db, u.app_id, u.user_id, "insight", cand.subject, cand.content):
        return "tombstoned", None
    if not cand.content or not cand.content.strip():
        return "noop", None
    # 修复第 6 轮：可见集 S（分桶后的 matched）只用于内容去重；血统集 L
    #（作用域精确相等）才是关闭/继承/版本递增的集合。
    # NOOP 回答“这份内容是否已存在”，UPDATE 回答“谁是这条经验的上一版本”——
    # 两问分离：内容一字不差时不建重复行（kind 不影响去重），内容不同时的
    # 版本滚动只关同作用域的行（kind 是版本的属性，新 kind 接管旧行是既定语义，
    # 见评测门禁 c09）；跨项目行根本不在 S 里，故既不冒领也不被关。
    scope_rows = _matched_insights(db, u.app_id, u.user_id, cand.subject, project=project)
    same = next((r for r in scope_rows if norm(r.content) == norm(cand.content)), None)
    if same is not None:
        return "noop", same.id
    lineage = [r for r in scope_rows if r.project == project]
    if additive:
        # 修复第 5 轮：原则是新增节点——不关行（分化双方以条件区分共存，
        # 综合若关掉双方即自毁）、不迁边、不继承计数（旧行还活着，继承即
        # 双重记账）。修复第 6 轮（A-F03）：project 继承行已删除——通用原则
        # 起草（project=None）永远产出通用行，不再被某成员项目偷换。
        row = MemoryInsight(app_id=u.app_id, user_id=u.user_id, kind=cand.kind,
                            subject=cand.subject, content=cand.content,
                            applies_when=cand.applies_when,
                            source_episode_id=episode_id, project=project,
                            verified=bool(getattr(cand, "verified", True)),
                            agent=agent,
                            evidence_chained=bool(getattr(cand, "evidence_chained", False)))
        db.add(row)
        db.flush()
        _embed_insight(embedder, row)
        return "add", row.id
    if not lineage:
        # 可见但无血统：例如只有通用行（项目写入不得 supersede 通用指导，
        # 否则全体项目失去共享行——应 fork 自己的行）。
        # 此时没有行被更新，故返回 "add" 而非 "update"。
        row = MemoryInsight(app_id=u.app_id, user_id=u.user_id, kind=cand.kind,
                            subject=cand.subject, content=cand.content,
                            applies_when=cand.applies_when,
                            source_episode_id=episode_id, project=project,
                            verified=bool(getattr(cand, "verified", True)),
                            agent=agent,
                            evidence_chained=bool(getattr(cand, "evidence_chained", False)))
        db.add(row)
        db.flush()
        _embed_insight(embedder, row)
        return "add", row.id
    now_version = max((r.version for r in lineage), default=0) + 1
    # 反馈声誉属于这条经验的血统，不属于某一行：新版本继承被关闭各行的计数与置信度，
    # 否则每次内容更新都把攒下的信任清零，反馈闭环就白建了（第十轮）。
    # 修复第 6 轮：继承只在血统集 L 内求和，跨项目行的计数不再被吞入。
    inh_pos = sum(r.feedback_pos or 0 for r in lineage)
    inh_neg = sum(r.feedback_neg or 0 for r in lineage)
    inh_use = sum(r.use_count or 0 for r in lineage)
    inh_succ = sum(r.success_count or 0 for r in lineage)
    # 第十六轮：自动回流信号与应用计数同属血统，一并继承
    inh_applied = sum(r.applied_count or 0 for r in lineage)
    inh_auto_pos = sum(r.auto_pos or 0 for r in lineage)
    inh_auto_neg = sum(r.auto_neg or 0 for r in lineage)
    closed_ids = []
    for row in lineage:
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
                        verified=bool(getattr(cand, "verified", True)),
                        agent=agent,
                        evidence_chained=bool(getattr(cand, "evidence_chained", False)))
    db.add(row)
    db.flush()
    _embed_insight(embedder, row)
    # 织网（第十五轮）：被关旧版本的关联边迁到新版本（矛盾边在迁移里关掉）
    for old_id in closed_ids:
        migrate_links(db, u.app_id, u.user_id, "insight", old_id, row.id)
    return "update", row.id


def _lock_user(db: Session, app_id: str, user_id: str) -> None:
    """同一用户的经验写入串行化，与 worker 对事实的 _lock_user 同一把咨询锁（同键）：
    两个 Worker 并发提炼同一用户的不同 episode 时，_apply_insight 的读-改-写不能交错，
    否则会写出双 active 经验。SQLite 单写者天然串行，跳过。"""
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k)::bigint)"),
                   {"k": f"{app_id}:{user_id}"})


def _switch_off(db: Session, ep: TaskEpisode) -> bool:
    """记忆开关是否被显式关掉（修复第 4 轮）。函数级导入避免循环
    （worker.py:113 同款写法）。

    语义取舍（与巡检过滤同口径，见 fix4-design-draft.md §5）：只认
    “有设置行且未开启（含同意版本过期）”。无设置行的用户不过滤——
    提炼入口（REST submit）本就对无设置用户严格拒绝，worker 侧这道门
    只负责“已有设置被关掉”的在途变更；直接调服务层提炼的存量行为
    （含多轮测试直接造轨迹）不受影响。主动关过开关的用户一定有
    设置行，一定被拦住——审计关心的情形全覆盖。"""
    from app.api.me import get_setting
    from app.schemas import CONSENT_VERSION

    s = get_setting(db, UserContext(ep.app_id, ep.user_id))
    return s is not None and not (s.memory_enabled and s.consent_version == CONSENT_VERSION)


def distill_episode(db: Session, ep: TaskEpisode, distiller, embedder: GeminiEmbedder | None = None,
                    linker=None) -> str:
    """对一个 episode 做复盘提炼并入库，返回聚合裁决 update > add > noop（无产出为 empty）。

    提炼器可以是对象（.distill(text)）或可调用；单条经验被注入闸门拦下只丢弃该条，
    不像事实抽取那样整批作废——轨迹里混入的恶意文本不该让正当经验陪葬，
    闸门拦截本身留痕在日志里方便复盘。
    另两种终态（修复第 4 轮）：开关关闭为 skipped（整批回滚、集置 done），
    轨迹行中途被删为 missing（整批回滚、不提交）。
    """
    u = UserContext(ep.app_id, ep.user_id)
    _lock_user(db, ep.app_id, ep.user_id)
    # 修复第 2 轮：起手禁写判定——轨迹本身被遗忘过，或本集创建于用户
    # 最近一次删除之前（旧任务整单丢弃，见 worker 的同款水位规则）。
    # 命中则不产出任何经验；提交前还会再查一次，覆盖提炼中途被遗忘。
    from app.tombstone import _aware, is_episode_tombstoned, user_forget_watermark

    def _distill_blocked() -> bool:
        if is_episode_tombstoned(db, ep.app_id, ep.user_id, ep.title, ep.steps):
            return True
        wm = user_forget_watermark(db, ep.app_id, ep.user_id)
        return wm is not None and _aware(ep.created_at) is not None \
            and _aware(ep.created_at) < wm

    if _distill_blocked():
        ep.distill_status = "done"
        ep.locked_until = None
        ep.last_error = None
        db.add(AuditLog(app_id=ep.app_id, user_id=ep.user_id, action="distill",
                        fact_ids=[], decision="tombstoned"))
        db.commit()
        return "tombstoned"
    # 修复第 4 轮：提炼起手开关门——审计 F1a：此前提炼全程无开关检查，
    # 关闭后仍写经验。命中即消费掉该集（done + skipped 审计），不置回
    # pending：pending 会在开关持续关闭期间被反复认领、空耗认领次数
    # 或主循环空转；开关关闭是状态变更不是瞬时故障，消费 + 留痕与
    # 事实侧 skip 语义一致，轨迹行本身保留，日后可重提。
    if _switch_off(db, ep):
        ep.distill_status = "done"
        ep.locked_until = None
        ep.last_error = None
        db.add(AuditLog(app_id=ep.app_id, user_id=ep.user_id, action="distill",
                        fact_ids=[], decision="skipped"))
        db.commit()
        return "skipped"
    rendered, sec_header, sec_steps = _render_episode_parts(ep)
    text = strip_injection(rendered).strip()
    # 校验用小节与提炼器视野同口径：题头与各渲染行各过一次 strip_injection
    # （第二十九轮；旧版用原始步骤字段拼 step_texts，与渲染行标记、脱敏、
    # 压行省略三处都不对齐，见设计草稿）。
    sec_header = strip_injection(sec_header)
    sec_steps = {n: strip_injection(line) for n, line in sec_steps.items()}
    sections = [sec_header, *sec_steps.values()]
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
        # 证据校验（第十六轮 verifier + 第十九轮证据链 + 第二十九轮逐节验）：
        # 提炼声称的经验必须能在提炼器实际看到的那份渲染里指出处。报了步号
        # 且该步在渲染里的，逐字核对那一行——引文对但步号错（或步号对但引文
        # 不在这一行）就是假引用，verified 直接落 False；没报步号、步号非法、
        # 或步号指向被省略的步时，退回单节容纳：引文必须完整落在某一节
        # （题头或某步渲染行）里，跨步拼接不再靠全文子串蒙混。回退发现步号
        # 不等于链上，chained 只给声称且命中的那一路。验不过不丢弃——标
        # verified=False 入库，简报排序打折、面板上看得见；直接丢弃会把
        # 「引文摘抄不规范但经验是真的」一并误杀，降权更诚实。
        step_no = getattr(cand, "evidence_step", None)
        if isinstance(step_no, int) and step_no in sec_steps:
            chained = _evidence_ok(evidence, sec_steps[step_no])
            verified = chained
        else:
            chained = False
            verified = any(_evidence_ok(evidence, sec) for sec in sections)
        if not verified:
            unverified += 1
            log.warning("insight unverified episode=%s subject=%s", ep.id, subject[:40])
        decision, iid = _apply_insight(
            db, u, _Cand(kind, subject, content, applies_when, verified=verified,
                         evidence_chained=chained),
            ep.id, embedder, project=ep.project, agent=ep.source)
        decisions.append(decision)
        if iid:
            insight_ids.append(iid)
            if decision in ("add", "update"):
                # 织网（第十五轮）：新经验落库即连边，与已有经验建立关联
                new_row = db.get(MemoryInsight, iid)
                if new_row is not None:
                    weave_on_write(db, u.app_id, u.user_id, new_row, linker=linker)
    # 修复第 2 轮：提交前再查一次——提炼中途被遗忘时，整批候选回滚，
    # 不留半批经验；行已删则只返回，行还在则标记 done 并审计。
    if _distill_blocked():
        ep_id = ep.id  # rollback 会过期 ep 对象，先取主键
        db.rollback()
        live = db.get(TaskEpisode, ep_id)
        if live is not None:
            live.distill_status = "done"
            live.locked_until = None
            live.last_error = None
            db.add(AuditLog(app_id=live.app_id, user_id=live.user_id,
                            action="distill", fact_ids=[], decision="tombstoned"))
            db.commit()
        return "tombstoned"
    # 修复第 4 轮：提交前开关门——提炼慢调用窗口内关闭开关时整批回滚，
    # 不留半批经验；语义与起手门一致（done + skipped 审计）。
    if _switch_off(db, ep):
        ep_id = ep.id
        db.rollback()
        live = db.get(TaskEpisode, ep_id)
        if live is not None:
            live.distill_status = "done"
            live.locked_until = None
            live.last_error = None
            db.add(AuditLog(app_id=live.app_id, user_id=live.user_id,
                            action="distill", fact_ids=[], decision="skipped"))
            db.commit()
        return "skipped"
    # 修复第 4 轮：提交前存在性重查——轨迹行在提炼中途被删时，
    # 末尾对已删行 UPDATE 0 行即 StaleDataError（与事实侧同机理），
    # 故先查后提交；行已删则回滚整批并返回 missing。
    if db.scalar(select(TaskEpisode.id).where(TaskEpisode.id == ep.id)) is None:
        db.rollback()
        return "missing"
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
    __slots__ = ("kind", "subject", "content", "applies_when", "verified", "evidence_chained")

    def __init__(self, kind: str, subject: str, content: str, applies_when: str,
                 verified: bool = True, evidence_chained: bool = False):
        self.kind, self.subject, self.content, self.applies_when = kind, subject, content, applies_when
        self.verified = verified
        self.evidence_chained = evidence_chained


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


def skeletonize_episodes(db: Session, now: datetime | None = None,
                         retention_days: int = 90) -> int:
    """轨迹骨架化巡检（第十七轮）：提炼已完成、超过保留期的轨迹瘦身。

    中间步（非首、非尾、无错误）只留动作骨架：观察细节整段丢弃——细节的价值
    已经沉淀进提炼出的经验里，流水账原文不必永久全量保存。出错步与首尾步
    保全（错误是复盘的锚点，首尾是上下文的边界）。返回处理条数，自己 commit。"""
    if retention_days <= 0:
        return 0
    now = now or _now()
    cutoff = now - timedelta(days=retention_days)
    from app.api.me import switched_off_users

    off = switched_off_users(db)
    rows = [r for r in list(db.scalars(select(TaskEpisode).where(
        TaskEpisode.distill_status == "done",
        TaskEpisode.created_at < cutoff,
        or_(TaskEpisode.skeletonized.is_(False), TaskEpisode.skeletonized.is_(None)))))
        if (r.app_id, r.user_id) not in off]
    done = 0
    for ep in rows:
        steps = ep.steps or []
        if len(steps) > 3:
            skel = []
            for i, st in enumerate(steps):
                if i == 0 or i == len(steps) - 1 or st.get("error"):
                    skel.append(st)
                else:
                    skel.append({"action": str(st.get("action", ""))[:120],
                                 "skeletonized": True})
            ep.steps = skel
        ep.skeletonized = True
        done += 1
    if done:
        db.commit()
    return done


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
        # 显著性领取（第十七轮）：失败/昂贵的经历先复盘，同显著性仍按提交序
        .order_by(TaskEpisode.salience.desc(), TaskEpisode.created_at).limit(1)
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
    # 修复第 4 轮：先取主键——distill_episode 回滚后 ep 对象过期，
    # 再访问 ep.id 会触发 refresh，已删行即抛 ObjectDeletedError；
    # 此前两处 except 分支与末尾返回都有同一隐患。
    ep_id = ep.id
    try:
        distill_episode(db, ep, distiller, embedder, linker=linker)
    except InvalidExtraction:
        db.rollback()
        ep = db.get(TaskEpisode, ep_id)
        # 修复第 4 轮：轨迹行在提炼中途被删是正常终态，直接返回 None；
        # 此前直接解引用抛 AttributeError，掩盖原错且无法记账。
        if ep is None:
            return None
        ep.distill_status = "done"
        ep.locked_until = None
        ep.last_error = "discarded: invalid distiller output"
        db.add(AuditLog(app_id=ep.app_id, user_id=ep.user_id, action="distill",
                        decision="discard"))
        db.commit()
    except Exception as e:  # 瞬时故障：计数后退回重试或标失败，错误留痕
        db.rollback()
        ep = db.get(TaskEpisode, ep_id)
        if ep is None:
            return None
        ep.distill_status = "failed" if ep.attempts >= MAX_DISTILL_ATTEMPTS else "pending"
        ep.locked_until = None
        ep.last_error = str(e)[:2000]
        db.commit()
    return db.get(TaskEpisode, ep_id)


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
    # 第二十轮「边学习」：人工反馈同样归因到边——取这条经验最近一次未结账的
    # 扩散交付（即最近一次简报经边带出它），按 helpful 改边权。直达/直写
    # 的经验没有交付边，边权不动（反馈只动经验置信度，如上）。归因是近似：
    # 反馈本身不带交付 id，最近一次未结账交付是最可能的“这次使用”。
    erow = db.scalar(select(BriefDeliveryEdge).where(
        BriefDeliveryEdge.app_id == u.app_id, BriefDeliveryEdge.user_id == u.user_id,
        BriefDeliveryEdge.insight_id == insight_id,
        BriefDeliveryEdge.applied.is_(False))
        .order_by(BriefDeliveryEdge.created_at.desc()))
    if erow is not None:
        erow.applied = True
        link = db.get(MemoryLink, erow.link_id)
        if link is not None and link.valid_to is None:
            link.strength = (strength_on_applied(link.strength or 0.5) if helpful
                             else strength_on_rejected(link.strength or 0.5))
    db.add(AuditLog(app_id=u.app_id, user_id=u.user_id, action="feedback",
                    fact_ids=[row.id]))
    db.flush()
    return row


def record_fact_feedback(db: Session, u: UserContext, fact_id: str, *,
                         helpful: bool) -> MemoryFact:
    """事实级反馈（第二十三轮）：经验反馈闭环在事实上的对位——直接 memory_write
    的用户终于有了事实回流通道。

    只认本人名下的事实（跨用户/不存在一律 ValueError，REST 转 404，不泄露
    存在性；与 record_feedback 同形，不查 valid_to——已关闭的旧版本同样可
    被评价，评价落到该行本身）。好评记 feedback_pos、差评记 feedback_neg，
    置信度当场按 worker.fact_confidence 重算并落库（无反馈时恒等于旧佐证
    公式，旧行数值不动）。事实没有扩散交付边，不碰边权；也没有闲置衰减
    时钟，不刷时间。调用方负责 commit。"""
    from app.worker import fact_confidence

    row = db.scalar(select(MemoryFact).where(
        MemoryFact.id == fact_id, MemoryFact.app_id == u.app_id,
        MemoryFact.user_id == u.user_id))
    if row is None:
        raise ValueError("fact not found")
    if helpful:
        row.feedback_pos = (row.feedback_pos or 0) + 1
    else:
        row.feedback_neg = (row.feedback_neg or 0) + 1
    row.confidence = fact_confidence(row.support_count or 1,
                                     row.feedback_pos or 0, row.feedback_neg or 0)
    db.add(AuditLog(app_id=u.app_id, user_id=u.user_id, action="feedback",
                    fact_ids=[row.id]))
    db.flush()
    return row


def record_preference_feedback(db: Session, u: UserContext, preference_id: str, *,
                               helpful: bool) -> MemoryPreference:
    """偏好级反馈（第二十三轮）：与 record_fact_feedback 同形——偏好与事实
    共用佐证置信口径（第十七轮），反馈计数与重算公式也共用。"""
    from app.worker import fact_confidence

    row = db.scalar(select(MemoryPreference).where(
        MemoryPreference.id == preference_id, MemoryPreference.app_id == u.app_id,
        MemoryPreference.user_id == u.user_id))
    if row is None:
        raise ValueError("preference not found")
    if helpful:
        row.feedback_pos = (row.feedback_pos or 0) + 1
    else:
        row.feedback_neg = (row.feedback_neg or 0) + 1
    row.confidence = fact_confidence(row.support_count or 1,
                                     row.feedback_pos or 0, row.feedback_neg or 0)
    db.add(AuditLog(app_id=u.app_id, user_id=u.user_id, action="feedback",
                    fact_ids=[row.id]))
    db.flush()
    return row


DECAY_GRACE_DAYS = 30      # 闲置宽限：最后活跃 30 天内置信度不动
DECAY_FACTOR = 0.95        # 宽限后每满 30 天置信度乘一次
CONFIDENCE_FLOOR = 0.1     # 衰减下限：再老也不归零（证据还在，只是久未验证）
MERGE_COSINE = 0.95        # 向量近似到这个程度且同主题，视为同一条经验
CROSS_SUBJECT_MERGE_COSINE = 0.98  # 第十九轮：主题不同时误合代价更大，门槛再高一档；无向量不猜
MERGE_CONTENT_CAP = 4000   # 第十八轮：合并时败者内容附录进保留行，总长上限
MERGE_APPLIES_CAP = 500    # 第十八轮：合并时适用情境拼接上限
RETIRE_IDLE_DAYS = 180     # 真遗忘线（第十六轮）：闲置超半年、从未被应用、从未有过正面证据
ADJUDICATE_GAP = 0.30      # 矛盾裁决门槛：置信度差距至少这么大才动手
ADJUDICATE_MIN_EVIDENCE = 2  # 且胜方至少攒了这么多条证据（人工+自动），防先验互相残杀


def _aware(dt):
    """SQLite 读回的 datetime 可能丢 tzinfo；统一补 UTC 再参与运算，免得 aware/naive 相减报错。
    None 原样返回（与 edge_learning._aware 同口径；decayed_until 对旧行就是 None）。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _pair_cosine(a: MemoryInsight, b: MemoryInsight) -> float | None:
    if a.embedding is None or b.embedding is None or a.embed_model != b.embed_model:
        return None
    va = [float(x) for x in a.embedding]
    vb = [float(x) for x in b.embedding]
    if len(va) != len(vb):
        return None
    return cosine(va, vb)


def consolidate_insights(db: Session, now: datetime | None = None, linker=None) -> dict:
    """经验生命周期巡检（第十一轮）：衰减、合并、矛盾标记。Worker 每小时跑一轮。

    - 衰减（第二十二轮遗忘曲线）：基线 = max(最后使用, 衰减锚点 decayed_until)；
      首扣（锚点为空或锚点 ≤ 最后使用）用宽限公式，续扣每满 30 天 exactly 一块，
      扣完锚点记到 now——同一段闲置只扣一次（此前无锚点时每小时巡检重复计费）。
      月因子随复述变慢：1 - 0.05/(1 + applied_count + feedback_pos)，0 次复述即
      旧的 0.95。只压置信度、不动反馈计数——新反馈进来时 record_feedback 按计数
      重算，等于用新证据覆盖衰减，这正是想要的语义。
    - 合并：同用户内 same_subject 且（内容规范化相同，或向量余弦 ≥ 0.95）的现行
      经验对，保留置信度高者（并列取更新的），另一行关行、计数并入保留行、留审计。
      第十九轮：主题不同但内容向量 ≥ 0.98 的对也合并（换个词说同一件事的经验
      不再并存）；无向量时跨主题不猜。
    - 矛盾：合并后仍同主题共存、内容不同的现行对只计数上报；有 contradicts 边
      （第十五轮织网）且证据悬殊的对在下方裁决环节处理。
    - 裁决（第十六轮）：被 contradicts 边连着的经验对，置信度差 ≥ 0.30 且胜方
      证据（人工+自动）≥ 2 条时，负方关行、边关行——矛盾不再无限期并存。
      置顶的经验是用户亲手策展的，永不自动裁掉。
    - 真遗忘（第十六轮）：闲置超 180 天、从未被应用（applied_count=0）、从未有
      正面证据（人工好评与自动成功皆无）的经验关行退出召回——衰减下限 0.1 还留
      约 58% 排序权重，那不叫遗忘，叫赖着不走。关行不真删，面板与导出仍可见。
    返回 {"decayed": n, "merged": n, "conflicts": n, "adjudicated": n, "retired": n,
    "differentiated": n, "synthesized": n}，自己 commit。
    修复第 4 轮：显式关掉记忆的用户整轮跳过（入口快照语义，轮内翻转
    下一轮起效，见 fix4-design-draft.md §5）。"""
    now = now or _now()
    from app.api.me import switched_off_users

    off = switched_off_users(db)
    rows = [r for r in list(db.scalars(select(MemoryInsight).where(MemoryInsight.valid_to.is_(None))))
            if (r.app_id, r.user_id) not in off]
    decayed = 0
    retired = 0
    for row in rows:
        conf = row.confidence if row.confidence is not None else 0.5
        idle = (now - _aware(row.last_used_at or row.valid_from)).days
        # 第二十二轮「遗忘曲线」：锚点结清 + 复述依赖因子。锚点为空或锚点 ≤
        # 最后使用（被用上刷新了时钟，宽限重算）时用宽限公式；锚点新于最后
        # 使用（续扣）时每满 30 天 exactly 一块，宽限不再重算——两式在总额上
        # 与旧单次口径一致，只修每小时重复计费。复述计数 = 任务回流证实应用
        # + 人工好评（曝光不算，auto_pos 已含在 applied_count 里不重复计）；
        # 复述越多月因子越近 1，0 次复述即旧的 0.95。
        anchor = _aware(row.decayed_until)
        last_use = _aware(row.last_used_at or row.valid_from)
        if anchor is None or anchor <= last_use:
            blocks = (idle - 1) // DECAY_GRACE_DAYS if idle > DECAY_GRACE_DAYS else 0
        else:
            blocks = (now - anchor).days // DECAY_GRACE_DAYS
        if blocks >= 1:
            rehearsals = (row.applied_count or 0) + (row.feedback_pos or 0)
            factor = 1.0 - (1.0 - DECAY_FACTOR) / (1 + rehearsals)
            new_conf = max(CONFIDENCE_FLOOR, conf * (factor ** blocks))
            if new_conf < conf - 1e-9:
                row.confidence = round(new_conf, 4)
                row.decayed_until = now
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
                # 修复第 6 轮：合并只在同一作用域内——项目不同即跳过。
                # 跨项目配对根本不在同一比较域，不计入 conflicts
                #（conflicts 是“同域但未达合并线”）；巡检不得借合并关掉
                # 别项目的行，否则写路径的分桶会被巡检重新打开。
                # kind 不设门：同主题只有一条现行行是既定语义（评测门禁 c09），
                # 跨 kind 合并与跨 kind 版本滚动同一口径。
                if a.project != b.project:
                    continue
                # 第十九轮：跨主题语义合并——此前合并只认 same_subject，换个词
                # 说同一件事的经验永远并存。主题不同时只认向量（≥0.98），不认
                # 词面相等（词面相等但主题不同，更可能是两回事）。
                pc = _pair_cosine(a, b)
                if same_subject(a.subject, b.subject):
                    merge = norm(a.content) == norm(b.content) or (pc is not None and pc >= MERGE_COSINE)
                else:
                    merge = pc is not None and pc >= CROSS_SUBJECT_MERGE_COSINE
                if merge:
                    keeper, loser = (a, b) if ((a.confidence or 0.5), a.valid_from) >= \
                        ((b.confidence or 0.5), b.valid_from) else (b, a)
                    # 第十八轮：败者内容不再静默丢弃——以附录形式并入保留行，
                    # 面板与导出仍能看到被合并掉的那份表述；适用情境去重拼接。
                    # 修复第 8 轮：附录段首打 merged-from 标记（审计 B-F6）——
                    # 忘掉败者时可按 id 精确剥离该段，不靠主题猜测。
                    appendix = (f"\n\n[合并自同类经验：{loser.subject} "
                                f"<!-- merged-from:{loser.id} -->] {loser.content or ''}")
                    keeper.content = (keeper.content + appendix)[:MERGE_CONTENT_CAP]
                    if loser.applies_when and loser.applies_when not in (keeper.applies_when or ""):
                        keeper.applies_when = (
                            (keeper.applies_when or "") + "；另见：" + loser.applies_when
                        )[:MERGE_APPLIES_CAP]
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
    # 第二十轮「条件分化」：裁决前先看输家——有与胜家不同的适用情境、且
    # 自己攒过证据（被用过/被反馈过）的，不退役：它不是错的，只是在不同
    # 条件下成立。关掉 contradicts 边（不再是对立），改连一条 related 边
    # 标注这次分化；只有同条件且证据更弱时才退役。
    adjudicated = 0
    differentiated = 0
    synthesized = 0
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
        loser_evidence = ((loser.feedback_pos or 0) + (loser.feedback_neg or 0)
                          + (loser.auto_pos or 0) + (loser.auto_neg or 0))
        if (loser_evidence >= 1 and (loser.applies_when or "").strip()
                and norm(loser.applies_when or "") != norm(winner.applies_when or "")):
            link.valid_to = now
            create_link(db, loser.app_id, loser.user_id,
                        ("insight", winner.id), ("insight", loser.id),
                        "related", 0.5, "rule")
            differentiated += 1
            db.add(AuditLog(app_id=loser.app_id, user_id=loser.user_id, action="consolidate",
                            fact_ids=[winner.id, loser.id], decision="differentiate"))
            # 第二十四轮「分化即综合」：分化到此为止还不够——“什么条件下用
            # 哪边”要落成一条可复用的原则，挂在两边之上。无 linker 或起草
            # 失败/返回空时分化照常（related 边已建）；判定器抛异常绝不回滚
            # 分化、不拖垮巡检（与 weave 的 judge 失败处理同哲学）。
            if linker is not None:
                try:
                    drafted = linker.synthesize_condition(
                        view_of(winner), view_of(loser), evidence, loser_evidence)
                except Exception as e:
                    log.warning("synthesize_condition failed: %s", e)
                    drafted = None
                if drafted is not None:
                    subject, content, applies_when = drafted
                    u = UserContext(winner.app_id, winner.user_id)
                    decision, gid = _apply_insight(
                        db, u, _Cand("principle", subject, content, applies_when),
                        None, None, project=None, additive=True)
                    # NOOP 表示同主题同内容已有一行：不再重复挂边，免得把
                    # 一条非原则行当成原则挂到两边之上。
                    if gid and decision != "noop":
                        create_link(db, winner.app_id, winner.user_id,
                                    ("insight", gid), ("insight", winner.id),
                                    "generalizes", 0.8, "llm")
                        create_link(db, winner.app_id, winner.user_id,
                                    ("insight", gid), ("insight", loser.id),
                                    "generalizes", 0.8, "llm")
                        synthesized += 1
                        db.add(AuditLog(app_id=winner.app_id, user_id=winner.user_id,
                                        action="consolidate",
                                        fact_ids=[winner.id, loser.id, gid],
                                        decision="synthesize"))
            continue
        loser.valid_to = now
        link.valid_to = now
        adjudicated += 1
        db.add(AuditLog(app_id=loser.app_id, user_id=loser.user_id, action="consolidate",
                        fact_ids=[winner.id, loser.id], decision="adjudicate"))
    db.commit()
    return {"decayed": decayed, "merged": merged, "conflicts": conflict_pairs,
            "adjudicated": adjudicated, "differentiated": differentiated, "retired": retired,
            "synthesized": synthesized}


def brief_for_task(db: Session, u: UserContext, task: str, k: int = 5,
                   embedder: GeminiEmbedder | None = None, token_budget: int = 1500,
                   project: str | None = None, meta: dict | None = None) -> list[InsightOut]:
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
    # 元记忆（第十八轮）：简报要自报家门——没向量时是纯关键词降级结果，
    # 调用方（REST/MCP）据此判断这份简报的可信度，而不是当成全量召回用。
    # 返回值保持 list 不变（旧测试与调用方不受影响），meta 字典外带。
    if meta is not None:
        if embedder is None:
            meta.update(degraded=True, degraded_reason="no_embedder")
        elif qvec is None:
            meta.update(degraded=True, degraded_reason="embed_failed")
        else:
            meta.update(degraded=False, degraded_reason=None)
    # 无查询向量时不搬 embedding 列：SQLite 上它是 JSON，每行都要解码，纯属浪费
    cols = [MemoryInsight.id, MemoryInsight.kind, MemoryInsight.subject,
            MemoryInsight.content, MemoryInsight.applies_when,
            MemoryInsight.confidence, MemoryInsight.project,
            MemoryInsight.pinned, MemoryInsight.valid_from,
            MemoryInsight.last_used_at, MemoryInsight.source_episode_id,
            MemoryInsight.verified, MemoryInsight.agent,
            MemoryInsight.evidence_chained]
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
        # 第十八轮：预算装包 break 改 skip——超预算的长条目跳过、继续试后面
        # 更小的条目，而不是一超就截断整份简报（扩散分支早就是 continue）。
        if used + cost > token_budget:
            continue
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
    # 曝光记账一条批量 UPDATE（不用逐行取 ORM 行再 +1 再 flush）：
    # 候选阶段只搬了轻量列，k 次逐行 UPDATE + k 次 after_update 事件是简报尾巴
    # 的主要零碎开销；Core UPDATE 不触发 after_update，正好配合 models 层
    # “非文本更新跳过倒排同步”（即使触发，use_count 也不是文本属性）。
    # 语义与旧代码一致：NULL 按 0 计（旧代码 orm.use_count or 0），
    # last_used_at 照旧不刷新（第十六轮口径）。
    if winners:
        db.execute(update(MemoryInsight).where(
            MemoryInsight.id.in_([row.id for _, row, _ in winners])).values(
                use_count=func.coalesce(MemoryInsight.use_count, 0) + 1))
    out: list[InsightOut] = []
    for score, row, reason in winners:
        out.append(InsightOut(insight_id=row.id, kind=row.kind, subject=row.subject,
                              content=row.content, applies_when=row.applies_when,
                              score=round(score, 4), reason=reason,
                              confidence=round(row.confidence, 4)
                              if row.confidence is not None else 0.5,
                              project=row.project, source_episode_id=row.source_episode_id,
                              conflicts_with=conflicts.get(row.id, []),
                              linked_from=linked_from.get(row.id),
                              verified=True if row.verified is None else bool(row.verified),
                              agent=row.agent,
                              evidence_chained=bool(row.evidence_chained)))
    # 第二十轮「抽象阶梯」：胜出的原则（kind=principle）把它的泛化支撑
    # 经验一并带上——原则是顶，支撑是底，简报自顶向下递出一小段阶梯。
    # 支撑不占 k 名额（它是原则的脚注，不是 competing 候选），但受 token
    # 预算约束；不进交付记录（回流只给胜出的原则结账，不给脚注），
    # 也不记交付边（避免未结账的交付边污染人工反馈的归因）。
    seen_ids = {row.id for _, row, _ in winners}
    for pscore, prow, _ in winners:
        if prow.kind != "principle":
            continue
        sup_links = list(db.scalars(select(MemoryLink).where(
            MemoryLink.app_id == u.app_id, MemoryLink.user_id == u.user_id,
            MemoryLink.valid_to.is_(None), MemoryLink.relation == "generalizes",
            MemoryLink.src_kind == "insight", MemoryLink.src_id == prow.id,
            MemoryLink.dst_kind == "insight")))
        if not sup_links:
            continue
        sup_rows = list(db.scalars(select(MemoryInsight).where(
            MemoryInsight.id.in_([lnk.dst_id for lnk in sup_links]),
            MemoryInsight.valid_to.is_(None),
            or_(MemoryInsight.muted.is_(False), MemoryInsight.muted.is_(None)))))
        by_id = {r.id: r for r in sup_rows}
        cands = [(lnk, by_id[lnk.dst_id]) for lnk in sup_links
                 if lnk.dst_id in by_id and lnk.dst_id not in seen_ids]
        # 同项目支撑优先，其次置信度；每原则最多 2 条
        cands.sort(key=lambda t: ((t[1].project == project) if project else False,
                                  t[1].confidence or 0.5), reverse=True)
        for link, srow in cands[:2]:
            cost = _est_tokens(srow.content)
            if used + cost > token_budget:
                continue
            used += cost
            seen_ids.add(srow.id)
            srow.use_count = (srow.use_count or 0) + 1
            out.append(InsightOut(
                insight_id=srow.id, kind=srow.kind, subject=srow.subject,
                content=srow.content, applies_when=srow.applies_when,
                score=round(pscore * (link.strength or 0.5), 4),
                reason="principle_support",
                confidence=round(srow.confidence, 4)
                if srow.confidence is not None else 0.5,
                project=srow.project, source_episode_id=srow.source_episode_id,
                conflicts_with=[], linked_from=prow.id,
                verified=True if srow.verified is None else bool(srow.verified),
                agent=srow.agent,
                evidence_chained=bool(srow.evidence_chained)))
    if winners:
        # 交付记录（第十六轮）：结果回流靠它把后续任务的成败结给这批经验
        delivery = BriefDelivery(app_id=u.app_id, user_id=u.user_id, project=project,
                                 task=task[:500],
                                 insight_ids=[row.id for _, row, _ in winners])
        db.add(delivery)
        # 第二十轮「边学习」的记账：扩散带出的赢家，记下它经哪条边进来。
        # 直达的赢家没有边；只有 reason == "linked" 的才有 linked_from。
        # flush 先把 _id 默认值落到 delivery.id 上，后面结账时按 delivery_id 找边。
        db.flush()
        for _, row, reason in winners:
            if reason != "linked":
                continue
            via = linked_from.get(row.id)
            if via is None:
                continue
            edge = find_spread_edge(db, u.app_id, u.user_id, via, row.id)
            if edge is None:
                continue
            db.add(BriefDeliveryEdge(app_id=u.app_id, user_id=u.user_id,
                                     delivery_id=delivery.id, link_id=edge.id,
                                     insight_id=row.id))
    # 曝光计数当场提交：brief 从第十轮起是「读 + 记账」，若只 flush 不 commit，
    # 调用方会话会一直攥着写事务——REST 与直调在同一进程交替出现时能把库锁死
    # （SQLite 实测 database is locked；PG 上则是行锁干等）。简报的账自己结清，
    # 调用方随后的审计 commit 不受影响。
    db.commit()
    return out
