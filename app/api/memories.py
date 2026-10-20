from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models as m
from app.auth import UserContext, current_user
from app.db import get_db
from app.recall import recall_facts
from app.schemas import RecallRequest, RecallResponse, WriteAccepted, WriteRequest

router = APIRouter(prefix="/v1")


def _audit(db: Session, u: UserContext, action: str, ids: list[str]) -> None:
    db.add(m.AuditLog(tenant_id=u.tenant_id, user_id=u.user_id, action=action, fact_ids=ids))


@router.post("/memories", status_code=202, response_model=WriteAccepted)
def write(req: WriteRequest, response: Response, idempotency_key: str = Header(...),
          u: UserContext = Depends(current_user), db: Session = Depends(get_db)):
    existing = db.scalar(select(m.MemoryJob).where(
        m.MemoryJob.tenant_id == u.tenant_id, m.MemoryJob.user_id == u.user_id,
        m.MemoryJob.idempotency_key == idempotency_key))
    if existing:
        return WriteAccepted(job_id=existing.id, duplicate=True)
    job = m.MemoryJob(tenant_id=u.tenant_id, user_id=u.user_id, idempotency_key=idempotency_key,
                      session_id=req.session_id)
    db.add(job)
    for msg in req.messages:
        db.add(m.SessionMessage(tenant_id=u.tenant_id, user_id=u.user_id, session_id=req.session_id,
                                msg_id=msg.msg_id, role=msg.role, content=msg.content))
    try:
        db.commit()
    except IntegrityError:  # 并发同键
        db.rollback()
        existing = db.scalar(select(m.MemoryJob).where(
            m.MemoryJob.tenant_id == u.tenant_id, m.MemoryJob.user_id == u.user_id,
            m.MemoryJob.idempotency_key == idempotency_key))
        return WriteAccepted(job_id=existing.id, duplicate=True)
    return WriteAccepted(job_id=job.id, duplicate=False)


@router.post("/memories/recall", response_model=RecallResponse)
def recall(req: RecallRequest, u: UserContext = Depends(current_user), db: Session = Depends(get_db)):
    prefs = db.scalars(select(m.MemoryPreference).where(
        m.MemoryPreference.tenant_id == u.tenant_id, m.MemoryPreference.user_id == u.user_id,
        m.MemoryPreference.valid_to.is_(None))).all()
    facts = recall_facts(db, u, req.query, req.k, req.token_budget)
    _audit(db, u, "recall", [f.fact_id for f in facts])
    db.commit()
    return RecallResponse(preferences={p.key: p.value for p in prefs}, facts=facts)


@router.delete("/me/memories/{fact_id}", status_code=204)
def forget(fact_id: str, u: UserContext = Depends(current_user), db: Session = Depends(get_db)):
    fact = db.get(m.MemoryFact, fact_id)
    # 不属于当前用户一律 404，不暴露存在性
    if not fact or fact.user_id != u.user_id or fact.tenant_id != u.tenant_id:
        raise HTTPException(404)
    versions = db.scalars(select(m.MemoryFact).where(
        m.MemoryFact.tenant_id == u.tenant_id, m.MemoryFact.user_id == u.user_id,
        m.MemoryFact.subject == fact.subject)).all()
    ids = [v.id for v in versions]
    db.execute(delete(m.MemoryFact).where(m.MemoryFact.id.in_(ids)))
    for i in ids:
        db.merge(m.ForgottenId(id=i, kind="fact"))
    # TODO(v1): 受影响的 session_summary 重新生成、来源 session_message 删除（Worker 任务）
    _audit(db, u, "forget", ids)
    db.commit()
