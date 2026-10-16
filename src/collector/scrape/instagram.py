"""Instagram scraper: calls the web app's own JSON endpoints from a logged-in browser page.

These endpoints are undocumented and change without notice; everything that knows their
shape lives in this module. Comments, post info and hashtags come from v1 endpoints via a
`Fetch` callable (path -> parsed JSON). A profile's post list has no working v1 endpoint
any more (checked 2026-10-14), so a `ListPosts` callable opens the profile page and reads
the posts out of the GraphQL responses the page receives while scrolling. Both are
callables so tests can swap the browser for fakes.

Targets: "@user" / "user" / profile URL, "#tag" / tag URL, or a post/reel URL.
"""

import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, urlparse

from sqlalchemy.orm import Session

from collector.models import Media
from collector.scrape.common import (
    BlockedError,
    Pacer,
    ScrapeError,
    ScrapeStats,
    Store,
    ensure_account,
    ts_iso,
)

log = logging.getLogger(__name__)

BASE = "https://www.instagram.com"
APP_ID = "936619743392459"  # the public web client id the site sends itself
SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
MEDIA_TYPES = {1: "IMAGE", 2: "VIDEO", 8: "CAROUSEL_ALBUM"}
BLOCK_MESSAGES = (
    "login_required",
    "checkpoint_required",
    "challenge_required",
    "feedback_required",
    "please wait a few minutes",
)
MAX_PAGES = 500  # hard stop for any one cursor loop

Fetch = Callable[[str], dict[str, Any]]
# (username, enough) -> posts newest first; stops loading once enough(posts_so_far) is true.
ListPosts = Callable[[str, Callable[[list[dict[str, Any]]], bool]], list[dict[str, Any]]]
PINNED_MAX = 3  # pinned posts lead a profile grid regardless of their age


@dataclass(frozen=True)
class Target:
    kind: str  # profile | hashtag | post
    value: str  # username, tag (no #), or shortcode


_RESERVED = {"explore", "accounts", "direct", "stories", "reels", "p", "reel", "tv"}


def parse_target(raw: str) -> Target:
    raw = raw.strip()
    if raw.startswith("#"):
        return Target("hashtag", raw[1:].lower())
    if raw.startswith("@"):
        return Target("profile", raw[1:].lower())
    if "instagram.com" not in raw:
        if re.fullmatch(r"[A-Za-z0-9._]+", raw):
            return Target("profile", raw.lower())
        raise ScrapeError(f"not an Instagram user, #tag or URL: {raw!r}")
    parts = [p for p in urlparse(raw if "//" in raw else "//" + raw).path.split("/") if p]
    if len(parts) >= 2 and parts[0] in ("p", "reel", "reels", "tv"):
        return Target("post", parts[1])
    if len(parts) >= 3 and parts[:2] == ["explore", "tags"]:
        return Target("hashtag", parts[2].lower())
    if len(parts) >= 3 and parts[1] in ("p", "reel"):  # /<user>/p/<code>/ form
        return Target("post", parts[2])
    if parts and parts[0] not in _RESERVED:
        return Target("profile", parts[0].lower())
    raise ScrapeError(f"unrecognized Instagram URL: {raw!r}")


def shortcode_to_pk(code: str) -> str:
    """Post shortcode -> numeric media id. Private-post links append extra chars; drop them."""
    pk = 0
    for ch in code[:11]:
        pk = pk * 64 + SHORTCODE_ALPHABET.index(ch)
    return str(pk)


def check_response(status: int, body: str) -> dict[str, Any]:
    """Parse an endpoint response, raising BlockedError for login walls and rate limits."""
    lowered = body[:2000].lower()
    if status == 429 or any(m in lowered for m in BLOCK_MESSAGES):
        raise BlockedError(f"Instagram blocked the request (HTTP {status}): {body[:200]!r}")
    if status in (401, 403):
        raise BlockedError(f"Instagram refused the request (HTTP {status}); log in again")
    if status == 404:
        raise ScrapeError("not found (deleted, private, or wrong name)")
    if status != 200:
        raise ScrapeError(f"HTTP {status}: {body[:200]!r}")
    try:
        data = json.loads(body)
    except ValueError as e:
        # HTML instead of JSON is how the login wall usually shows up.
        raise BlockedError("Instagram returned a web page instead of data; log in again") from e
    if not isinstance(data, dict):
        raise ScrapeError("unexpected response shape")
    return data


def media_item(m: dict[str, Any]) -> dict[str, Any]:
    caption = m.get("caption")
    return {
        "id": f"ig:{m['pk']}",
        "caption": caption.get("text") if isinstance(caption, dict) else None,
        "media_type": MEDIA_TYPES.get(m.get("media_type") or 0),
        "permalink": f"{BASE}/p/{m['code']}/" if m.get("code") else None,
        "timestamp": ts_iso(m.get("taken_at")),
        "comments_count": m.get("comment_count"),
    }


def comment_item(c: dict[str, Any], parent_pk: str | None = None) -> dict[str, Any]:
    user = c.get("user") or {}
    author = user.get("pk") or user.get("id") or c.get("user_id")
    return {
        "id": f"ig:{c['pk']}",
        "text": c.get("text") or "",
        "timestamp": ts_iso(c.get("created_at")),
        "like_count": c.get("comment_like_count"),
        "parent_id": f"ig:{parent_pk}" if parent_pk else None,
        # Only used to compute author_hash; sanitize() drops it before storage.
        "from": {"id": f"ig:{author}"} if author else {},
    }


def find_media(payload: Any) -> list[dict[str, Any]]:
    """Every post object anywhere in a response (hashtag pages nest them deeply)."""
    found: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "pk" in node and "code" in node and "taken_at" in node:
                found.setdefault(str(node["pk"]), node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return list(found.values())


class InstagramScraper:
    def __init__(
        self,
        session: Session,
        fetch: Fetch,
        list_posts: ListPosts,
        pacer: Pacer,
        *,
        max_posts: int = 20,
        max_comments: int = 5000,
        since: date | None = None,
        category: str | None = None,
        refresh: bool = False,
        progress: Callable[[str], None] | None = None,
        only_hits: bool | None = None,
    ):
        self.session = session
        self.fetch = fetch
        self.list_posts = list_posts
        self.pacer = pacer
        self.max_posts = max_posts
        self.max_comments = max_comments
        self.since = since
        self.category = category
        self.refresh = refresh
        self.only_hits = only_hits
        self.progress = progress

    def scrape(self, raw_target: str) -> ScrapeStats:
        target = parse_target(raw_target)
        stats = ScrapeStats()
        if target.kind == "profile":
            handle, posts = target.value, self._profile_posts(target.value)
        elif target.kind == "hashtag":
            handle, posts = "#" + target.value, self._hashtag_posts(target.value)
        else:
            post = self._post(target.value)
            owner = (post.get("user") or {}).get("username") or "posts"
            handle, posts = owner, iter([post])

        account = ensure_account(self.session, "instagram", handle, self.category)
        store = Store(
            self.session, account, stats, self.refresh, self.progress, self.only_hits
        )
        for n, m in enumerate(posts):
            if n >= self.max_posts:
                break
            item = media_item(m)
            if store.unchanged(item["id"], item["comments_count"]):
                stats.posts_skipped += 1
                continue
            media = store.post(item)
            if m.get("comments_disabled") or not item["comments_count"]:
                store.finish(media)
                continue
            self._comments(store, media, str(m["pk"]))
            store.finish(media)
            log.info("%s: %s comments seen so far", item["permalink"], stats.comments_seen)
        return stats

    # --- posts ----------------------------------------------------------------

    def _too_old(self, m: dict[str, Any]) -> bool:
        taken = m.get("taken_at")
        return bool(self.since and taken and datetime.fromtimestamp(taken, UTC).date() < self.since)

    def _profile_posts(self, username: str) -> Iterator[dict[str, Any]]:
        def enough(posts: list[dict[str, Any]]) -> bool:
            if sum(not self._too_old(m) for m in posts) >= self.max_posts:
                return True
            # Past the pinned posts the grid is newest first, so an old post ends it.
            return len(posts) > PINNED_MAX and self._too_old(posts[-1])

        posts = self.list_posts(username, enough)
        if not posts:
            raise ScrapeError(f"@{username}: no posts visible (private, empty or not found)")
        yield from (m for m in posts if not self._too_old(m))

    def _hashtag_posts(self, tag: str) -> Iterator[dict[str, Any]]:
        data = self.fetch(f"/api/v1/tags/web_info/?tag_name={quote(tag)}")
        posts = [m for m in find_media(data) if not self._too_old(m)]
        if not posts:
            raise ScrapeError(f"#{tag}: no posts returned")
        yield from posts

    def _post(self, shortcode: str) -> dict[str, Any]:
        data = self.fetch(f"/api/v1/media/{shortcode_to_pk(shortcode)}/info/")
        items = data.get("items") or []
        if not items:
            raise ScrapeError(f"post {shortcode} not found")
        post: dict[str, Any] = items[0]
        return post

    # --- comments ---------------------------------------------------------------

    def _comments(self, store: Store, media: Media, pk: str) -> None:
        base = f"/api/v1/media/{pk}/comments/?can_support_threading=true&permalink_enabled=false"
        limit = store.stats.comments_seen + self.max_comments
        query = ""
        tried: set[str] = set()
        for _ in range(MAX_PAGES):
            self.pacer.wait()
            data = self.fetch(base + (f"&{query}" if query else ""))
            for c in data.get("comments") or []:
                store.comment(media, comment_item(c))
                previews = c.get("preview_child_comments") or []
                if (c.get("child_comment_count") or 0) > len(previews):
                    self._replies(store, media, pk, str(c["pk"]))
                else:
                    for r in previews:
                        store.comment(media, comment_item(r, str(c["pk"])))
                if store.stats.comments_seen >= limit:
                    self.session.commit()
                    return
            self.session.commit()
            query = _next_query(data, ("next_min_id", "min_id"), ("next_max_id", "max_id"))
            if not query or query in tried:
                return
            tried.add(query)

    def _replies(self, store: Store, media: Media, pk: str, comment_pk: str) -> None:
        base = f"/api/v1/media/{pk}/comments/{comment_pk}/child_comments/"
        query = ""
        tried: set[str] = set()
        for _ in range(MAX_PAGES):
            self.pacer.wait(0.5)
            data = self.fetch(base + (f"?{query}" if query else ""))
            for r in data.get("child_comments") or []:
                store.comment(media, comment_item(r, comment_pk))
            self.session.commit()
            pairs = []
            if data.get("has_more_tail_child_comments"):
                pairs.append(("next_max_child_cursor", "max_id"))
            if data.get("has_more_head_child_comments"):
                pairs.append(("next_min_child_cursor", "min_id"))
            query = _next_query(data, *pairs)
            if not query or query in tried:
                return
            tried.add(query)


def _next_query(data: dict[str, Any], *pairs: tuple[str, str]) -> str:
    """'param=cursor' from the first cursor field present, else ''."""
    for field, param in pairs:
        cursor = data.get(field)
        if cursor:
            if not isinstance(cursor, str):
                cursor = json.dumps(cursor)
            return f"{param}={quote(cursor)}"
    return ""
