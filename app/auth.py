import hmac
from dataclasses import dataclass

from fastapi import Query
from sqlalchemy.orm import Session

from app.config import Settings, settings


def is_valid_service_key(provided_key: str | None, s: Settings | None = None) -> bool:
    """校验服务密钥请求头 X-Service-Key。支持多个值轮换，常量时间比较。"""
    if not provided_key:
        return False
    conf = s or settings
    keys = conf.service_key_list
    if not keys:
        return False
    valid = False
    for k in keys:
        if hmac.compare_digest(provided_key, k):
            valid = True
    return valid


def is_valid_admin_key(provided_key: str | None, s: Settings | None = None) -> bool:
    """校验管理端密钥请求头 X-Admin-Key（第十七轮）。与服务密钥同一纪律：
    多值轮换、常量时间比较；未配置任何管理密钥时恒 False（管理端关闭）。"""
    if not provided_key:
        return False
    conf = s or settings
    keys = conf.admin_key_list
    if not keys:
        return False
    valid = False
    for k in keys:
        if hmac.compare_digest(provided_key, k):
            valid = True
    return valid


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
