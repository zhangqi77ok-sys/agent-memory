"""身份由业务侧传入：记忆服务不解析 JWT、不做登录。app_id 来自配置，调用方不能切换。"""
from dataclasses import dataclass

from fastapi import Query
from sqlalchemy.orm import Session

from app.config import settings


@dataclass(frozen=True)
class UserContext:
    app_id: str
    user_id: str


def user_ctx(user_id: str) -> UserContext:
    return UserContext(app_id=settings.app_id, user_id=user_id)


def user_from_query(user_id: str = Query(..., min_length=1, max_length=64)) -> UserContext:
    return user_ctx(user_id)


def bind_user(db: Session, u: UserContext) -> UserContext:
    """必须在本会话第一条 SQL 之前调用：after_begin 钩子据此设置 app.user_id（RLS）。"""
    db.info["user_id"] = u.user_id
    return u
