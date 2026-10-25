"""Riding out network outages: when the connection drops, wait for it to come back and
carry on, instead of failing the request (and with it the whole backfill or scrape).

"Offline" means the host we were talking to can't be reached at all (DNS or TCP connect
fails). An error while that host is still reachable is a real error and is raised as before.
"""

import logging
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

POLL_S = 10.0
# Chromium / fetch() errors that mean the request never got through.
NETWORK_MARKERS = (
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_NAME_RESOLUTION_FAILED",
    "ERR_NETWORK_CHANGED",
    "ERR_NETWORK_IO_SUSPENDED",
    "ERR_ADDRESS_UNREACHABLE",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_FAILED",
    "ERR_TIMED_OUT",
    "ERR_PROXY_CONNECTION_FAILED",
    "Failed to fetch",
    "NetworkError",
)

_local = threading.local()  # per-thread notifier, set by the job runner


def set_notifier(notify: Callable[[str], None] | None) -> None:
    """Where to tell the user that we're waiting for the network (the running job)."""
    _local.notify = notify


def notify(text: str) -> None:
    """Show `text` on the running job, if any."""
    notify = getattr(_local, "notify", None)
    if notify is not None:
        try:
            notify(text)
        except Exception:  # status display must never break the wait
            log.debug("network notifier failed", exc_info=True)


def online(host: str, port: int = 443, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_network(
    host: str,
    *,
    poll: float = POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    check: Callable[[str], bool] | None = None,
) -> bool:
    """Block until `host` is reachable, polling every `poll` seconds, with no time limit.
    Returns True if it had to wait."""
    check = check or online
    if check(host):
        return False
    since = datetime.now()
    log.warning("network down (%s unreachable); waiting for it to come back", host)
    while True:
        notify(f"network down since {since:%H:%M:%S}: waiting for {host} to be reachable…")
        sleep(poll)
        if check(host):
            break
    log.warning("network back after %s", str(datetime.now() - since).split(".")[0])
    notify("network is back, continuing…")
    return True


def is_network_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    text = str(exc)
    return any(marker in text for marker in NETWORK_MARKERS)


def call[T](
    fn: Callable[[], T],
    host: str,
    *,
    attempts: int = 3,
    pause: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    check: Callable[[str], bool] | None = None,
) -> T:
    """Run `fn`; on a network error wait (as long as it takes) for `host` and run it again.
    A network error while `host` is reachable is retried `attempts - 1` times, then raised."""
    failures = 0
    while True:
        try:
            return fn()
        except Exception as e:
            if not is_network_error(e):
                raise
            if not wait_for_network(host, sleep=sleep, check=check):
                failures += 1
                if failures >= attempts:
                    raise
                sleep(pause)


def host_of(url: str) -> str:
    return urlsplit(url).hostname or url


class WaitingTransport(httpx.BaseTransport):
    """httpx transport that waits out network outages instead of raising."""

    def __init__(self, inner: httpx.BaseTransport | None = None, **options: Any):
        self._inner = inner or httpx.HTTPTransport()
        self._options = options

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        while True:
            try:
                return self._inner.handle_request(request)
            except httpx.TransportError:
                # Reachable: a real error (timeout, reset); let the caller's retries decide.
                if not wait_for_network(host, **self._options):
                    raise

    def close(self) -> None:
        self._inner.close()


def http_client(timeout: float = 30, **kwargs: Any) -> httpx.Client:
    """An httpx.Client whose requests survive the network going away."""
    return httpx.Client(timeout=timeout, transport=WaitingTransport(), **kwargs)
