"""多模态备忘的 REST 入口（第 40 轮）：截图 / 语音复盘以文本层入库。

与 /v1/episodes 平行：同意闸门一致（未同意或已暂停 → skipped/空，
不落数据）；墓碑检查默认就开（无 opt-out）；原文件字节永不入库，
external_uri 只存字符串、服务端永不拉取。
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.api.me import memory_enabled
from app.auth import UserContext, bind_user, resolve_asserted, user_ctx, user_from_query
from app.db import get_db
from app.media import MediaError, submit_media_batch, submit_media_memo
from app.schemas import (
    MediaAccepted,
    MediaBatchItemResult,
    MediaBatchRequest,
    MediaBatchResponse,
    MediaOut,
    MediaSubmitRequest,
)

router = APIRouter(prefix="/v1")


def _media_out(db: Session, ref: m.MemoryMediaRef) -> MediaOut:
    ep = db.get(m.TaskEpisode, ref.episode_id)
    return MediaOut(
        episode_id=ref.episode_id, kind=ref.kind, origin=ref.origin,
        title=ep.title if ep else "", outcome=ep.outcome if ep else "",
        project=ep.project if ep else None, filename=ref.filename,
        mime=ref.mime, byte_size=ref.byte_size, sha256=ref.sha256,
        external_uri=ref.external_uri, created_at=ref.created_at)


def _submit_one(db, u, req: MediaSubmitRequest, idempotency_key: str | None):
    try:
        ep, _ref = submit_media_memo(
            db, u, kind=req.kind, origin=req.origin, title=req.title,
            text=req.text, outcome=req.outcome, project=req.project,
            idempotency_key=idempotency_key, filename=req.filename,
            mime=req.mime, byte_size=req.byte_size, sha256=req.sha256,
            external_uri=req.external_uri)
    except MediaError as e:
        if e.code == "tombstoned":
            raise HTTPException(status_code=409, detail={"code": "tombstoned"}) from None
        raise HTTPException(status_code=400, detail={"code": e.code, "message": str(e)}) from None
    return ep


@router.post("/media", status_code=status.HTTP_202_ACCEPTED, response_model=MediaAccepted)
def submit_media(req: MediaSubmitRequest, idempotency_key: str | None = Header(default=None),
                 db: Session = Depends(get_db), request: Request = None):
    u = bind_user(db, user_ctx(resolve_asserted(request, req.user_id)))
    if not memory_enabled(db, u):
        return MediaAccepted(episode_id=None, skipped=True)
    if idempotency_key:
        from app.episodes import find_episode_by_key

        existing = find_episode_by_key(db, u.app_id, u.user_id, idempotency_key)
        if existing is not None:
            return MediaAccepted(episode_id=existing.id, duplicate=True)
    ep = _submit_one(db, u, req, idempotency_key)
    return MediaAccepted(episode_id=ep.id)


@router.post("/media/batch", response_model=MediaBatchResponse)
def submit_media_batch_endpoint(req: MediaBatchRequest,
                                db: Session = Depends(get_db), request: Request = None):
    u = bind_user(db, user_ctx(resolve_asserted(request, req.user_id)))
    if not memory_enabled(db, u):
        return MediaBatchResponse(results=[
            MediaBatchItemResult(index=i, ok=False, error="skipped")
            for i in range(len(req.items))])
    items = []
    for item in req.items:
        d = item.model_dump()
        d.pop("user_id", None)
        items.append(d)
    results = submit_media_batch(db, u, items)
    return MediaBatchResponse(results=[
        MediaBatchItemResult(index=r["index"], ok=r["ok"],
                             episode_id=r.get("episode_id"), error=r.get("error"))
        for r in results])


@router.get("/media", response_model=list[MediaOut])
def list_media(limit: int = Query(50, ge=1, le=100),
               u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    bind_user(db, u)
    if not memory_enabled(db, u):
        return []
    refs = db.scalars(select(m.MemoryMediaRef).where(
        m.MemoryMediaRef.app_id == u.app_id,
        m.MemoryMediaRef.user_id == u.user_id).order_by(
        m.MemoryMediaRef.created_at.desc()).limit(limit)).all()
    return [_media_out(db, r) for r in refs]


@router.get("/media/{episode_id}", response_model=MediaOut)
def get_media(episode_id: str, u: UserContext = Depends(user_from_query),
              db: Session = Depends(get_db)):
    bind_user(db, u)
    ref = db.scalar(select(m.MemoryMediaRef).where(
        m.MemoryMediaRef.app_id == u.app_id,
        m.MemoryMediaRef.user_id == u.user_id,
        m.MemoryMediaRef.episode_id == episode_id))
    if ref is None or not memory_enabled(db, u):
        # 与 dismiss 同形：他人/不存在/开关关闭一律 404，不泄露归属
        raise HTTPException(status_code=404, detail="not found")
    return _media_out(db, ref)
