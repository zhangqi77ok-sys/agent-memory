from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    url = url or settings.database_url
    kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
    return create_engine(url, **kw)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
# Worker 跨用户处理任务：用独立的 BYPASSRLS 账号，而不是让应用账号自己声明绕过
worker_engine = make_engine(settings.worker_database_url) if settings.worker_database_url else engine
WorkerSession = sessionmaker(bind=worker_engine, expire_on_commit=False)


@event.listens_for(Session, "after_begin")
def _set_rls_context(session, transaction, connection):
    """每个事务开头把当前用户写进 Postgres 会话变量，供行级权限策略使用（事务级，不会串到连接池里的下一个请求）。"""
    if connection.dialect.name != "postgresql":
        return
    if uid := session.info.get("user_id"):
        connection.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": uid})


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
