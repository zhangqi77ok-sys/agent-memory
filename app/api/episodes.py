"""经验记忆的 REST 入口：提交任务过程、开工前按情境要简报。

与 /v1/memories/* 平行：同意闸门一致（未同意或已暂停 → skipped/空，不落数据），
限流中间件按 /v1/ 路径统一覆盖。
"""
from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.orm import Session

from app.api.me import memory_enabled
from app.auth import bind_user, user_ctx
from app.config import settings
from app.db import get_db
from app.embeddings import build_embedder
from app.episodes import brief_for_task, find_episode_by_key, submit_episode
from app.schemas import BriefRequest, BriefResponse, EpisodeAccepted, EpisodeSubmitRequest

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
                        idempotency_key=idempotency_key)
    return EpisodeAccepted(episode_id=ep.id)


@router.post("/brief", response_model=BriefResponse)
def brief(req: BriefRequest, db: Session = Depends(get_db)):
    u = bind_user(db, user_ctx(req.user_id))
    if not memory_enabled(db, u):
        return BriefResponse(insights=[])
    insights = brief_for_task(db, u, req.task, req.k,
                              embedder=build_embedder(timeout=settings.recall_timeout_s))
    return BriefResponse(insights=insights)
