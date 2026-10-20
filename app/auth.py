from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException

from app.config import settings


@dataclass(frozen=True)
class UserContext:
    tenant_id: str
    user_id: str


def current_user(authorization: str = Header(default="")) -> UserContext:
    """身份只来自 JWT：sub=user_id，app=tenant_id（C 端默认 tiancode）。"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    try:
        claims = jwt.decode(authorization[7:], settings.jwt_secret, algorithms=[settings.jwt_alg])
    except jwt.PyJWTError as e:
        raise HTTPException(401, "invalid token") from e
    if not claims.get("sub"):
        raise HTTPException(401, "token without sub")
    return UserContext(tenant_id=claims.get("app", "tiancode"), user_id=str(claims["sub"]))
