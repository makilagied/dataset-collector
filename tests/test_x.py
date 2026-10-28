import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from collector.models import Account, Comment, Media
from collector.scrape import twitter
from collector.scrape.browser import logged_in
from collector.scrape.common import BlockedError, Pacer, ScrapeError
from collector.security import hash_author

NO_WAIT = Pacer(0)
JAN = "Sat Jan 10 12:00:00 +0000 2026"
MAR = "Tue Mar 10 12:00:00 +0000 2026"


# --- targets ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind", "value", "url"),
    [
        ("@Diamond", "profile", "diamond", "https://x.com/diamond"),
        ("millard_ayo", "profile", "millard_ayo", "https://x.com/millard_ayo"),
        (
            "#Udaku",
            "hashtag",
            "udaku",
            "https://x.com/search?q=%23udaku&src=hashtag_click&f=live",
        ),
        ("https://twitter.com/Simba/", "profile", "simba", "https://x.com/simba"),
        ("x.com/simba/with_replies", "profile", "simba", "https://x.com/simba"),
        (
            "https://x.com/Simba/status/1830000000000000001?s=20",
            "post",
            "1830000000000000001",
            "https://x.com/simba/status/1830000000000000001",
        ),
        (
            "https://mobile.twitter.com/i/web/status/1830000000000000002",
            "post",
            "1830000000000000002",
            "https://x.com/i/status/1830000000000000002",
        ),
        (
            "https://x.com/simba/status/1830000000000000003/photo/1",
            "post",
            "1830000000000000003",
            "https://x.com/simba/status/1830000000000000003",
        ),
        (
            "https://x.com/hashtag/BongoFleva",
            "hashtag",
            "bongofleva",
            "https://x.com/search?q=%23bongofleva&src=hashtag_click&f=live",
        ),
        (
            "https://x.com/search?q=%23Udaku&src=typed_query",
            "hashtag",
            "udaku",
            "https://x.com/search?q=%23udaku&src=hashtag_click&f=live",
        ),
    ],
)
def test_parse_target(raw: str, kind: str, value: str, url: str) -> None:
    assert twitter.parse_target(raw) == twitter.Target(kind, value, url)


@pytest.mark.parametrize(
    "raw",
    [
        "not a user!",
        "@way_too_long_for_x_handles",
        "https://x.com/",
        "https://x.com/home",
        "https://x.com/simba/status/abc",
        "https://x.com/search?q=mjinga",
        "https://www.tiktok.com/@simba",
    ],
)
def test_parse_target_rejects_junk(raw: str) -> None:
    with pytest.raises(ScrapeError):
        twitter.parse_target(raw)


# --- data shapes ------------------------------------------------------------------


def tweet(
    tid: str,
    text: str,
    *,
    user: str = "Mama",
    uid: str = "100",
    created: str = MAR,
    replies: int = 0,
    reply_to: str | None = None,
    **legacy: Any,
) -> dict[str, Any]:
    """A tweet object the way GraphQL timelines and TweetDetail carry it."""
    return {
        "__typename": "Tweet",
        "rest_id": tid,
        "core": {
            "user_results": {
                "result": {
                    "rest_id": uid,
                    "core": {"screen_name": user, "name": "Secret Name"},
                    "legacy": {},
                }
            }
        },
        "legacy": {
            "full_text": text,
            "created_at": created,
            "reply_count": replies,
            "favorite_count": 2,
            "in_reply_to_status_id_str": reply_to,
            "user_id_str": uid,
            **legacy,
        },
    }


def reply(tid: str, text: str, uid: str, to: str, **kw: Any) -> dict[str, Any]:
    full = f"@mama {text}"
    return tweet(tid, full, user=f"user{uid}", uid=uid, reply_to=to,
                 display_text_range=[6, len(full)], **kw)  # fmt: skip


def entries(*tweets: dict[str, Any]) -> dict[str, Any]:
    """Tweets wrapped in a timeline response, as X nests them."""
    return {
        "data": {
            "timeline": {
                "instructions": [
                    {
                        "type": "TimelineAddEntries",
                        "entries": [
                            {"content": {"itemContent": {"tweet_results": {"result": t}}}}
                            for t in tweets
                        ],
                    }
                ]
            }
        }
    }


def test_find_tweets_skips_quotes_and_unwraps_visibility() -> None:
    quoted = tweet("9", "quoted post", user="other")
    outer = tweet("1", "look at this", quoted_status_id_str="9")
    outer["quoted_status_result"] = {"result": quoted}
    wrapped = {"__typename": "TweetWithVisibilityResults", "tweet": tweet("2", "limited")}
    found = twitter.find_tweets(entries(outer, wrapped, {"__typename": "TweetTombstone"}))
    assert [t["rest_id"] for t in found] == ["1", "2"]


def test_media_item() -> None:
    post = tweet("1", "habari", replies=12, extended_entities={"media": [{"type": "photo"}]})
    assert twitter.media_item(post) == {
        "id": "x:1",
        "caption": "habari",
        "media_type": "IMAGE",
        "permalink": "https://x.com/mama/status/1",
        "timestamp": datetime(2026, 3, 10, 12, tzinfo=UTC).isoformat(),
        "comments_count": 12,
    }
    assert twitter.media_item(tweet("2", "text only"))["media_type"] == "TEXT"


def test_comment_text_drops_reply_mentions_and_keeps_long_notes() -> None:
    r = reply("11", "wewe ni mjinga", "7", to="1")
    assert twitter.comment_item(r, "1")["text"] == "wewe ni mjinga"
    assert twitter.comment_item(r, "1")["parent_id"] is None
    nested = reply("111", "acha", "8", to="11")
    assert twitter.comment_item(nested, "1")["parent_id"] == "x:11"
    note = tweet("3", "short…")
    note["note_tweet"] = {"note_tweet_results": {"result": {"text": "the whole long post"}}}
    assert twitter.text(note) == "the whole long post"


def test_thread_keeps_only_replies_to_the_post() -> None:
    thread = twitter.Thread("1")
    first = thread.add(
        [
            tweet("0", "the post this one answers"),
            tweet("1", "the post", reply_to="0"),
            reply("111", "reply to a reply, before its parent", "8", to="11"),
            reply("11", "a reply", "7", to="1"),
            tweet("55", "discover more: unrelated"),
        ]
    )
    assert [t["rest_id"] for t in first] == ["11", "111"]
    later = thread.add([reply("11", "a reply", "7", to="1"), reply("12", "another", "9", to="1")])
    assert [t["rest_id"] for t in later] == ["12"]


# --- scraper ----------------------------------------------------------------------


class FakeX:
    """A twitter.Site with canned pages: timelines load two posts per scroll, a
    conversation comes in the batches a real page would load it in."""

    def __init__(self) -> None:
        self.timelines: dict[str, list[dict[str, Any]]] = {
            "https://x.com/mama": [
                tweet("1", "pinned", replies=3),
                tweet("5", "RT someone", retweeted_status_result={}),
                tweet("2", "no replies yet"),
                tweet("3", "old one", created=JAN, replies=1),
            ],
            twitter.search_url("udaku"): [tweet("4", "#udaku leo", user="other", replies=1)],
        }
        self.posts = {t["rest_id"]: t for tl in self.timelines.values() for t in tl}
        self.conversations: dict[str, list[list[dict[str, Any]]]] = {
            "1": [
                [self.posts["1"], reply("11", "wewe ni mjinga", "7", to="1")],
                [reply("111", "nyie wajinga", "8", to="11"), tweet("55", "unrelated")],
                [reply("12", "safi sana", "9", to="1")],
            ],
            "3": [[reply("31", "habari", "5", to="3")]],
            "4": [[reply("41", "malaya wewe", "6", to="4")]],
        }
        self.opened: list[str] = []
        self.current: str | None = None

    def list_posts(self, url: str, enough: Callable[[twitter.Tweets], bool]) -> twitter.Tweets:
        timeline = self.timelines.get(url, [])
        loaded: list[dict[str, Any]] = []
        while len(loaded) < len(timeline) and not enough(loaded):
            loaded = timeline[: len(loaded) + 2]
        return json.loads(json.dumps(loaded))

    def open_post(self, url: str) -> dict[str, Any] | None:
        self.opened.append(url)
        pid = url.rstrip("/").split("/")[-1]
        self.current = pid if pid in self.posts else None
        return json.loads(json.dumps(self.posts[pid])) if self.current else None

    def read_replies(self, on_tweets: twitter.OnTweets) -> None:
        for batch in self.conversations.get(self.current or "", []):
            if not on_tweets(json.loads(json.dumps(batch))):
                return


def scraper(db: Session, site: FakeX, **kw: Any) -> twitter.XScraper:
    return twitter.XScraper(db, site, NO_WAIT, **kw)


def test_profile_scrape(db: Session) -> None:
    site = FakeX()
    stats = scraper(db, site, category="Udaku").scrape("@mama")

    assert (stats.posts_seen, stats.comments_seen, stats.comments_inserted) == (3, 4, 4)
    account = db.scalars(select(Account)).one()
    assert (account.ig_user_id, account.username, account.category) == (
        "scrape:x:mama",
        "mama",
        "udaku",
    )
    assert db.scalars(select(Media.ig_media_id).order_by(Media.ig_media_id)).all() == [
        "x:1",
        "x:2",
        "x:3",
    ]  # the retweet is not a post of hers
    rows = {c.ig_comment_id: c for c in db.scalars(select(Comment))}
    assert rows["x:11"].text_raw == "wewe ni mjinga" and rows["x:11"].parent_ig_comment_id is None
    assert rows["x:111"].parent_ig_comment_id == "x:11"
    assert rows["x:11"].author_hash == hash_author("id:x:7")
    assert "x:55" not in rows
    for c in rows.values():  # no commenter identity in stored payloads
        assert "Secret" not in json.dumps(c.raw_json) and "user" not in (c.raw_json or {})
    assert "https://x.com/mama/status/2" in site.opened


def test_rerun_skips_unchanged_posts(db: Session) -> None:
    scraper(db, FakeX()).scrape("@mama")
    stats = scraper(db, FakeX()).scrape("@mama")
    assert (stats.posts_seen, stats.posts_skipped) == (0, 3)


def test_since_and_max_posts(db: Session) -> None:
    stats = scraper(db, FakeX(), since=datetime(2026, 2, 1).date()).scrape("@mama")
    assert stats.posts_seen == 2
    stats = scraper(db, FakeX(), max_posts=1, refresh=True).scrape("@mama")
    assert stats.posts_seen == 1


def test_max_comments_per_post(db: Session) -> None:
    stats = scraper(db, FakeX(), max_comments=2).scrape("https://x.com/mama/status/1")
    assert stats.comments_seen == 2


def test_post_url_files_under_its_author(db: Session) -> None:
    scraper(db, FakeX()).scrape("https://x.com/i/status/4")
    assert db.scalars(select(Account.ig_user_id)).one() == "scrape:x:other"


def test_hashtag(db: Session) -> None:
    stats = scraper(db, FakeX()).scrape("#Udaku")
    assert (stats.posts_seen, stats.comments_inserted) == (1, 1)
    assert db.scalars(select(Account.ig_user_id)).one() == "scrape:x:#udaku"


def test_missing_targets(db: Session) -> None:
    with pytest.raises(ScrapeError, match="no posts visible"):
        scraper(db, FakeX()).scrape("@nobody")
    with pytest.raises(ScrapeError, match="not found"):
        scraper(db, FakeX()).scrape("https://x.com/mama/status/999")


class RefusingX(FakeX):
    def __init__(self, refuse: set[str]) -> None:
        super().__init__()
        self.refuse = refuse

    def open_post(self, url: str) -> dict[str, Any] | None:
        if url.rstrip("/").split("/")[-1] in self.refuse:
            raise ScrapeError("X refused to open it")
        return super().open_post(url)


def test_a_refused_post_is_skipped(db: Session) -> None:
    stats = scraper(db, RefusingX({"1"})).scrape("@mama")
    assert (stats.posts_seen, stats.posts_failed) == (2, 1)


def test_three_refusals_in_a_row_stop_the_run(db: Session) -> None:
    site = RefusingX({"1", "2", "3"})
    with pytest.raises(BlockedError, match="refused 3 posts in a row"):
        scraper(db, site).scrape("@mama")


def test_login_cookie() -> None:
    class Context:
        def __init__(self, cookies: list[dict[str, str]]) -> None:
            self._cookies = cookies

        def cookies(self) -> list[dict[str, str]]:
            return self._cookies

    assert logged_in(Context([{"name": "auth_token", "domain": ".x.com"}]), "x")  # type: ignore[arg-type]
    assert not logged_in(Context([{"name": "guest_id", "domain": ".x.com"}]), "x")  # type: ignore[arg-type]
