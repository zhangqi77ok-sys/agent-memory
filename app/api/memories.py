from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models as m
from app.auth import UserContext, bind_user, user_ctx, user_from_query
from app.config import settings
from app.db import SessionLocal
from app.embeddings import build_embedder
from app.recall import recall_facts
from app.schemas import RecallRequest, RecallResponse, WriteAccepted, WriteRequest

router = APIRouter(prefix="/v1")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _audit(db: Session, u: UserContext, action: str, ids: list[str]) -> None:
    db.add(m.AuditLog(app_id=u.app_id, user_id=u.user_id, action=action, fact_ids=ids))


@router.post("/memories", status_code=202, response_model=WriteAccepted)
def write(req: WriteRequest, response: Response, idempotency_key: str = Header(...),
          db: Session = Depends(get_db)):
    u = bind_user(db, user_ctx(req.user_id))
    from app.api.me import memory_enabled  # 避免循环导入
    if not memory_enabled(db, u):
        # 未同意或已暂停：不落任何数据（连原始消息都不存），调用方照常继续
        return WriteAccepted(job_id=None, duplicate=False, skipped=True)
    existing = db.scalar(select(m.MemoryJob).where(
        m.MemoryJob.app_id == u.app_id, m.MemoryJob.user_id == u.user_id,
        m.MemoryJob.idempotency_key == idempotency_key))
    if existing:
        return WriteAccepted(job_id=existing.id, duplicate=True)
    job = m.MemoryJob(app_id=u.app_id, user_id=u.user_id, idempotency_key=idempotency_key,
                      session_id=req.session_id)
    db.add(job)
    db.flush()  # 客户端默认的 id 在 flush 时才生成，原文要挂上这个 job_id
    for msg in req.messages:
        db.add(m.SessionMessage(app_id=u.app_id, user_id=u.user_id, session_id=req.session_id,
                                job_id=job.id, msg_id=msg.msg_id, role=msg.role, content=msg.content))
    try:
        db.commit()
    except IntegrityError:  # 并发同键
        db.rollback()
        existing = db.scalar(select(m.MemoryJob).where(
            m.MemoryJob.app_id == u.app_id, m.MemoryJob.user_id == u.user_id,
            m.MemoryJob.idempotency_key == idempotency_key))
        return WriteAccepted(job_id=existing.id, duplicate=True)
    return WriteAccepted(job_id=job.id, duplicate=False)


@router.post("/memories/recall", response_model=RecallResponse)
def recall(req: RecallRequest, db: Session = Depends(get_db)):
    u = bind_user(db, user_ctx(req.user_id))
    from app.api.me import memory_enabled
    if not memory_enabled(db, u):
        return RecallResponse(preferences={}, facts=[])
    prefs = db.scalars(select(m.MemoryPreference).where(
        m.MemoryPreference.app_id == u.app_id, m.MemoryPreference.user_id == u.user_id,
        m.MemoryPreference.valid_to.is_(None))).all()
    facts = recall_facts(db, u, req.query, req.k, req.token_budget,
                         embedder=build_embedder(timeout=settings.recall_timeout_s))
    _audit(db, u, "recall", [f.fact_id for f in facts])
    db.commit()
    return RecallResponse(preferences={p.key: p.value for p in prefs}, facts=facts)


@router.delete("/me/memories/{item_id}", status_code=204)
def forget(item_id: str, u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """删除一条事实（同 subject 全部版本）或一条偏好。不属于当前用户一律 404。"""
    bind_user(db, u)
    fact = db.get(m.MemoryFact, item_id)
    if fact and fact.user_id == u.user_id and fact.app_id == u.app_id:
        versions = db.scalars(select(m.MemoryFact).where(
            m.MemoryFact.app_id == u.app_id, m.MemoryFact.user_id == u.user_id,
            m.MemoryFact.subject == fact.subject)).all()
        ids = [v.id for v in versions]
        db.execute(delete(m.MemoryFact).where(m.MemoryFact.id.in_(ids)))
        for i in ids:
            db.merge(m.ForgottenId(id=i, kind="fact"))
        # TODO(v1): 受影响的 session_summary 重新生成、来源 session_message 删除（Worker 任务）
        _audit(db, u, "forget", ids)
        db.commit()
        return
    pref = db.get(m.MemoryPreference, item_id)
    if pref and pref.user_id == u.user_id and pref.app_id == u.app_id:
        versions = db.scalars(select(m.MemoryPreference).where(
            m.MemoryPreference.app_id == u.app_id, m.MemoryPreference.user_id == u.user_id,
            m.MemoryPreference.key == pref.key)).all()
        ids = [v.id for v in versions]
        db.execute(delete(m.MemoryPreference).where(m.MemoryPreference.id.in_(ids)))
        for i in ids:
            db.merge(m.ForgottenId(id=i, kind="preference"))
        _audit(db, u, "forget", ids)
        db.commit()
        return
    raise HTTPException(404)
