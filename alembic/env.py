from alembic import context

from collector.db import make_engine
from collector.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    with make_engine().connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
