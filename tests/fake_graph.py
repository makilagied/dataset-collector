"""A tiny in-memory stand-in for graph.instagram.com, shaped like recorded responses."""

from typing import Any
from urllib.parse import urlencode

import httpx

BASE = "https://graph.instagram.com/v25.0"
LEAKY = "leaky_user"  # a commenter username that must never be persisted


def comment(
    cid: str, text: str, ts: str = "2026-09-02T10:00:00+0000", **extra: Any
) -> dict[str, Any]:
    return {
        "id": cid,
        "text": text,
        "timestamp": ts,
        "from": {"id": f"igsid-{cid}", "username": LEAKY},
        "username": LEAKY,
        "like_count": 1,
        "hidden": False,
        **extra,
    }


class FakeGraph:
    """Routes GETs by path and `after` cursor. Set `fail_on` to make a page return 500."""

    def __init__(self) -> None:
        self.media = [
            {
                "id": "M1",
                "caption": "habari",
                "media_type": "IMAGE",
                "permalink": "https://x/p/1",
                "timestamp": "2026-09-01T08:00:00+0000",
                "comments_count": 7,
            },
            {
                "id": "M2",
                "caption": "old",
                "media_type": "IMAGE",
                "permalink": "https://x/p/2",
                "timestamp": "2026-01-01T08:00:00+0000",
                "comments_count": 1,
            },
        ]
        r1 = comment("r1", "jibu la kwanza", parent_id="c1")
        r2 = comment("r2", "jibu la pili", parent_id="c1")
        c1 = comment(
            "c1",
            "wewe ni mjinga",
            replies={
                "data": [r1],
                "paging": {"next": self._url("c1/replies", after="rr1")},
            },
        )
        # Three pages of comments for M1, one page for M2.
        self.comment_pages: dict[str, list[list[dict[str, Any]]]] = {
            "M1": [
                [c1, comment("c2", "safi sana")],
                [comment("c3", "@" + LEAKY + " acha"), comment("c4", "poa")],
                [comment("c5", "mwisho")],
            ],
            "M2": [[comment("c6", "zamani")]],
        }
        self.replies = {"c1": [r1, r2]}
        self.fail_on: set[tuple[str, str | None]] = set()
        self.requests: list[httpx.Request] = []

    def _url(self, path: str, **params: str) -> str:
        return f"{BASE}/{path}?{urlencode({**params, 'access_token': 'TOKEN', 'limit': '50'})}"

    def _paged(
        self, path: str, pages: list[list[dict[str, Any]]], after: str | None
    ) -> dict[str, Any]:
        idx = int(after.removeprefix("p")) if after else 0
        body: dict[str, Any] = {"data": pages[idx]}
        if idx + 1 < len(pages):
            body["paging"] = {"next": self._url(path, after=f"p{idx + 1}")}
        return body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v25.0/")
        after = request.url.params.get("after")
        if (path, after) in self.fail_on:
            return httpx.Response(500, json={"error": {"message": "boom", "code": 1}})
        if path == "me/media":
            return httpx.Response(200, json={"data": self.media})
        if path.endswith("/comments"):
            return httpx.Response(200, json=self._paged(path, self.comment_pages[path[:-9]], after))
        if path.endswith("/replies"):
            return httpx.Response(200, json={"data": self.replies[path[:-8]]})
        return httpx.Response(404, json={"error": {"message": f"unknown {path}", "code": 100}})
