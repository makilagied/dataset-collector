from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from collector.config import get_settings


def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or get_settings().database_url, pool_pre_ping=True)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    session = sessionmaker(engine or make_engine(), expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
