from alembic import context

import app.models  # noqa: F401
from app.config import settings
from app.db import Base, make_engine

target_metadata = Base.metadata


def run_migrations_online():
    url = settings.migrate_database_url or settings.database_url
    engine = make_engine(url)
    # 打印目标（隐藏密码），卡住时能看出连的是哪里；connect_timeout=10 保证连不上就报错而不是挂死
    print(f"[alembic] connecting to {engine.url.render_as_string(hide_password=True)}", flush=True)
    with engine.connect() as conn:
        if conn.dialect.name == "postgresql":
            conn.exec_driver_sql("SET lock_timeout = '30s'")
            conn.commit()  # SQLAlchemy 2 会自动开事务；不提交的话下面的迁移事务不会落库
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
