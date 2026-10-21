from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.me import router as me_router
from app.api.memories import router as memories_router
from app.auth import is_valid_service_key
from app.config import Settings, check_settings, settings
from app.db import engine


def create_app(s: Settings = settings) -> FastAPI:
    check_settings(s)
    docs_on = s.docs_enabled if s.docs_enabled is not None else not s.is_prod
    # 反向代理前缀（如 /memory）由 uvicorn --root-path 传入，openapi.json 地址随之正确
    application = FastAPI(
        title="agent-memory",
        version="0.1.0",
        docs_url="/docs" if docs_on else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_on else None,
    )

    @application.middleware("http")
    async def verify_service_key(request: Request, call_next):
        path = request.scope.get("path") or request.url.path
        path = path.rstrip("/") or "/"
        if path.startswith("/memory/"):
            path = path[len("/memory"):]
        # 健康检查与文档接口不需要服务密钥
        if path in ("/healthz", "/readyz", "/openapi.json") or path.startswith("/docs"):
            return await call_next(request)

        key = request.headers.get("x-service-key")
        if not is_valid_service_key(key, s):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)

    application.include_router(memories_router)
    application.include_router(me_router)

    @application.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @application.get("/readyz")
    def readyz():
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return {"status": "ready"}

    return application


app = create_app()
