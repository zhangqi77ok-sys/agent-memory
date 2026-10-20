from fastapi import FastAPI
from sqlalchemy import text

from app.api.me import router as me_router
from app.api.memories import router as memories_router
from app.config import check_settings, settings
from app.db import engine

check_settings()

_docs = settings.docs_enabled if settings.docs_enabled is not None else settings.app_env != "prod"
# 反向代理前缀（如 /memory）由 uvicorn --root-path 传入，openapi.json 地址随之正确
app = FastAPI(title="agent-memory", version="0.1.0",
              docs_url="/docs" if _docs else None, redoc_url=None,
              openapi_url="/openapi.json" if _docs else None)
app.include_router(memories_router)
app.include_router(me_router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    with engine.connect() as c:
        c.execute(text("SELECT 1"))
    return {"status": "ready"}
