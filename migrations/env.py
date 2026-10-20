from alembic import context

import app.models  # noqa: F401
from app.config import settings
from app.db import Base, make_engine

target_metadata = Base.metadata


def run_migrations_online():
    engine = make_engine(settings.migrate_database_url or settings.database_url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
