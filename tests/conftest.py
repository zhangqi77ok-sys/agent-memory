import os

os.environ["DATABASE_URL"] = "sqlite:///./test.db"

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db():
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
