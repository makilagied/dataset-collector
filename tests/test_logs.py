import logging
from collections.abc import Iterator

import pytest
import respx
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from collector.logs import RedactFilter, configure_logging, redact
from collector.models import Account
from collector.security import encrypt_token
from tests.fake_graph import FakeGraph


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (
            "GET https://graph.instagram.com/v25.0/me?fields=id&access_token=IGAAT123 200",
            "GET https://graph.instagram.com/v25.0/me?fields=id&access_token=[redacted] 200",
        ),
        (
            "GET /access_token?grant_type=x&client_secret=abc123&access_token=t",
            "GET /access_token?grant_type=x&client_secret=[redacted]&access_token=[redacted]",
        ),
        (
            '"GET /oauth/callback?code=AQLe-x_1&state=eyJ.sig HTTP/1.1" 200',
            '"GET /oauth/callback?code=[redacted]&state=[redacted] HTTP/1.1" 200',
        ),
        (
            "GET /webhook?hub.mode=subscribe&hub.verify_token=s3cret&hub.challenge=1",
            "GET /webhook?hub.mode=subscribe&hub.verify_token=[redacted]&hub.challenge=1",
        ),
        (
            "GET /me/media: HTTP 400 code=100 bad field",  # error codes are not secrets
            "GET /me/media: HTTP 400 code=100 bad field",
        ),
    ],
)
def test_redact(raw: str, clean: str) -> None:
    assert redact(raw) == clean


def test_filter_rewrites_formatted_record() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s"',
        ("1.2.3.4", "GET", "/oauth/callback?code=SECRET&state=S"),
        None,
    )
    assert RedactFilter().filter(record)
    assert "SECRET" not in record.getMessage()
    assert "[redacted]" in record.getMessage()


def test_uvicorn_access_formatter_still_works() -> None:
    """Regression: the filter must keep record.args as uvicorn's 5-tuple."""
    from uvicorn.logging import AccessFormatter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/oauth/callback?code=SECRET&state=S", "1.1", 400),
        None,
    )
    RedactFilter().filter(record)
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(record)
    assert line == (
        '1.2.3.4:5 - "GET /oauth/callback?code=[redacted]&state=[redacted] HTTP/1.1" '
        "400 Bad Request"
    )


def test_httpx_url_object_redacted() -> None:
    import httpx

    url = httpx.URL("https://graph.instagram.com/me?access_token=SECRET")
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, "HTTP Request: %s %s", ("GET", url), None
    )
    RedactFilter().filter(record)
    assert "SECRET" not in record.getMessage()


def test_configure_logging_quiets_httpx() -> None:
    configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert any(isinstance(f, RedactFilter) for f in logging.getLogger("uvicorn.access").filters)


@pytest.fixture
def graph() -> Iterator[FakeGraph]:
    fake = FakeGraph()
    with respx.mock:
        respx.get(url__startswith="https://graph.instagram.com/").mock(side_effect=fake)
        yield fake


def test_cli_backfill_never_logs_token(
    db: Session, graph: FakeGraph, caplog: pytest.LogCaptureFixture
) -> None:
    from collector.cli import app

    acct = Account(
        ig_user_id="1", username="a", access_token_enc=encrypt_token("TOKEN-XYZ"), active=True
    )
    db.add(acct)
    db.commit()
    caplog.set_level(logging.DEBUG)

    result = CliRunner().invoke(app, ["backfill", "--account", str(acct.id)])

    assert result.exit_code == 0, result.output
    assert graph.requests  # the API really was called with the token...
    assert "TOKEN-XYZ" not in caplog.text  # ...but it never reached the logs
    assert "TOKEN-XYZ" not in result.output
