"""TikTok scraper: reads videos and comments out of the data a logged-in tiktok.com page
loads by itself.

TikTok signs its API requests in page JavaScript, so nothing here calls an endpoint
directly. The browser side (browser.TikTokSite) opens profile, tag and video pages and
hands over what the page receives: video objects from the page's embedded JSON and from
item_list responses while scrolling, comment objects from comment/list responses while
scrolling the comment panel and opening replies. Everything that knows the shape of that
data lives in this module; the `Site` protocol keeps the browser swappable in tests.

Targets: "@user" / "user" / profile URL, "#tag" / tag URL, or a video / photo URL
(including vm.tiktok.com short links).
"""

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol
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

BASE = "https://www.tiktok.com"
PINNED_MAX = 3  # pinned videos lead a profile regardless of their age
MAX_FAILURES = 3  # videos in a row TikTok refuses to open before the run stops
_RESERVED = {"tag", "music", "search", "explore", "foryou", "following", "live", "discover"}

Videos = list[dict[str, Any]]
# Called with each batch of comment objects the page loads; returns False to stop reading.
OnComments = Callable[[list[dict[str, Any]]], bool]


class Site(Protocol):
    def list_videos(self, url: str, enough: Callable[[Videos], bool]) -> Videos:
        """Videos on a profile or tag page, in page order, loading more until enough()."""
        ...

    def open_video(self, url: str) -> dict[str, Any] | None:
        """Open a video page; its video object, or None if it is not there."""
        ...

    def read_comments(self, on_comments: OnComments) -> None:
        """Load the open video's comments and replies, passing each batch on."""
        ...


@dataclass(frozen=True)
class Target:
    kind: str  # profile | hashtag | video
    value: str  # username, tag (no #), or video id ("" for a short link)
    url: str


def parse_target(raw: str) -> Target:
    raw = raw.strip()
    if raw.startswith("#"):
        tag = raw[1:].lower()
        return Target("hashtag", tag, f"{BASE}/tag/{quote(tag)}")
    if raw.startswith("@"):
        user = raw[1:].lower()
        return Target("profile", user, f"{BASE}/@{quote(user)}")
    if "tiktok.com" not in raw:
        if re.fullmatch(r"[A-Za-z0-9._]+", raw):
            return Target("profile", raw.lower(), f"{BASE}/@{quote(raw.lower())}")
        raise ScrapeError(f"not a TikTok user, #tag or URL: {raw!r}")
    url = raw if "//" in raw else "https://" + raw
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.netloc.split(".")[0] in ("vm", "vt") and parts:  # short link, resolved later
        return Target("video", "", url)
    if len(parts) >= 3 and parts[0].startswith("@") and parts[1] in ("video", "photo"):
        if not parts[2].isdigit():
            raise ScrapeError(f"unrecognized TikTok video URL: {raw!r}")
        return Target("video", parts[2], f"{BASE}/{parts[0]}/{parts[1]}/{parts[2]}")
    if len(parts) >= 2 and parts[0] == "tag":
        tag = parts[1].lower()
        return Target("hashtag", tag, f"{BASE}/tag/{quote(tag)}")
    if parts and parts[0].startswith("@") and len(parts[0]) > 1:
        user = parts[0][1:].lower()
        return Target("profile", user, f"{BASE}/@{quote(user)}")
    if parts and parts[0] not in _RESERVED and re.fullmatch(r"[A-Za-z0-9._]+", parts[0]):
        return Target("profile", parts[0].lower(), f"{BASE}/@{quote(parts[0].lower())}")
    raise ScrapeError(f"unrecognized TikTok URL: {raw!r}")


# --- data shapes ------------------------------------------------------------------


def _int(value: Any) -> int | None:
    """Counts arrive as ints or as digit strings ("statsV2")."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def author_name(video: dict[str, Any]) -> str | None:
    author = video.get("author")
    if isinstance(author, dict):
        name = author.get("uniqueId") or author.get("unique_id")
    else:
        name = author
    return str(name).lower() if name else None


def created_at(video: dict[str, Any]) -> int | None:
    return _int(video.get("createTime"))


def comment_count(video: dict[str, Any]) -> int | None:
    for stats in (video.get("statsV2"), video.get("stats")):
        if isinstance(stats, dict) and (n := _int(stats.get("commentCount"))) is not None:
            return n
    return None


def video_url(video: dict[str, Any]) -> str:
    kind = "photo" if video.get("imagePost") else "video"
    return f"{BASE}/@{quote(author_name(video) or 'user')}/{kind}/{video['id']}"


def media_item(video: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"tt:{video['id']}",
        "caption": video.get("desc") or None,
        "media_type": "IMAGE" if video.get("imagePost") else "VIDEO",
        "permalink": video_url(video),
        "timestamp": ts_iso(created_at(video)),
        "comments_count": comment_count(video),
    }


def _parent(c: dict[str, Any]) -> str | None:
    parent = c.get("reply_id")
    return str(parent) if parent and str(parent) != "0" else None


def comment_item(c: dict[str, Any]) -> dict[str, Any]:
    user = c.get("user") or {}
    author = user.get("uid") or user.get("sec_uid") or user.get("unique_id")
    parent = _parent(c)
    return {
        "id": f"tt:{c['cid']}",
        "text": c.get("text") or "",
        "timestamp": ts_iso(_int(c.get("create_time"))),
        "like_count": _int(c.get("digg_count")),
        "parent_id": f"tt:{parent}" if parent else None,
        # Only used to compute author_hash; sanitize() drops it before storage.
        "from": {"id": f"tt:{author}"} if author else {},
    }


def is_video(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and isinstance(node.get("id"), str | int)
        and str(node["id"]).isdigit()
        and "createTime" in node
        and "author" in node
    )


def find_videos(payload: Any) -> Videos:
    """Every video object anywhere in a payload (page JSON nests them deeply), in order."""
    found: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if is_video(node):
            found.setdefault(str(node["id"]), node)
            return
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return list(found.values())


def find_comments(payload: Any) -> list[dict[str, Any]]:
    """Comment objects in a comment/list or comment/list/reply response, with the reply
    previews TikTok nests inside top-level comments flattened after their parent."""
    out: list[dict[str, Any]] = []
    comments = payload.get("comments") if isinstance(payload, dict) else None
    for c in comments or []:
        if not isinstance(c, dict) or not c.get("cid"):
            continue
        out.append(c)
        for r in c.get("reply_comment") or []:
            if isinstance(r, dict) and r.get("cid"):
                out.append({**r, "reply_id": _parent(r) or c["cid"]})
    return out


# --- scraper ----------------------------------------------------------------------


class TikTokScraper:
    def __init__(
        self,
        session: Session,
        site: Site,
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
        self.site = site
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
        opened: dict[str, Any] | None = None
        if target.kind == "profile":
            handle, videos = target.value, self._listed(target, f"@{target.value}")
        elif target.kind == "hashtag":
            handle, videos = "#" + target.value, self._listed(target, f"#{target.value}")
        else:
            opened = self.site.open_video(target.url)
            if opened is None:
                raise ScrapeError(f"video {target.url} not found (deleted or private)")
            handle, videos = author_name(opened) or "videos", iter([opened])

        account = ensure_account(self.session, "tiktok", handle, self.category)
        store = Store(self.session, account, stats, self.refresh, self.progress, self.only_hits)
        failures = 0
        for n, video in enumerate(videos):
            if n >= self.max_posts:
                break
            item = media_item(video)
            if store.unchanged(item["id"], item["comments_count"]):
                stats.posts_skipped += 1
                continue
            if opened is None:
                self.pacer.wait()
                try:
                    fresh = self.site.open_video(item["permalink"])
                except BlockedError:
                    raise
                except ScrapeError as e:
                    stats.posts_failed += 1
                    failures += 1
                    log.warning("%s: %s", item["permalink"], e)
                    if failures >= MAX_FAILURES:
                        raise BlockedError(
                            f"TikTok refused {failures} videos in a row; wait a while, or "
                            "log in to TikTok, and scrape again"
                        ) from e
                    continue
                failures = 0
                if fresh is None:
                    log.info("%s: gone, skipped", item["permalink"])
                    continue
                item = media_item({**video, **fresh})
            opened = None
            media = store.post(item)
            if item["comments_count"] != 0:
                self._comments(store, media)
            store.finish(media)
            log.info("%s: %s comments seen so far", item["permalink"], stats.comments_seen)
        return stats

    def _too_old(self, video: dict[str, Any]) -> bool:
        created = created_at(video)
        return bool(
            self.since and created and datetime.fromtimestamp(created, UTC).date() < self.since
        )

    def _listed(self, target: Target, label: str) -> Iterator[dict[str, Any]]:
        def enough(videos: Videos) -> bool:
            if target.kind == "profile":
                videos = [v for v in videos if author_name(v) == target.value]
            if sum(not self._too_old(v) for v in videos) >= self.max_posts:
                return True
            # Past the pinned videos a profile is newest first, so an old one ends it.
            return (
                target.kind == "profile" and len(videos) > PINNED_MAX and self._too_old(videos[-1])
            )

        videos = self.site.list_videos(target.url, enough)
        if target.kind == "profile":
            # Profile pages also carry recommended videos by other people.
            videos = [v for v in videos if author_name(v) == target.value]
        if not videos:
            raise ScrapeError(f"{label}: no videos visible (private, empty or not found)")
        yield from (v for v in videos if not self._too_old(v))

    def _comments(self, store: Store, media: Media) -> None:
        limit = store.stats.comments_seen + self.max_comments
        seen: set[str] = set()  # a reply shows up as a preview and again in its thread

        def on_comments(batch: list[dict[str, Any]]) -> bool:
            for c in batch:
                if str(c["cid"]) in seen:
                    continue
                seen.add(str(c["cid"]))
                store.comment(media, comment_item(c))
                if store.stats.comments_seen >= limit:
                    self.session.commit()
                    return False
            self.session.commit()
            return True

        self.site.read_comments(on_comments)
