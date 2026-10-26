import os

# TEST_DATABASE_URL（owner/管理员账号）存在时在真实 Postgres(+pgvector) 上跑：
# 迁移用管理员账号，服务用非超级用户 memsvc_app 账号，行级权限真实生效。
PG_ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
APP_PW = "memsvc_app_test"
WORKER_PW = "memsvc_worker_test"
if PG_ADMIN_URL:
    _u = PG_ADMIN_URL.split("://", 1)
    _host = _u[1].split("@", 1)[1]
    os.environ["DATABASE_URL"] = f"{_u[0]}://memsvc_app:{APP_PW}@{_host}"
    os.environ["WORKER_DATABASE_URL"] = f"{_u[0]}://memsvc_worker:{WORKER_PW}@{_host}"
    os.environ["MIGRATE_DATABASE_URL"] = PG_ADMIN_URL
else:
    os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SERVICE_KEYS", "test-service-key,test-service-key-2")
# 修复第 1 轮：存量套件历来就是单用户断言模式（未绑定测试密钥 + ?user_id=/body 断言），
# 在此显式声明测试态放行断言；默认拒绝口径由 tests/test_fix1_key_binding.py 专测，
# 不改几百处旧测试的调用方式。
os.environ.setdefault("ALLOW_UNBOUND_ASSERTION", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

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
        from app.db import worker_engine
        worker_engine.dispose()
        # db fixture 走 admin_engine：上轮 DROP SCHEMA CASCADE 会删掉
        # vector 扩展再重建（类型 OID 变化），池里残留的连接若带着旧
        # OID 的参数类型缓存，下一次 INSERT 会报 cache lookup failed。
        # 其余引擎已 dispose，admin_engine 同样要清，否则是时序性
        # 随机失败（PG 全量偶发一例属此机制）。
        admin_engine.dispose()
        with admin_engine.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
            c.execute(text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='memsvc_app') THEN "
                "CREATE ROLE memsvc_app; END IF; END $$"))
            c.execute(text(f"ALTER ROLE memsvc_app LOGIN PASSWORD '{APP_PW}' NOSUPERUSER NOBYPASSRLS"))
            c.execute(text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='memsvc_worker') THEN "
                "CREATE ROLE memsvc_worker; END IF; END $$"))
            c.execute(text(f"ALTER ROLE memsvc_worker LOGIN PASSWORD '{WORKER_PW}' NOSUPERUSER BYPASSRLS"))
        command.upgrade(Config("alembic.ini"), "head")
    else:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    return TestClient(app, headers={"X-Service-Key": "test-service-key"})


@pytest.fixture
def db():
    """造数用管理员账号（不受 RLS 约束）。"""
    s = AdminSession()
    yield s
    s.close()


def uid(user: str) -> dict:
    """查询参数形式的身份（GET/DELETE 用）。业务侧自己验完会话后直接传 user_id。"""
    return {"user_id": user}


def consent(client, *users: str) -> None:
    """模拟用户在授权页点了同意并开启记忆（未同意时写入/召回都不生效）。"""
    from app.schemas import CONSENT_VERSION
    for u in users:
        r = client.put("/v1/me/settings",
                       json={"user_id": u, "memory_enabled": True, "consent_version": CONSENT_VERSION})
        assert r.status_code == 200, r.text
