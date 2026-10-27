import json

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.admin import router as admin_router
from app.api.episodes import router as episodes_router
from app.api.me import router as me_router
from app.api.media import router as media_router
from app.api.memories import router as memories_router
from app.api.reminders import router as reminders_router
from app.auth import bound_user, is_valid_service_key
from app.config import Settings, check_settings, settings
from app.db import engine
from app.metrics import metrics_collector
from app.ratelimit import UserRateLimiter
from app.redact import setup_logging_filters


def create_app(s: Settings = settings) -> FastAPI:
    check_settings(s)
    setup_logging_filters()
    docs_on = s.docs_enabled if s.docs_enabled is not None else not s.is_prod
    # 反向代理前缀（如 /memory）由 uvicorn --root-path 传入，openapi.json 地址随之正确
    application = FastAPI(
        title="BrainWeave Memory",
        version="0.1.0",
        docs_url="/docs" if docs_on else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_on else None,
    )

    rate_limiter = UserRateLimiter(s)

    @application.middleware("http")
    async def request_middleware(request: Request, call_next):
        path = request.scope.get("path") or request.url.path
        path = path.rstrip("/") or "/"
        if path.startswith("/memory/"):
            path = path[len("/memory"):]

        # 健康检查、探针、监控与文档接口不需要服务密钥与限流
        is_exempt = path in ("/healthz", "/readyz", "/metrics", "/openapi.json") or path.startswith("/docs")

        if not is_exempt:
            key = request.headers.get("x-service-key")
            if not is_valid_service_key(key, s):
                metrics_collector.record_request(request.method, path, 401)
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
            # 第二十五轮身份绑定：密钥映射到用户时，该请求后续的身份解析
            # 一律以绑定用户为准（user_from_query / body 端点）；未绑定为 None。
            request.state.bound_user = bound_user(key, s)
            # 修复第 1 轮：把该应用的断言放行开关同样盖到请求上，身份函数
            # （user_from_query / resolve_asserted）据此执行默认拒绝口径；
            # 不经模块级 settings，避免 create_app(Settings(...)) 测试应用走错口径。
            request.state.allow_unbound_assertion = s.allow_unbound_assertion

            # 应用层 user_id 限流（绑定用户优先，否则账算到被断言的人头上）
            if s.is_rate_limit_enabled and path.startswith("/v1/"):
                user_id = request.state.bound_user or request.query_params.get("user_id")
                if not user_id and request.method in ("POST", "PUT", "PATCH"):
                    try:
                        body_bytes = await request.body()

                        async def receive():
                            return {"type": "http.request", "body": body_bytes}

                        request._receive = receive
                        if body_bytes:
                            data = json.loads(body_bytes)
                            if isinstance(data, dict):
                                user_id = data.get("user_id")
                    except Exception:
                        pass

                if user_id:
                    allowed, retry_after = rate_limiter.check(user_id, path, request.method)
                    if not allowed:
                        metrics_collector.record_request(request.method, path, 429)
                        return JSONResponse(
                            status_code=429,
                            content={"detail": "Too Many Requests"},
                            headers={"Retry-After": str(int(retry_after))},
                        )

        response = await call_next(request)
        metrics_collector.record_request(request.method, path, response.status_code)
        return response

    application.include_router(memories_router)
    application.include_router(episodes_router)
    application.include_router(media_router)
    application.include_router(me_router)
    application.include_router(reminders_router)
    application.include_router(admin_router)

    @application.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @application.get("/readyz")
    def readyz():
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return {"status": "ready"}

    @application.get("/metrics")
    def metrics():
        content = metrics_collector.generate_metrics_text(engine)
        return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")

    return application


app = create_app()
