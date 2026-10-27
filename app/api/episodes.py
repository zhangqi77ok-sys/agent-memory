"""经验记忆的 REST 入口：提交任务过程、开工前按情境要简报。

与 /v1/memories/* 平行：同意闸门一致（未同意或已暂停 → skipped/空，不落数据），
限流中间件按 /v1/ 路径统一覆盖。
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app import models as m
from app.api.me import memory_enabled
from app.auth import bind_user, resolve_asserted, user_ctx
from app.config import settings
from app.db import get_db
from app.embeddings import build_embedder
from app.episodes import (
    brief_for_task,
    find_episode_by_key,
    record_fact_feedback,
    record_feedback,
    record_preference_feedback,
    submit_episode,
)
from app.schemas import (
    BriefRequest,
    BriefResponse,
    EpisodeAccepted,
    EpisodeSubmitRequest,
    FactFeedbackOut,
    FactFeedbackRequest,
    FeedbackOut,
    FeedbackRequest,
    PreferenceFeedbackOut,
    PreferenceFeedbackRequest,
)

router = APIRouter(prefix="/v1")


@router.post("/episodes", status_code=status.HTTP_202_ACCEPTED, response_model=EpisodeAccepted)
def submit(req: EpisodeSubmitRequest, idempotency_key: str | None = Header(default=None),
           db: Session = Depends(get_db), request: Request = None):
    u = bind_user(db, user_ctx(resolve_asserted(request, req.user_id)))
    if not memory_enabled(db, u):
        return EpisodeAccepted(episode_id=None, skipped=True)
    if idempotency_key:
        existing = find_episode_by_key(db, u.app_id, u.user_id, idempotency_key)
        if existing is not None:
            return EpisodeAccepted(episode_id=existing.id, duplicate=True)
    if req.tombstone_check:
        # 第 39 轮：自动采集提交前置禁写判定（fix2 的内容哈希禁令在入库层
        # 落实一次）。手动提交默认不带 flag，走老路，不改现有语义。
        # 哈希口径必须与入库形态一致：submit_episode 存的是脱敏后的
        # title/steps。但入库形态按提交通道有两种：REST 经 model_dump()
        # 会带 observation/error 的 None 键，MCP/capture 的 dict 则不带；
        # clear_all 的墓碑直接对入库形态哈希，所以两种形态都要查，
        # 只查一种会漏拦另一通道提交后被遗忘的轨迹。
        from app.redact import redact
        from app.tombstone import is_episode_tombstoned
        check_steps_dropped = []
        check_steps_full = []
        for s in req.steps:
            dropped = {}
            full = {}
            for k, v in s.model_dump().items():
                full[k] = redact(v) if isinstance(v, str) else v
                if v is not None:
                    dropped[k] = full[k]
            check_steps_dropped.append(dropped)
            check_steps_full.append(full)
        title = redact(req.title)
        if is_episode_tombstoned(db, u.app_id, u.user_id,
                                 title, check_steps_dropped) or \
                is_episode_tombstoned(db, u.app_id, u.user_id,
                                      title, check_steps_full):
            raise HTTPException(status_code=409,
                                detail={"code": "tombstoned"})
    try:
        ep = submit_episode(db, u, source=req.source, title=req.title, outcome=req.outcome,
                            steps=[s.model_dump() for s in req.steps], cost=req.cost,
                            idempotency_key=idempotency_key, project=req.project)
    except ValueError as e:
        # 第十八轮：服务层完整性校验失败是客户端问题，不是 500
        raise HTTPException(status_code=400, detail=str(e)) from None
    return EpisodeAccepted(episode_id=ep.id)


@router.post("/brief", response_model=BriefResponse)
def brief(req: BriefRequest, db: Session = Depends(get_db), request: Request = None):
    u = bind_user(db, user_ctx(resolve_asserted(request, req.user_id)))
    if not memory_enabled(db, u):
        return BriefResponse(insights=[])
    embedder = build_embedder(timeout=settings.recall_timeout_s)
    meta: dict = {}
    insights = brief_for_task(db, u, req.task, req.k,
                              embedder=embedder,
                              token_budget=req.token_budget, project=req.project, meta=meta,
                              session_id=req.session_id)
    # 前瞻记忆（第十七轮）：武装中的意图拿这次任务撞一遍，命中的单独列出
    from app.reminders import check_triggers
    from app.schemas import reminder_out

    fired = check_triggers(db, u, req.task, embedder=embedder, project=req.project,
                           session_id=req.session_id)
    # 与事实召回对齐留审计（第八轮补：此前 brief 查了什么、给了什么完全无痕）
    db.add(m.AuditLog(app_id=u.app_id, user_id=u.user_id, action="brief",
                      fact_ids=[i.insight_id for i in insights]))
    db.commit()
    # 第二十轮「织网简报」：胜出集的诱导子图——胜出经验之间怎么连着，一并递出。
    from app.links import induced_edges

    graph = {"nodes": [{"id": i.insight_id, "subject": i.subject} for i in insights],
             "edges": induced_edges(db, u.app_id, u.user_id,
                                    [i.insight_id for i in insights])}
    return BriefResponse(insights=insights,
                         reminders=[reminder_out(r) for r in fired],
                         degraded=bool(meta.get("degraded")),
                         degraded_reason=meta.get("degraded_reason"),
                         graph=graph)


@router.post("/insights/feedback", response_model=FeedbackOut)
def feedback(req: FeedbackRequest, db: Session = Depends(get_db), request: Request = None):
    """反馈闭环入口（第十轮）：任务结束后回报某条经验有没有用，置信度当场更新。

    未同意/已暂停时与 brief 一致地静默拒绝（404 与不存在同形，不泄露状态）；
    跨用户与不存在同样 404。"""
    u = bind_user(db, user_ctx(resolve_asserted(request, req.user_id)))
    if not memory_enabled(db, u):
        raise HTTPException(status_code=404, detail="insight not found")
    try:
        row = record_feedback(db, u, req.insight_id, helpful=req.helpful,
                              task_outcome=req.task_outcome)
    except ValueError:
        db.rollback()
        raise HTTPException(status_code=404, detail="insight not found") from None
    db.commit()
    return FeedbackOut(insight_id=row.id, confidence=round(row.confidence, 4),
                       feedback_pos=row.feedback_pos or 0, feedback_neg=row.feedback_neg or 0,
                       success_count=row.success_count or 0)


@router.post("/facts/feedback", response_model=FactFeedbackOut)
def fact_feedback(req: FactFeedbackRequest, db: Session = Depends(get_db), request: Request = None):
    """事实级反馈入口（第二十三轮）：与 /v1/insights/feedback 同形——
    未同意/已暂停、跨用户、不存在一律 404，不泄露存在性。"""
    u = bind_user(db, user_ctx(resolve_asserted(request, req.user_id)))
    if not memory_enabled(db, u):
        raise HTTPException(status_code=404, detail="fact not found")
    try:
        row = record_fact_feedback(db, u, req.fact_id, helpful=req.helpful)
    except ValueError:
        db.rollback()
        raise HTTPException(status_code=404, detail="fact not found") from None
    db.commit()
    return FactFeedbackOut(fact_id=row.id, confidence=round(row.confidence, 4),
                           support_count=row.support_count or 1,
                           feedback_pos=row.feedback_pos or 0,
                           feedback_neg=row.feedback_neg or 0)


@router.post("/preferences/feedback", response_model=PreferenceFeedbackOut)
def preference_feedback(req: PreferenceFeedbackRequest, db: Session = Depends(get_db), request: Request = None):
    """偏好级反馈入口（第二十三轮）：与事实反馈同形。"""
    u = bind_user(db, user_ctx(resolve_asserted(request, req.user_id)))
    if not memory_enabled(db, u):
        raise HTTPException(status_code=404, detail="preference not found")
    try:
        row = record_preference_feedback(db, u, req.preference_id, helpful=req.helpful)
    except ValueError:
        db.rollback()
        raise HTTPException(status_code=404, detail="preference not found") from None
    db.commit()
    return PreferenceFeedbackOut(preference_id=row.id, confidence=round(row.confidence, 4),
                                 support_count=row.support_count or 1,
                                 feedback_pos=row.feedback_pos or 0,
                                 feedback_neg=row.feedback_neg or 0)
