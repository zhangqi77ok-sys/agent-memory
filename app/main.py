from fastapi import FastAPI
from sqlalchemy import text

from app.api.me import router as me_router
from app.api.memories import router as memories_router
from app.config import check_settings
from app.db import engine

check_settings()

app = FastAPI(title="agent-memory", version="0.1.0")
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
