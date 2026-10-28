"""X (formerly Twitter) scraper: reads posts and replies out of the GraphQL responses a
logged-in x.com page loads by itself.

X signs and rate-limits its API calls in page JavaScript, so nothing here calls an
endpoint directly. The browser side (browser.XSite) opens profile, search and post pages
and hands over the tweet objects the page receives: UserTweets / SearchTimeline while
scrolling a timeline, TweetDetail while scrolling a conversation. Everything that knows the
shape of that data lives in this module; the `Site` protocol keeps the browser swappable in
tests.

Replies are the "comments" of a post. A reply to a reply keeps its parent, so threads
under a post are stored the way Instagram reply threads are.

Targets: "@user" / "user" / profile URL, "#tag" / hashtag or search URL, or a post URL
(x.com or twitter.com).
"""

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, urlparse

from sqlalchemy.orm import Session

from collector.models import Media
from collector.scrape.common import (
    BlockedError,
    Pacer,
    ScrapeError,
    ScrapeStats,
    Store,
    ensure_account,
)

log = logging.getLogger(__name__)

BASE = "https://x.com"
HOSTS = ("x.com", "twitter.com")
PINNED_MAX = 1  # a pinned post leads a profile regardless of its age
MAX_FAILURES = 3  # posts in a row X refuses to open before the run stops
_RESERVED = {
    "home", "explore", "search", "i", "notifications", "messages", "settings", "hashtag",
    "compose", "login", "logout", "signup", "tos", "privacy", "intent", "share",
}  # fmt: skip
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
_CREATED = "%a %b %d %H:%M:%S %z %Y"  # "Wed Oct 08 20:19:24 +0000 2026"

Tweets = list[dict[str, Any]]
# Called with each batch of tweets the conversation page loads; returns False to stop.
OnTweets = Callable[[Tweets], bool]


class Site(Protocol):
    def list_posts(self, url: str, enough: Callable[[Tweets], bool]) -> Tweets:
        """Posts on a profile or search page, in page order, loading more until enough()."""
        ...

    def open_post(self, url: str) -> dict[str, Any] | None:
        """Open a post's page; its tweet object, or None if it is not there."""
        ...

    def read_replies(self, on_tweets: OnTweets) -> None:
        """Load the open post's conversation, passing each batch of tweets on."""
        ...


@dataclass(frozen=True)
class Target:
    kind: str  # profile | hashtag | post
    value: str  # username, tag (no #), or post id
    url: str


def search_url(tag: str) -> str:
    # f=live: the Latest tab, newest first (Top hides most posts and reorders as you scroll).
    return f"{BASE}/search?q={quote('#' + tag)}&src=hashtag_click&f=live"


def parse_target(raw: str) -> Target:
    raw = raw.strip()
    if raw.startswith("#") and len(raw) > 1:
        tag = raw[1:].lower()
        return Target("hashtag", tag, search_url(tag))
    if raw.startswith("@"):
        if not _HANDLE.fullmatch(raw[1:]):
            raise ScrapeError(f"not an X username: {raw!r}")
        user = raw[1:].lower()
        return Target("profile", user, f"{BASE}/{user}")
    url = raw if "//" in raw else "https://" + raw
    parsed = urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.").removeprefix("mobile.")
    if host not in HOSTS:
        if _HANDLE.fullmatch(raw):
            return Target("profile", raw.lower(), f"{BASE}/{raw.lower()}")
        raise ScrapeError(f"not an X user, #tag or URL: {raw!r}")
    parts = [p for p in parsed.path.split("/") if p]
    # /user/status/ID, /i/status/ID, /i/web/status/ID (and /photo/1 etc. after the id)
    if "status" in parts[1:3]:
        i = parts.index("status")
        if i + 1 >= len(parts) or not parts[i + 1].isdigit():
            raise ScrapeError(f"unrecognized X post URL: {raw!r}")
        user = parts[0].lower() if i == 1 and parts[0] != "i" else "i"
        return Target("post", parts[i + 1], f"{BASE}/{user}/status/{parts[i + 1]}")
    if len(parts) >= 2 and parts[0] == "hashtag":
        tag = parts[1].lower()
        return Target("hashtag", tag, search_url(tag))
    if parts and parts[0] == "search":
        q = (parse_qs(parsed.query).get("q") or [""])[0].strip()
        if re.fullmatch(r"#\w+", q):
            tag = q[1:].lower()
            return Target("hashtag", tag, search_url(tag))
        raise ScrapeError(f"only #hashtag searches can be scraped: {raw!r}")
    if parts and parts[0].lower() not in _RESERVED and _HANDLE.fullmatch(parts[0]):
        user = parts[0].lower()
        return Target("profile", user, f"{BASE}/{user}")
    raise ScrapeError(f"unrecognized X URL: {raw!r}")


# --- data shapes ------------------------------------------------------------------


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def is_tweet(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and str(node.get("rest_id", "")).isdigit()
        and isinstance(node.get("legacy"), dict)
        and "created_at" in node["legacy"]
        and ("full_text" in node["legacy"] or "note_tweet" in node)
    )


def find_tweets(payload: Any) -> Tweets:
    """Every tweet object in a GraphQL response, in page order. Quoted and retweeted posts
    nested inside a tweet are not separate entries: they belong to someone else's page."""
    found: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if is_tweet(node):
            found.setdefault(node["rest_id"], node)
            return
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return list(found.values())


def _user(tweet: dict[str, Any]) -> dict[str, Any]:
    result = ((tweet.get("core") or {}).get("user_results") or {}).get("result") or {}
    return result if isinstance(result, dict) else {}


def author_name(tweet: dict[str, Any]) -> str | None:
    user = _user(tweet)
    # The handle moved from user.legacy to user.core in 2025; accept either.
    name = (user.get("core") or {}).get("screen_name") or (user.get("legacy") or {}).get(
        "screen_name"
    )
    return str(name).lower() if name else None


def author_id(tweet: dict[str, Any]) -> str | None:
    uid = _user(tweet).get("rest_id") or tweet["legacy"].get("user_id_str")
    return str(uid) if uid else None


def created_at(tweet: dict[str, Any]) -> datetime | None:
    try:
        return datetime.strptime(tweet["legacy"]["created_at"], _CREATED).astimezone(UTC)
    except (KeyError, TypeError, ValueError):
        return None


def reply_count(tweet: dict[str, Any]) -> int | None:
    return _int(tweet["legacy"].get("reply_count"))


def reply_to(tweet: dict[str, Any]) -> str | None:
    parent = tweet["legacy"].get("in_reply_to_status_id_str")
    return str(parent) if parent else None


def is_retweet(tweet: dict[str, Any]) -> bool:
    return "retweeted_status_result" in tweet["legacy"]


def text(tweet: dict[str, Any]) -> str:
    """The text as shown: a long post's full note, without the @mentions X puts in front of
    every reply (they name who is being answered, not what was said)."""
    note = ((tweet.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result")
    if isinstance(note, dict) and note.get("text"):
        return str(note["text"])
    legacy = tweet["legacy"]
    full = str(legacy.get("full_text") or "")
    span = legacy.get("display_text_range")
    if isinstance(span, list) and len(span) == 2 and all(isinstance(i, int) for i in span):
        return full[span[0] : span[1]]
    return full


def _media_type(tweet: dict[str, Any]) -> str:
    media = (tweet["legacy"].get("extended_entities") or {}).get("media") or []
    kinds = {m.get("type") for m in media if isinstance(m, dict)}
    if kinds & {"video", "animated_gif"}:
        return "VIDEO"
    if "photo" in kinds:
        return "IMAGE"
    return "TEXT"


def post_url(tweet: dict[str, Any]) -> str:
    return f"{BASE}/{author_name(tweet) or 'i'}/status/{tweet['rest_id']}"


def media_item(tweet: dict[str, Any]) -> dict[str, Any]:
    created = created_at(tweet)
    return {
        "id": f"x:{tweet['rest_id']}",
        "caption": text(tweet) or None,
        "media_type": _media_type(tweet),
        "permalink": post_url(tweet),
        "timestamp": created.isoformat() if created else None,
        "comments_count": reply_count(tweet),
    }


def comment_item(tweet: dict[str, Any], post_id: str) -> dict[str, Any]:
    created = created_at(tweet)
    parent = reply_to(tweet)
    uid = author_id(tweet)
    return {
        "id": f"x:{tweet['rest_id']}",
        "text": text(tweet),
        "timestamp": created.isoformat() if created else None,
        "like_count": _int(tweet["legacy"].get("favorite_count")),
        "parent_id": f"x:{parent}" if parent and parent != post_id else None,
        # Only used to compute author_hash; sanitize() drops it before storage.
        "from": {"id": f"x:{uid}"} if uid else {},
    }


class Thread:
    """Sorts the tweets a conversation page loads into replies to one post.

    The page also carries the post itself, the posts it answers (when it is a reply) and,
    in some layouts, unrelated suggestions. A tweet is a reply when the tweet it answers is
    the post or another reply; one whose parent hasn't loaded yet waits for it."""

    def __init__(self, post_id: str):
        self.post_id = post_id
        self.members = {post_id}
        self.waiting: dict[str, dict[str, Any]] = {}

    def add(self, batch: Tweets) -> Tweets:
        replies: Tweets = []
        for t in batch:
            if t["rest_id"] not in self.members:
                self.waiting.setdefault(t["rest_id"], t)
        grew = True
        while grew:
            grew = False
            for tid, t in list(self.waiting.items()):
                if reply_to(t) in self.members:
                    del self.waiting[tid]
                    self.members.add(tid)
                    replies.append(t)
                    grew = True
        return replies


# --- scraper ----------------------------------------------------------------------


class XScraper:
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
            handle, posts = target.value, self._listed(target, f"@{target.value}")
        elif target.kind == "hashtag":
            handle, posts = "#" + target.value, self._listed(target, f"#{target.value}")
        else:
            opened = self.site.open_post(target.url)
            if opened is None:
                raise ScrapeError(f"post {target.url} not found (deleted, protected or hidden)")
            handle, posts = author_name(opened) or "posts", iter([opened])

        account = ensure_account(self.session, "x", handle, self.category)
        store = Store(self.session, account, stats, self.refresh, self.progress, self.only_hits)
        failures = 0
        for n, tweet in enumerate(posts):
            if n >= self.max_posts:
                break
            item = media_item(tweet)
            if store.unchanged(item["id"], item["comments_count"]):
                stats.posts_skipped += 1
                continue
            if opened is None:
                self.pacer.wait()
                try:
                    fresh = self.site.open_post(item["permalink"])
                except BlockedError:
                    raise
                except ScrapeError as e:
                    stats.posts_failed += 1
                    failures += 1
                    log.warning("%s: %s", item["permalink"], e)
                    if failures >= MAX_FAILURES:
                        raise BlockedError(
                            f"X refused {failures} posts in a row; wait a while and scrape again"
                        ) from e
                    continue
                failures = 0
                if fresh is None:
                    log.info("%s: gone, skipped", item["permalink"])
                    continue
                tweet = fresh
                item = media_item(fresh)
            opened = None
            media = store.post(item)
            if item["comments_count"] != 0:
                self._replies(store, media, tweet["rest_id"])
            store.finish(media)
            log.info("%s: %s comments seen so far", item["permalink"], stats.comments_seen)
        return stats

    def _too_old(self, tweet: dict[str, Any]) -> bool:
        created = created_at(tweet)
        return bool(self.since and created and created.date() < self.since)

    def _keep(self, target: Target, tweets: Tweets) -> Tweets:
        # Timelines also carry retweets (the replies belong to the original) and, on a
        # profile, the posts a reply answers.
        tweets = [t for t in tweets if not is_retweet(t)]
        if target.kind == "profile":
            tweets = [t for t in tweets if author_name(t) == target.value]
        return tweets

    def _listed(self, target: Target, label: str) -> Iterator[dict[str, Any]]:
        def enough(tweets: Tweets) -> bool:
            tweets = self._keep(target, tweets)
            if sum(not self._too_old(t) for t in tweets) >= self.max_posts:
                return True
            # Past the pinned post a timeline is newest first, so an old post ends it.
            return len(tweets) > PINNED_MAX and self._too_old(tweets[-1])

        tweets = self._keep(target, self.site.list_posts(target.url, enough))
        if not tweets:
            raise ScrapeError(f"{label}: no posts visible (protected, empty or not found)")
        yield from (t for t in tweets if not self._too_old(t))

    def _replies(self, store: Store, media: Media, post_id: str) -> None:
        limit = store.stats.comments_seen + self.max_comments
        thread = Thread(post_id)

        def on_tweets(batch: Tweets) -> bool:
            for t in thread.add(batch):
                store.comment(media, comment_item(t, post_id))
                if store.stats.comments_seen >= limit:
                    self.session.commit()
                    return False
            self.session.commit()
            return True

        self.site.read_replies(on_tweets)
