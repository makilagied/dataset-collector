import base64
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from collector.models import Account, Comment, Media
from collector.scrape import facebook, instagram
from collector.scrape.common import (
    BlockedError,
    Pacer,
    ScrapeError,
    ScrapeStats,
    Store,
    ensure_account,
)
from collector.security import hash_author

NO_WAIT = Pacer(0)


@pytest.fixture
def only_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    from collector.config import get_settings

    monkeypatch.setenv("STORE_ONLY_HITS", "true")
    get_settings.cache_clear()


JAN = int(datetime(2026, 1, 10, tzinfo=UTC).timestamp())
MAR = int(datetime(2026, 3, 10, tzinfo=UTC).timestamp())

# --- instagram: pure parts ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("@Diamond", "profile", "diamond"),
        ("millardayo", "profile", "millardayo"),
        ("#Udaku", "hashtag", "udaku"),
        ("https://www.instagram.com/simbasctanzania/", "profile", "simbasctanzania"),
        ("https://www.instagram.com/p/C1a2B3c4D5e/?img_index=1", "post", "C1a2B3c4D5e"),
        ("instagram.com/reel/C1a2B3c4D5e", "post", "C1a2B3c4D5e"),
        ("https://www.instagram.com/explore/tags/bongofleva/", "hashtag", "bongofleva"),
    ],
)
def test_ig_parse_target(raw: str, kind: str, value: str) -> None:
    assert instagram.parse_target(raw) == instagram.Target(kind, value)


def test_ig_parse_target_rejects_junk() -> None:
    with pytest.raises(ScrapeError):
        instagram.parse_target("not a user!")


def test_shortcode_to_pk() -> None:
    assert instagram.shortcode_to_pk("A") == "0"
    assert instagram.shortcode_to_pk("BA") == "64"
    assert instagram.shortcode_to_pk("B_") == str(64 + 63)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (429, "{}"),
        (401, "{}"),
        (200, '{"message": "checkpoint_required", "status": "fail"}'),
        (200, "<!DOCTYPE html><html>login</html>"),
    ],
)
def test_check_response_blocked(status: int, body: str) -> None:
    with pytest.raises(BlockedError):
        instagram.check_response(status, body)


def test_check_response_not_found_is_not_blocked() -> None:
    with pytest.raises(ScrapeError) as e:
        instagram.check_response(404, "{}")
    assert not isinstance(e.value, BlockedError)


# --- instagram: scraper against a fake site --------------------------------------


def ig_comment(pk: int, text: str, user: int, **extra: Any) -> dict[str, Any]:
    return {
        "pk": pk,
        "text": text,
        "created_at": MAR,
        "user": {"pk": user, "username": f"user{user}", "full_name": "Secret Name"},
        "comment_like_count": 2,
        **extra,
    }


class FakeInstagram:
    """Routes endpoint paths to canned responses and serves profile grids two posts per
    "scroll"; records every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.scrolls = 0
        self.grids: dict[str, list[dict[str, Any]]] = {
            "mama": [
                {
                    "pk": "1",
                    "code": "AAA",
                    "taken_at": MAR,
                    "comment_count": 3,
                    "caption": {"text": "Habari"},
                    "media_type": 1,
                },
                {"pk": "2", "code": "AAB", "taken_at": MAR, "comment_count": 0},
                {"pk": "3", "code": "AAC", "taken_at": JAN, "comment_count": 9},
            ]
        }
        self.routes: dict[str, dict[str, Any]] = {
            "/api/v1/media/1/comments/?can_support_threading=true&permalink_enabled=false": {
                "comments": [
                    ig_comment(
                        11,
                        "wewe ni mjinga",
                        7,
                        child_comment_count=2,
                        preview_child_comments=[ig_comment(111, "acha", 8)],
                    ),
                ],
                "next_min_id": '{"cached_comments_cursor": "11"}',
            },
            "/api/v1/media/1/comments/?can_support_threading=true&permalink_enabled=false"
            "&min_id=%7B%22cached_comments_cursor%22%3A%20%2211%22%7D": {
                "comments": [ig_comment(12, "safi sana", 9)],
            },
            "/api/v1/media/3/comments/?can_support_threading=true&permalink_enabled=false": {
                "comments": [],
            },
            "/api/v1/media/1/comments/11/child_comments/": {
                "child_comments": [ig_comment(111, "acha", 8)],
                "has_more_tail_child_comments": True,
                "next_max_child_cursor": "r1",
            },
            "/api/v1/media/1/comments/11/child_comments/?max_id=r1": {
                "child_comments": [ig_comment(112, "nyie wajinga", 7)],
            },
        }

    def __call__(self, path: str) -> dict[str, Any]:
        self.calls.append(path)
        if path not in self.routes:
            raise AssertionError(f"unexpected fetch {path}")
        return json.loads(json.dumps(self.routes[path]))  # fresh copy

    def list_posts(
        self, username: str, enough: Callable[[list[dict[str, Any]]], bool]
    ) -> list[dict[str, Any]]:
        grid = self.grids.get(username, [])
        loaded: list[dict[str, Any]] = []
        while len(loaded) < len(grid) and not enough(loaded):
            self.scrolls += 1
            loaded = grid[: len(loaded) + 2]
        return [dict(m) for m in loaded]


def ig_scraper(
    db: Session,
    site: FakeInstagram,
    fetch: Callable[[str], dict[str, Any]] | None = None,
    **kw: Any,
) -> Any:
    return instagram.InstagramScraper(db, fetch or site, site.list_posts, NO_WAIT, **kw)


def test_ig_profile_scrape(db: Session) -> None:
    site = FakeInstagram()
    stats = ig_scraper(db, site, category="Udaku").scrape("@mama")

    assert (stats.posts_seen, stats.comments_seen, stats.comments_inserted) == (3, 4, 4)
    account = db.scalars(select(Account)).one()
    assert (account.ig_user_id, account.username, account.category) == (
        "scrape:ig:mama",
        "mama",
        "udaku",
    )
    comments = {c.ig_comment_id: c for c in db.scalars(select(Comment))}
    assert set(comments) == {"ig:11", "ig:111", "ig:112", "ig:12"}
    reply = comments["ig:112"]
    assert reply.parent_ig_comment_id == "ig:11"
    assert reply.source == "scrape"
    assert reply.author_hash == hash_author("id:ig:7")
    assert reply.commented_at == datetime.fromtimestamp(MAR, UTC)
    for c in comments.values():  # no commenter identity in stored payloads
        assert "Secret" not in json.dumps(c.raw_json) and "user" not in (c.raw_json or {})
    post = db.scalars(select(Media).where(Media.ig_media_id == "ig:1")).one()
    assert (post.caption, post.permalink, post.media_type) == (
        "Habari",
        "https://www.instagram.com/p/AAA/",
        "IMAGE",
    )
    assert post.last_synced_at is not None


def test_ig_rerun_skips_unchanged_posts(db: Session) -> None:
    ig_scraper(db, FakeInstagram()).scrape("mama")
    site = FakeInstagram()
    stats = ig_scraper(db, site).scrape("mama")
    assert stats.posts_skipped == 3 and stats.comments_seen == 0
    assert not any("/comments/" in c for c in site.calls)


def test_ig_since_and_max_posts(db: Session) -> None:
    site = FakeInstagram()
    stats = ig_scraper(db, site, since=date(2026, 2, 1), max_posts=1).scrape("mama")
    assert stats.posts_seen == 1
    assert {m.ig_media_id for m in db.scalars(select(Media))} == {"ig:1"}
    assert site.scrolls == 1  # stopped loading the grid once enough posts were in


def test_ig_since_stops_at_old_posts_past_the_pinned_ones(db: Session) -> None:
    site = FakeInstagram()
    new = {"pk": "9", "code": "AAZ", "taken_at": MAR, "comment_count": 0}
    old = {"pk": "8", "code": "AAY", "taken_at": JAN, "comment_count": 0}
    site.grids["mama"] = [old, new, new | {"pk": "7"}, new | {"pk": "6"}, old, old, old, old]
    stats = ig_scraper(db, site, since=date(2026, 2, 1)).scrape("mama")
    assert stats.posts_seen == 3  # the old pinned post is skipped, not a stop signal
    assert site.scrolls == 3


def test_ig_profile_without_posts(db: Session) -> None:
    with pytest.raises(ScrapeError, match="no posts visible"):
        ig_scraper(db, FakeInstagram()).scrape("@nobody")


def test_ig_stores_only_hits(db: Session, only_hits: None) -> None:
    stats = ig_scraper(db, FakeInstagram()).scrape("mama")
    assert (stats.comments_seen, stats.comments_kept) == (4, 2)
    assert {c.ig_comment_id for c in db.scalars(select(Comment))} == {"ig:11", "ig:112"}


def test_ig_post_url_files_under_owner(db: Session) -> None:
    site = FakeInstagram()
    pk = instagram.shortcode_to_pk("AAA")
    site.routes[f"/api/v1/media/{pk}/info/"] = {
        "items": [
            {
                "pk": 1,
                "code": "AAA",
                "taken_at": MAR,
                "comment_count": 3,
                "user": {"username": "mama"},
            }
        ]
    }
    ig_scraper(db, site).scrape("https://www.instagram.com/p/AAA/")
    assert db.scalars(select(Account.ig_user_id)).one() == "scrape:ig:mama"
    assert db.query(Comment).count() == 4


def test_ig_hashtag(db: Session) -> None:
    site = FakeInstagram()
    site.routes["/api/v1/tags/web_info/?tag_name=udaku"] = {
        "data": {
            "top": {
                "sections": [
                    {
                        "layout_content": {
                            "medias": [
                                {
                                    "media": {
                                        "pk": 1,
                                        "code": "AAA",
                                        "taken_at": MAR,
                                        "comment_count": 3,
                                    }
                                },
                                {
                                    "media": {
                                        "pk": 2,
                                        "code": "AAB",
                                        "taken_at": MAR,
                                        "comment_count": 0,
                                    }
                                },
                            ]
                        }
                    }
                ]
            }
        }
    }
    stats = ig_scraper(db, site).scrape("#udaku")
    assert stats.posts_seen == 2
    assert db.scalars(select(Account.username)).one() == "#udaku"


def test_ig_blocked_keeps_progress(db: Session) -> None:
    site = FakeInstagram()
    route = "/api/v1/media/1/comments/11/child_comments/?max_id=r1"
    del site.routes[route]

    def fetch(path: str) -> dict[str, Any]:
        if path == route:
            raise BlockedError("rate limited")
        return site(path)

    with pytest.raises(BlockedError):
        ig_scraper(db, site, fetch).scrape("mama")
    db.rollback()
    # The first comment page was committed before the block.
    assert db.query(Comment).count() >= 1
    # The interrupted post isn't marked done, so the next run retries it.
    assert db.scalars(select(Media.last_synced_at).where(Media.ig_media_id == "ig:1")).one() is None


# --- facebook ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind", "handle"),
    [
        ("https://www.facebook.com/millardayo", "page", "millardayo"),
        ("millardayo", "page", "millardayo"),
        ("https://www.facebook.com/millardayo/posts/pfbid02abc", "post", "millardayo"),
        ("https://web.facebook.com/reel/123456", "post", "posts"),
        ("https://www.facebook.com/permalink.php?story_fbid=1&id=2", "post", "posts"),
        ("https://www.facebook.com/profile.php?id=1000123", "page", "id:1000123"),
        ("https://www.facebook.com/groups/bongo/", "page", "group:bongo"),
        ("https://www.facebook.com/groups/bongo/posts/987/", "post", "group:bongo"),
    ],
)
def test_fb_parse_target(raw: str, kind: str, handle: str) -> None:
    t = facebook.parse_target(raw)
    assert (t.kind, t.handle) == (kind, handle)


def test_fb_post_key() -> None:
    assert facebook.post_key("https://www.facebook.com/x/posts/pfbid02abc") == "pfbid02abc"
    assert facebook.post_key("https://www.facebook.com/permalink.php?story_fbid=55&id=2") == "55"
    assert facebook.post_key("https://www.facebook.com/watch/?v=77") == "77"


def test_fb_find_post_urls_from_escaped_json() -> None:
    text = (
        '{"url":"https:\\/\\/www.facebook.com\\/millardayo\\/posts\\/pfbid0X1"}'
        ' <a href="https://web.facebook.com/reel/123">'
        " https://www.facebook.com/millardayo/posts/pfbid0X1"
    )
    assert facebook.find_post_urls(text) == [
        "https://www.facebook.com/millardayo/posts/pfbid0X1",
        "https://www.facebook.com/reel/123",
    ]


def fb_node(cid: str, text: str | None, author: str, parent: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "__typename": "Comment",
        "id": base64.b64encode(f"comment:999_{cid}".encode()).decode().rstrip("="),
        "body": {"text": text} if text is not None else None,
        "author": {"__typename": "User", "id": author, "name": "Secret Name"},
        "created_time": MAR,
        "feedback": {"reactors": {"count": 3}},
    }
    if parent:
        node["comment_direct_parent"] = {"id": "x", "legacy_fbid": parent}
    return node


def graphql_text() -> str:
    doc1 = {
        "data": {
            "node": {
                "comments": {
                    "edges": [
                        {"node": fb_node("1", "wewe ni mjinga", "u1")},
                        {"node": fb_node("2", None, "u2")},  # sticker-only comment
                    ]
                }
            }
        }
    }
    doc2 = {"data": {"replies": [{"node": fb_node("3", "acha wewe", "u3", parent="1")}]}}
    return "for (;;);" + json.dumps(doc1) + "\n" + json.dumps(doc2)


def test_fb_comment_extraction() -> None:
    docs = list(facebook.iter_json_docs(graphql_text()))
    nodes = [n for d in docs for n in facebook.find_comments(d)]
    items = [facebook.comment_item(n) for n in nodes]
    assert [i["id"] for i in items if i] == ["fb:1", "fb:2", "fb:3"]
    reply = items[2]
    assert reply is not None
    assert reply["parent_id"] == "fb:1"
    assert reply["like_count"] == 3
    assert reply["from"] == {"id": "fb:u3"}
    assert items[1] is not None and items[1]["text"] == ""


def test_fb_store_docs(db: Session) -> None:
    account = ensure_account(db, "facebook", "millardayo")
    stats = ScrapeStats()
    store = Store(db, account, stats)
    media = store.post({"id": "fb:pfbid0X1", "permalink": "https://www.facebook.com/x"})
    seen: set[str] = set()
    facebook.store_docs(store, media, [graphql_text(), graphql_text()], seen)
    assert stats.comments_seen == 3  # duplicates across responses counted once
    rows = {c.ig_comment_id: c for c in db.scalars(select(Comment))}
    assert rows["fb:3"].parent_ig_comment_id == "fb:1"
    assert rows["fb:1"].author_hash == hash_author("id:fb:u1")
    assert all("Secret" not in json.dumps(c.raw_json) for c in rows.values())
    assert account.ig_user_id == "scrape:fb:millardayo"


# --- rate limits ----------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_429_is_a_rate_limit() -> None:
    from collector.scrape.common import RateLimitedError

    with pytest.raises(RateLimitedError):
        instagram.check_response(429, "")
    with pytest.raises(RateLimitedError):
        instagram.check_response(200, '{"message": "Please wait a few minutes"}')
    with pytest.raises(BlockedError) as e:
        instagram.check_response(200, '{"message": "checkpoint_required"}')
    assert not isinstance(e.value, RateLimitedError)


def test_cooldown_waits_and_carries_on() -> None:
    from collector.scrape.common import COOLDOWN_STEPS_S, Cooldown, RateLimitedError

    clock = FakeClock()
    cd = Cooldown("instagram", sleep=clock.sleep, clock=clock)
    answers: list[object] = [RateLimitedError("429"), RateLimitedError("429"), {"ok": 1}]

    def fetch() -> object:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert cd.call(fetch) == {"ok": 1}
    assert clock.slept == list(COOLDOWN_STEPS_S[:2])  # 15 then 30 minutes
    assert cd.call(lambda: "next") == "next" and len(clock.slept) == 2  # no pause once lifted


def test_cooldown_gives_up_and_next_job_waits_first() -> None:
    from collector.scrape.common import COOLDOWN_MAX_S, COOLDOWN_STEPS_S, Cooldown, RateLimitedError

    clock = FakeClock()
    cd = Cooldown("instagram", sleep=clock.sleep, clock=clock)

    def always_429() -> None:
        raise RateLimitedError("429")

    with pytest.raises(RateLimitedError):
        cd.call(always_429)
    assert sum(clock.slept) >= COOLDOWN_MAX_S
    clock.slept.clear()
    assert cd.call(lambda: "ok") == "ok"
    assert clock.slept == [COOLDOWN_STEPS_S[-1]]  # the next run starts after a last pause


def test_pacer_hourly_cap() -> None:
    clock = FakeClock()
    pacer = Pacer(0, sleep=clock.sleep, max_per_hour=3, clock=clock)
    for _ in range(3):
        pacer.wait()
        clock.now += 60
    assert clock.slept == []
    pacer.wait()  # 4th request within the hour waits for the 1st to age out
    assert clock.slept == [3600 - 180]
    assert Pacer(0, sleep=clock.sleep).max_per_hour == 0  # uncapped by default
