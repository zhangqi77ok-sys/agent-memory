"""前瞻记忆的 REST 入口（第十七轮）：武装、列出、解除、按任务查触发。

与其它 /v1 入口同一纪律：未同意/已暂停时静默拒绝（不落数据），
解除只认本人名下（他人/不存在一律 404，不泄露存在性）。
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.me import memory_enabled
from app.auth import UserContext, bind_user, resolve_asserted, user_ctx, user_from_query
from app.db import get_db
from app.embeddings import build_embedder
from app.reminders import check_triggers, create_reminder, dismiss_reminder, list_reminder_deliveries, list_reminders
from app.schemas import ReminderCreateRequest, ReminderDeliveryOut, ReminderDismissRequest, ReminderOut, reminder_delivery_out, reminder_out

router = APIRouter(prefix="/v1")


@router.post("/reminders", status_code=status.HTTP_201_CREATED, response_model=ReminderOut)
def create(req: ReminderCreateRequest, db: Session = Depends(get_db), request: Request = None):
    u = bind_user(db, user_ctx(resolve_asserted(request, req.user_id)))
    if not memory_enabled(db, u):
        raise HTTPException(status_code=404, detail="reminder not found")
    row = create_reminder(db, u, condition=req.condition, action=req.action,
                          project=req.project, once=req.once,
                          embedder=build_embedder(), expires_at=req.expires_at)
    return reminder_out(row)


@router.get("/reminders", response_model=list[ReminderOut])
def list_(include_closed: bool = Query(False),
          u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    u = bind_user(db, u)
    if not memory_enabled(db, u):
        return []
    return [reminder_out(r) for r in list_reminders(db, u, include_closed=include_closed)]


@router.post("/reminders/{reminder_id}/dismiss", response_model=ReminderOut)
def dismiss(reminder_id: str, req: ReminderDismissRequest,
            db: Session = Depends(get_db), request: Request = None):
    u = bind_user(db, user_ctx(resolve_asserted(request, req.user_id)))
    if not memory_enabled(db, u):
        raise HTTPException(status_code=404, detail="reminder not found")
    try:
        row = dismiss_reminder(db, u, reminder_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="reminder not found") from None
    return reminder_out(row)


@router.get("/reminders/{reminder_id}/deliveries", response_model=list[ReminderDeliveryOut])
def deliveries(reminder_id: str, limit: int = Query(100, ge=1, le=100),
               u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """查一条提醒的投递账本（第 38 轮）：开关关闭用户返回 []（与 list_ 同形，
    不泄露）；他人/不存在的提醒 404（归属检查在开关检查之后，见设计稿）。"""
    u = bind_user(db, u)
    if not memory_enabled(db, u):
        return []
    try:
        rows = list_reminder_deliveries(db, u, reminder_id, limit=limit)
    except ValueError:
        raise HTTPException(status_code=404, detail="reminder not found") from None
    return [reminder_delivery_out(r) for r in rows]


@router.get("/reminders/check", response_model=list[ReminderOut])
def check(task: str = Query(..., min_length=1, max_length=2000),
          project: str | None = Query(None, max_length=128),
          session_id: str | None = Query(None, max_length=64),
          u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """拿一段任务描述去撞武装中的前瞻记忆（简报之外单独查触发的入口）。
    第 37 轮：可选 session_id，传了才启用会话上下文时机（暂缓与救援）；
    不传则行为与之前一致。"""
    u = bind_user(db, u)
    if not memory_enabled(db, u):
        return []
    fired = check_triggers(db, u, task, embedder=build_embedder(), project=project,
                           session_id=session_id)
    return [reminder_out(r) for r in fired]
