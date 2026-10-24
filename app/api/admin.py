"""管理端接口（第十七轮，用户要求）：跨用户总览所有人的记忆，只读。

安全口径：X-Admin-Key 头校验（app/auth.is_valid_admin_key，常量时间比较）；
未配置 ADMIN_KEYS 时整组接口 404（等于没这个功能），密钥错误 403。
只读：管理端能看、不能改——写操作仍走各用户自己的入口与同意闸门。
RLS 注意：迁移 0002 之后，应用账号（memsvc_app）不绑用户时一行都看不到
（这是有意为之的隔离，没有“未设置用户=全可见”的后门），所以管理查询不能
走 get_db。跨用户读统一走 WorkerSession——数据库账号 memsvc_worker 带
BYPASSRLS，绕过与否由账号决定、应用代码无法自行打开；接口层再用管理密钥
把关，两套机制各司其职。WORKER_DATABASE_URL 未配时（worker_engine 回落到
应用引擎）管理端直接 503，绝不静默返回空表。
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models as m
from app.auth import is_valid_admin_key
from app.db import WorkerSession

router = APIRouter(prefix="/v1/admin")


def get_admin_db():
    from app.config import settings

    if not settings.worker_database_url:
        raise HTTPException(status_code=503, detail="admin requires worker database")
    db = WorkerSession()
    try:
        yield db
    finally:
        db.close()


def _require_admin(x_admin_key: str | None) -> None:
    from app.config import settings

    if not settings.admin_key_list:
        raise HTTPException(status_code=404, detail="admin disabled")
    if not is_valid_admin_key(x_admin_key):
        raise HTTPException(status_code=403, detail="invalid admin key")


def _counts(db: Session, model, user_id: str) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(
        model.app_id == _app_id(), model.user_id == user_id)) or 0)


def _app_id() -> str:
    from app.config import settings

    return settings.app_id


@router.get("/overview")
def overview(db: Session = Depends(get_admin_db),
             x_admin_key: str | None = Header(default=None)):
    """用户清单 + 每人各类记忆计数：管理端首页一张表看全貌。"""
    _require_admin(x_admin_key)
    app_id = _app_id()
    user_ids = list(db.scalars(select(m.MemoryFact.user_id).where(
        m.MemoryFact.app_id == app_id).distinct()))
    seen = set(user_ids)
    for model in (m.MemoryPreference, m.MemoryInsight, m.TaskEpisode, m.MemoryReminder):
        for uid in db.scalars(select(model.user_id).where(model.app_id == app_id).distinct()):
            if uid not in seen:
                seen.add(uid)
                user_ids.append(uid)
    users = []
    for uid in sorted(seen):
        users.append({
            "user_id": uid,
            "facts": _counts(db, m.MemoryFact, uid),
            "preferences": _counts(db, m.MemoryPreference, uid),
            "insights": _counts(db, m.MemoryInsight, uid),
            "episodes": _counts(db, m.TaskEpisode, uid),
            "reminders_armed": int(db.scalar(select(func.count()).select_from(m.MemoryReminder).where(
                m.MemoryReminder.app_id == app_id, m.MemoryReminder.user_id == uid,
                m.MemoryReminder.status == "armed")) or 0),
        })
    return {"users": users}


@router.get("/users/{user_id}/memories")
def user_memories(user_id: str, db: Session = Depends(get_admin_db),
                  x_admin_key: str | None = Header(default=None),
                  limit: int = Query(200, ge=1, le=1000)):
    """某个用户的全部记忆（各类各取最近 limit 条）：事实/偏好/经验/轨迹/前瞻记忆。"""
    _require_admin(x_admin_key)
    app_id = _app_id()

    def rows(model, *extra):
        q = select(model).where(model.app_id == app_id, model.user_id == user_id, *extra)
        return list(db.scalars(q.order_by(model.id).limit(limit)))

    facts = rows(m.MemoryFact)
    prefs = rows(m.MemoryPreference)
    insights = rows(m.MemoryInsight)
    episodes = rows(m.TaskEpisode)
    reminders = rows(m.MemoryReminder)
    return {
        "user_id": user_id,
        "facts": [{"id": f.id, "subject": f.subject, "content": f.content,
                   "version": f.version, "confidence": f.confidence,
                   "support_count": f.support_count,
                   "valid_to": f.valid_to.isoformat() if f.valid_to else None} for f in facts],
        "preferences": [{"id": p.id, "key": p.key, "value": p.value,
                         "version": p.version, "confidence": p.confidence,
                         "support_count": p.support_count,
                         "valid_to": p.valid_to.isoformat() if p.valid_to else None} for p in prefs],
        "insights": [{"id": i.id, "kind": i.kind, "subject": i.subject, "content": i.content,
                      "confidence": i.confidence, "verified": bool(i.verified),
                      "applied_count": i.applied_count or 0, "use_count": i.use_count or 0,
                      "project": i.project,
                      "valid_to": i.valid_to.isoformat() if i.valid_to else None} for i in insights],
        "episodes": [{"id": e.id, "title": e.title, "outcome": e.outcome,
                      "project": e.project, "salience": e.salience,
                      "skeletonized": bool(e.skeletonized),
                      "distill_status": e.distill_status} for e in episodes],
        "reminders": [{"id": r.id, "condition": r.condition_text, "action": r.action_text,
                       "status": r.status, "once": bool(r.once),
                       "fire_count": r.fire_count or 0} for r in reminders],
    }
