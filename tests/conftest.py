import os
from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL", "postgresql+psycopg://collector:collector@localhost:5432/collector"
)
TEST_DB = "collector_test"
TEST_DATABASE_URL = ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB}"

TEST_ENV = {
    "DATABASE_URL": TEST_DATABASE_URL,
    "IG_APP_ID": "test-app",
    "IG_APP_SECRET": "test-secret",
    "IG_REDIRECT_URI": "https://example.test/oauth/callback",
    "IG_WEBHOOK_VERIFY_TOKEN": "verify-me",
    "AUTHOR_HASH_KEY": "test-hmac-key",
    "GRAPH_API_VERSION": "v25.0",
    # Pin optional settings too, so a developer's .env never leaks into tests
    # (environment variables take precedence over .env).
    "FB_APP_SECRET": "",
    "ADMIN_USER": "admin",
    "ADMIN_PASSWORD": "",
    "ADMIN_ALLOW_REMOTE": "false",
    # Most tests store innocent sample comments; test_store_only_hits.py turns this on.
    "STORE_ONLY_HITS": "false",
}


# Lines tests want shown at the end of the run (e.g. matcher precision/recall).
REPORT: list[str] = []


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if REPORT:
        terminalreporter.section("metrics")
        for line in REPORT:
            terminalreporter.write_line(line)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    from collector.config import get_settings

    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("TOKEN_ENC_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """Fresh schema in a dedicated test database (needs `docker compose up -d db`)."""
    from collector.models import Base

    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB}
            )
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    except OperationalError:
        pytest.skip("Postgres not reachable; run `docker compose up -d db`")
    finally:
        admin.dispose()

    eng = create_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    from collector.models import Base

    session = sessionmaker(engine, expire_on_commit=False)()
    yield session
    session.close()
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
