import hmac
from dataclasses import dataclass

from fastapi import HTTPException, Query, Request
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


def bound_user(provided_key: str | None, s: Settings | None = None) -> str | None:
    """服务密钥绑定的用户（第二十五轮）：key 在 SERVICE_KEY_USERS 映射里时
    返回映射用户，否则 None。常量时间比较；映射本身不授权限——调用方须先
    过 is_valid_service_key（中间件 401 在前）。"""
    if not provided_key:
        return None
    conf = s or settings
    for key, user in conf.service_key_user_map.items():
        if hmac.compare_digest(provided_key, key):
            return user
    return None


def resolve_asserted(request, asserted_user_id: str) -> str:
    """body 身份的绑定口径：中间件已算好的 bound_user 优先，否则用断言值。
    与 user_from_query 同规则——绑定部署里 body 里的 user_id 同样失效。
    request 可能为 None（端点被直接调用、未走中间件）：此时无绑定，断言生效。"""
    bound = getattr(getattr(request, "state", None), "bound_user", None)
    return bound if bound else asserted_user_id


def user_from_query(request: Request,
                    user_id: str | None = Query(default=None, min_length=1, max_length=64),
                    ) -> UserContext:
    """查询身份的绑定口径：绑定密钥直接定身份（?user_id= 缺省也不 422）；
    未绑定时沿用查询 user_id，缺失仍 422（与 docs/api.md 的约定一致）。
    兼容旧直接调用 user_from_query("alice")（首参即 user_id）；HTTP 依赖注入
    恒传真实 Request，不走这条。"""
    if isinstance(request, str) and user_id is None:
        return user_ctx(request)
    bound = getattr(getattr(request, "state", None), "bound_user", None)
    if bound:
        return user_ctx(bound)
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id is required")
    return user_ctx(user_id)


def bind_user(db: Session, u: UserContext) -> UserContext:
    """必须在本会话第一条 SQL 之前调用：after_begin 钩子据此设置 app.user_id（RLS）。"""
    db.info["user_id"] = u.user_id
    return u
