"""「我的记忆」面板用的 /v1/me/* 接口：查看、纠正、清空、导出、总开关、写入任务状态。

所有查询都带 user_id 条件，Postgres 上再由 FORCE RLS 兜底。
"""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import models as m
from app.api.memories import _audit, get_db
from app.auth import UserContext, bind_user, user_ctx, user_from_query
from app.schemas import CONSENT_VERSION, CorrectRequest, JobOut, MemoryItem, MemoryPage, SettingsIn, SettingsOut

router = APIRouter(prefix="/v1/me")


def _mine(model, u: UserContext):
    return (model.app_id == u.app_id, model.user_id == u.user_id)


def get_setting(db: Session, u: UserContext) -> m.UserSetting | None:
    return db.scalar(select(m.UserSetting).where(*_mine(m.UserSetting, u)))


def memory_enabled(db: Session, u: UserContext) -> bool:
    s = get_setting(db, u)
    return bool(s and s.memory_enabled and s.consent_version == CONSENT_VERSION)


def _fact_item(f: m.MemoryFact) -> MemoryItem:
    return MemoryItem(id=f.id, kind="fact", subject=f.subject, content=f.content, version=f.version,
                      valid_from=f.valid_from, source_session_id=f.source_session_id)


def _pref_item(p: m.MemoryPreference) -> MemoryItem:
    return MemoryItem(id=p.id, kind="preference", subject=p.key, content=p.value, version=p.version,
                      valid_from=p.valid_from)


@router.get("/memories", response_model=MemoryPage)
def list_memories(kind: str | None = Query(None, pattern="^(fact|preference)$"),
                  offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                  u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """只列当前有效版本；按时间倒序。"""
    bind_user(db, u)
    items: list[MemoryItem] = []
    if kind in (None, "preference"):
        items += [_pref_item(p) for p in db.scalars(select(m.MemoryPreference).where(
            *_mine(m.MemoryPreference, u), m.MemoryPreference.valid_to.is_(None)))]
    if kind in (None, "fact"):
        items += [_fact_item(f) for f in db.scalars(select(m.MemoryFact).where(
            *_mine(m.MemoryFact, u), m.MemoryFact.valid_to.is_(None)))]
    items.sort(key=lambda i: i.valid_from, reverse=True)
    return MemoryPage(items=items[offset:offset + limit], total=len(items), offset=offset, limit=limit)


@router.patch("/memories/{item_id}", response_model=MemoryItem)
def correct(item_id: str, req: CorrectRequest, db: Session = Depends(get_db)):
    """纠正：旧版本 valid_to=now 失效，新建 version+1；召回只会看到新版本。"""
    u = bind_user(db, user_ctx(req.user_id))
    now = datetime.now(UTC)
    old = db.get(m.MemoryFact, item_id)
    if old and old.user_id == u.user_id and old.app_id == u.app_id and old.valid_to is None:
        old.valid_to = now
        new = m.MemoryFact(app_id=u.app_id, user_id=u.user_id, subject=old.subject, content=req.content,
                           version=old.version + 1, valid_from=now, source_session_id=None)
        # 用户手动纠正没有来源会话；向量由 Worker 后续补（embedding 为空时召回走关键词）
        db.add(new)
        _audit(db, u, "correct", [old.id, new.id])
        db.commit()
        return _fact_item(new)
    oldp = db.get(m.MemoryPreference, item_id)
    if oldp and oldp.user_id == u.user_id and oldp.app_id == u.app_id and oldp.valid_to is None:
        oldp.valid_to = now
        newp = m.MemoryPreference(app_id=u.app_id, user_id=u.user_id, key=oldp.key, value=req.content,
                                  version=oldp.version + 1, valid_from=now)
        db.add(newp)
        _audit(db, u, "correct", [oldp.id, newp.id])
        db.commit()
        return _pref_item(newp)
    raise HTTPException(404)  # 不存在、已失效或不属于当前用户，一律 404


@router.delete("/memories", status_code=204)
def clear_all(u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """全部清空：事实（含历史版本和向量）、偏好、摘要、原始消息、未完成的写入任务。

    v1 同步执行（单用户数据量小）；id 记入 forgotten_id，备份恢复后据此重删。
    """
    bind_user(db, u)
    fact_ids = list(db.scalars(select(m.MemoryFact.id).where(*_mine(m.MemoryFact, u))))
    pref_ids = list(db.scalars(select(m.MemoryPreference.id).where(*_mine(m.MemoryPreference, u))))
    for model in (m.MemoryFact, m.MemoryPreference, m.SessionSummary, m.SessionMessage, m.MemoryJob):
        db.execute(delete(model).where(*_mine(model, u)))
    for i in fact_ids:
        db.merge(m.ForgottenId(id=i, kind="fact"))
    for i in pref_ids:
        db.merge(m.ForgottenId(id=i, kind="preference"))
    _audit(db, u, "clear_all", fact_ids + pref_ids)
    db.commit()


@router.get("/memories/export")
def export(u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """导出我的全部数据（个保法 可携带权）：含历史版本，不含向量。"""
    bind_user(db, u)
    facts = db.scalars(select(m.MemoryFact).where(*_mine(m.MemoryFact, u)).order_by(m.MemoryFact.valid_from))
    prefs = db.scalars(select(m.MemoryPreference).where(*_mine(m.MemoryPreference, u)))
    s = get_setting(db, u)
    _audit(db, u, "export", [])
    db.commit()
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "user_id": u.user_id,
        "settings": {"memory_enabled": bool(s and s.memory_enabled),
                     "consent_version": s.consent_version if s else None},
        "facts": [{"id": f.id, "subject": f.subject, "content": f.content, "version": f.version,
                   "valid_from": iso(f.valid_from), "valid_to": iso(f.valid_to),
                   "source_session_id": f.source_session_id} for f in facts],
        "preferences": [{"id": p.id, "key": p.key, "value": p.value, "version": p.version,
                         "valid_from": iso(p.valid_from), "valid_to": iso(p.valid_to)} for p in prefs],
    }


def _settings_out(s: m.UserSetting | None) -> SettingsOut:
    return SettingsOut(memory_enabled=bool(s and s.memory_enabled),
                       consent_version=s.consent_version if s else None, consent_at=s.consent_at if s else None)


@router.get("/settings", response_model=SettingsOut)
def read_settings(u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    bind_user(db, u)
    return _settings_out(get_setting(db, u))


@router.put("/settings", response_model=SettingsOut)
def write_settings(req: SettingsIn, db: Session = Depends(get_db)):
    """开启记忆必须带当前隐私说明版本（= 用户在授权页点了同意）；关闭随时可以。"""
    u = bind_user(db, user_ctx(req.user_id))
    s = get_setting(db, u)
    if req.memory_enabled:
        if req.consent_version != CONSENT_VERSION:
            raise HTTPException(409, detail={"code": "consent_required", "current_consent_version": CONSENT_VERSION})
        if not s:
            s = m.UserSetting(app_id=u.app_id, user_id=u.user_id)
            db.add(s)
        if s.consent_version != CONSENT_VERSION:
            s.consent_version, s.consent_at = CONSENT_VERSION, datetime.now(UTC)
        s.memory_enabled = True
    elif s:
        s.memory_enabled = False
    _audit(db, u, "settings", [])
    db.commit()
    return _settings_out(s)


@router.get("/jobs", response_model=list[JobOut])
def jobs(limit: int = Query(20, ge=1, le=100), u: UserContext = Depends(user_from_query),
         db: Session = Depends(get_db)):
    bind_user(db, u)
    rows = db.scalars(select(m.MemoryJob).where(*_mine(m.MemoryJob, u))
                      .order_by(m.MemoryJob.created_at.desc()).limit(limit))
    return [JobOut(job_id=j.id, session_id=j.session_id, status=j.status, attempts=j.attempts,
                   last_error=j.last_error, created_at=j.created_at) for j in rows]


