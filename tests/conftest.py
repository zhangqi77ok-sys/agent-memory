import os

# 设置 TEST_DATABASE_URL 时在真实 Postgres(+pgvector) 上跑，并走 Alembic 迁移
PG_URL = os.environ.get("TEST_DATABASE_URL")
os.environ["DATABASE_URL"] = PG_URL or "sqlite:///./test.db"

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    if PG_URL:
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import text

        with engine.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        command.upgrade(Config("alembic.ini"), "head")
    else:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


def token(user: str, app_id: str = "tiancode") -> dict:
    t = jwt.encode({"sub": user, "app": app_id}, settings.jwt_secret, algorithm=settings.jwt_alg)
    return {"Authorization": f"Bearer {t}"}
