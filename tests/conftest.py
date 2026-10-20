import os

# TEST_DATABASE_URL（owner/管理员账号）存在时在真实 Postgres(+pgvector) 上跑：
# 迁移用管理员账号，服务用非超级用户 memsvc_app 账号，行级权限真实生效。
PG_ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
APP_PW = "memsvc_app_test"
if PG_ADMIN_URL:
    _u = PG_ADMIN_URL.split("://", 1)
    _host = _u[1].split("@", 1)[1]
    os.environ["DATABASE_URL"] = f"{_u[0]}://memsvc_app:{APP_PW}@{_host}"
    os.environ["MIGRATE_DATABASE_URL"] = PG_ADMIN_URL
else:
    os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ.setdefault("APP_ENV", "test")

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base, engine, make_engine
from app.main import app

admin_engine = make_engine(PG_ADMIN_URL) if PG_ADMIN_URL else engine
AdminSession = sessionmaker(bind=admin_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def fresh_db():
    if PG_ADMIN_URL:
        from alembic import command
        from alembic.config import Config

        engine.dispose()
        with admin_engine.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
            c.execute(text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='memsvc_app') THEN "
                "CREATE ROLE memsvc_app; END IF; END $$"))
            c.execute(text(f"ALTER ROLE memsvc_app LOGIN PASSWORD '{APP_PW}' NOSUPERUSER NOBYPASSRLS"))
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
    """造数用管理员账号（不受 RLS 约束）。"""
    s = AdminSession()
    yield s
    s.close()


def token(user: str, app_id: str = "tiancode") -> dict:
    t = jwt.encode({"sub": user, "app": app_id}, settings.jwt_secret, algorithm=settings.jwt_alg)
    return {"Authorization": f"Bearer {t}"}
