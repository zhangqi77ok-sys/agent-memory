"""前瞻记忆的 REST 入口（第十七轮）：武装、列出、解除、按任务查触发。

与其它 /v1 入口同一纪律：未同意/已暂停时静默拒绝（不落数据），
解除只认本人名下（他人/不存在一律 404，不泄露存在性）。
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.me import memory_enabled
from app.auth import bind_user, user_ctx
from app.db import get_db
from app.embeddings import build_embedder
from app.reminders import check_triggers, create_reminder, dismiss_reminder, list_reminders
from app.schemas import ReminderCreateRequest, ReminderDismissRequest, ReminderOut, reminder_out

router = APIRouter(prefix="/v1")


@router.post("/reminders", status_code=status.HTTP_201_CREATED, response_model=ReminderOut)
def create(req: ReminderCreateRequest, db: Session = Depends(get_db)):
    u = bind_user(db, user_ctx(req.user_id))
    if not memory_enabled(db, u):
        raise HTTPException(status_code=404, detail="reminder not found")
    row = create_reminder(db, u, condition=req.condition, action=req.action,
                          project=req.project, once=req.once,
                          embedder=build_embedder())
    return reminder_out(row)


@router.get("/reminders", response_model=list[ReminderOut])
def list_(user_id: str = Query(..., min_length=1, max_length=64),
          include_closed: bool = Query(False),
          db: Session = Depends(get_db)):
    u = bind_user(db, user_ctx(user_id))
    if not memory_enabled(db, u):
        return []
    return [reminder_out(r) for r in list_reminders(db, u, include_closed=include_closed)]


@router.post("/reminders/{reminder_id}/dismiss", response_model=ReminderOut)
def dismiss(reminder_id: str, req: ReminderDismissRequest,
            db: Session = Depends(get_db)):
    u = bind_user(db, user_ctx(req.user_id))
    if not memory_enabled(db, u):
        raise HTTPException(status_code=404, detail="reminder not found")
    try:
        row = dismiss_reminder(db, u, reminder_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="reminder not found") from None
    return reminder_out(row)


@router.get("/reminders/check", response_model=list[ReminderOut])
def check(user_id: str = Query(..., min_length=1, max_length=64),
          task: str = Query(..., min_length=1, max_length=2000),
          project: str | None = Query(None, max_length=128),
          db: Session = Depends(get_db)):
    """拿一段任务描述去撞武装中的前瞻记忆（简报之外单独查触发的入口）。"""
    u = bind_user(db, user_ctx(user_id))
    if not memory_enabled(db, u):
        return []
    fired = check_triggers(db, u, task, embedder=build_embedder(), project=project)
    return [reminder_out(r) for r in fired]
