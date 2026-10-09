"""Paged GET wrapper for graph.instagram.com with retry/backoff and rate-limit awareness.

Rate-limit behaviour checked October 2026 against
https://developers.facebook.com/docs/graph-api/overview/rate-limiting
"""

import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt

log = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.instagram.com"
# App (4), user (17), page (32), custom (613) and Instagram BUC (80002) throttling.
THROTTLE_CODES = {4, 17, 32, 613, 80002}
# Slow down proactively once any usage metric in the rate-limit headers passes this percentage.
USAGE_SLOWDOWN_PCT = 90
MAX_WAIT_S = 3600


class GraphError(Exception):
    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class RetryableError(GraphError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        code: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message, status, code)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Page:
    data: list[dict[str, Any]]
    next_url: str | None  # access_token stripped; safe to persist


def strip_token(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "access_token"
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _usage_pct_and_wait(headers: httpx.Headers) -> tuple[float, float | None]:
    """Highest usage percentage across rate-limit headers, and the regain hint in seconds."""
    pct, wait = 0.0, None
    for name in ("x-business-use-case-usage", "x-app-usage"):
        raw = headers.get(name)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        # BUC header: {business_id: [{...}]}; app header: {...}
        if name == "x-app-usage":
            entries = [parsed]
        else:
            entries = [e for v in parsed.values() if isinstance(v, list) for e in v]
        for e in entries:
            if not isinstance(e, dict):
                continue
            for key in ("call_count", "total_cputime", "total_time"):
                pct = max(pct, float(e.get(key) or 0))
            minutes = e.get("estimated_time_to_regain_access")
            if minutes:
                wait = max(wait or 0.0, float(minutes) * 60)
    return pct, wait


class GraphClient:
    def __init__(
        self,
        access_token: str,
        *,
        http: httpx.Client,
        version: str,
        max_attempts: int = 6,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._token = access_token
        self._http = http
        self._base = f"{GRAPH_HOST}/{version}"
        self._max_attempts = max_attempts
        self._sleep = sleep

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, RetryableError) and exc.retry_after is not None:
            return min(exc.retry_after, MAX_WAIT_S)
        return float(min(2**state.attempt_number, 60))

    def _request(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        # Merge explicitly: passing params= to httpx replaces a URL's existing query,
        # which would drop the paging cursor from a `next` URL.
        full = httpx.URL(url).copy_merge_params({**(params or {}), "access_token": self._token})
        try:
            resp = self._http.get(full)
        except httpx.TransportError as e:
            raise RetryableError(f"transport error: {type(e).__name__}") from e

        pct, regain = _usage_pct_and_wait(resp.headers)
        path = urlsplit(url).path  # never log the query: it carries the token

        if resp.status_code == 200:
            if pct >= USAGE_SLOWDOWN_PCT:
                delay = min(regain or 60.0, MAX_WAIT_S)
                log.warning("rate-limit usage at %.0f%%, sleeping %.0fs", pct, delay)
                self._sleep(delay)
            data: dict[str, Any] = resp.json()
            return data

        error: dict[str, Any] = {}
        try:
            error = resp.json().get("error", {}) or {}
        except ValueError:
            pass
        code = error.get("code")
        msg = f"GET {path}: HTTP {resp.status_code} code={code} {error.get('message', '')}".strip()

        if resp.status_code == 429 or resp.status_code >= 500 or code in THROTTLE_CODES:
            retry_after = resp.headers.get("retry-after")
            hint = float(retry_after) if retry_after and retry_after.isdigit() else regain
            if code in THROTTLE_CODES and hint is None:
                hint = 60.0
            raise RetryableError(msg, resp.status_code, code, retry_after=hint)
        raise GraphError(msg, resp.status_code, code)

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{self._base}/{path_or_url}"
        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._wait,
            retry=retry_if_exception_type(RetryableError),
            sleep=self._sleep,
            reraise=True,
        )
        result: dict[str, Any] = retrying(self._request, url, params)
        return result

    def pages(
        self, path: str, params: dict[str, Any] | None = None, *, resume_url: str | None = None
    ) -> Iterator[Page]:
        """Yield pages, following paging.next. `resume_url` continues from a saved cursor."""
        if resume_url:
            body = self.get(resume_url)
        else:
            body = self.get(path, params)
        while True:
            raw_next = (body.get("paging") or {}).get("next")
            next_url = strip_token(raw_next) if raw_next else None
            yield Page(data=list(body.get("data") or []), next_url=next_url)
            if not next_url:
                return
            body = self.get(next_url)
