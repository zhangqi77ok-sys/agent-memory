"""经验记忆的 REST 入口：提交任务过程、开工前按情境要简报。

与 /v1/memories/* 平行：同意闸门一致（未同意或已暂停 → skipped/空，不落数据），
限流中间件按 /v1/ 路径统一覆盖。
"""
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app import models as m
from app.api.me import memory_enabled
from app.auth import bind_user, user_ctx
from app.config import settings
from app.db import get_db
from app.embeddings import build_embedder
from app.episodes import brief_for_task, find_episode_by_key, record_feedback, submit_episode
from app.schemas import BriefRequest, BriefResponse, EpisodeAccepted, EpisodeSubmitRequest, FeedbackOut, FeedbackRequest

router = APIRouter(prefix="/v1")


@router.post("/episodes", status_code=status.HTTP_202_ACCEPTED, response_model=EpisodeAccepted)
def submit(req: EpisodeSubmitRequest, idempotency_key: str | None = Header(default=None),
           db: Session = Depends(get_db)):
    u = bind_user(db, user_ctx(req.user_id))
    if not memory_enabled(db, u):
        return EpisodeAccepted(episode_id=None, skipped=True)
    if idempotency_key:
        existing = find_episode_by_key(db, u.app_id, u.user_id, idempotency_key)
        if existing is not None:
            return EpisodeAccepted(episode_id=existing.id, duplicate=True)
    ep = submit_episode(db, u, source=req.source, title=req.title, outcome=req.outcome,
                        steps=[s.model_dump() for s in req.steps], cost=req.cost,
                        idempotency_key=idempotency_key, project=req.project)
    return EpisodeAccepted(episode_id=ep.id)


@router.post("/brief", response_model=BriefResponse)
def brief(req: BriefRequest, db: Session = Depends(get_db)):
    u = bind_user(db, user_ctx(req.user_id))
    if not memory_enabled(db, u):
        return BriefResponse(insights=[])
    insights = brief_for_task(db, u, req.task, req.k,
                              embedder=build_embedder(timeout=settings.recall_timeout_s),
                              token_budget=req.token_budget, project=req.project)
    # 与事实召回对齐留审计（第八轮补：此前 brief 查了什么、给了什么完全无痕）
    db.add(m.AuditLog(app_id=u.app_id, user_id=u.user_id, action="brief",
                      fact_ids=[i.insight_id for i in insights]))
    db.commit()
    return BriefResponse(insights=insights)


@router.post("/insights/feedback", response_model=FeedbackOut)
def feedback(req: FeedbackRequest, db: Session = Depends(get_db)):
    """反馈闭环入口（第十轮）：任务结束后回报某条经验有没有用，置信度当场更新。

    未同意/已暂停时与 brief 一致地静默拒绝（404 与不存在同形，不泄露状态）；
    跨用户与不存在同样 404。"""
    u = bind_user(db, user_ctx(req.user_id))
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
