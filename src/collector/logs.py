"""Logging setup that keeps secrets out of logs.

httpx logs every request URL at INFO, and Instagram calls carry access_token and
client_secret in the query string; uvicorn's access log prints the OAuth code/state and
the webhook verify token. Request logging from httpx is raised to WARNING, and a filter
redacts those query parameters from any record that still mentions them.
"""

import logging
import re

_SENSITIVE = re.compile(
    r"((?:[?&]|\b)(?:access_token|client_secret|hub[._]verify_token)=|[?&](?:code|state)=)"
    r"[^&\s\"']+"
)
_FILTERED_LOGGERS = ("uvicorn.access", "uvicorn.error", "httpx", "httpcore")


def redact(text: str) -> str:
    return _SENSITIVE.sub(r"\1[redacted]", text)


def _redact_arg(value: object) -> object:
    # httpx passes the URL as an httpx.URL object, not a str.
    text = str(value)
    cleaned = redact(text)
    return cleaned if cleaned != text else value


class RedactFilter(logging.Filter):
    """Redacts in place, keeping the record's msg/args shape: uvicorn's access formatter
    unpacks record.args, so collapsing them into a pre-formatted message breaks it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        return True


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(levelname)s:     %(name)s %(message)s")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    redactor = RedactFilter()
    # Logger filters only see records logged on that logger; handler filters see
    # everything that reaches the handler, including propagated records.
    for name in _FILTERED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactFilter) for f in logger.filters):
            logger.addFilter(redactor)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactFilter) for f in handler.filters):
            handler.addFilter(redactor)
