import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from collector.models import Account, Comment, Media
from collector.scrape import tiktok
from collector.scrape.browser import logged_in
from collector.scrape.common import Pacer, ScrapeError
from collector.security import hash_author

NO_WAIT = Pacer(0)
JAN = int(datetime(2026, 1, 10, tzinfo=UTC).timestamp())
MAR = int(datetime(2026, 3, 10, tzinfo=UTC).timestamp())


# --- targets ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind", "value", "url"),
    [
        ("@Diamond", "profile", "diamond", "https://www.tiktok.com/@diamond"),
        ("millardayo", "profile", "millardayo", "https://www.tiktok.com/@millardayo"),
        ("#Udaku", "hashtag", "udaku", "https://www.tiktok.com/tag/udaku"),
        ("https://www.tiktok.com/@Simba/", "profile", "simba", "https://www.tiktok.com/@simba"),
        (
            "https://www.tiktok.com/@simba/video/7301234567890123456?is_from_webapp=1",
            "video",
            "7301234567890123456",
            "https://www.tiktok.com/@simba/video/7301234567890123456",
        ),
        (
            "tiktok.com/@simba/photo/7301234567890123457",
            "video",
            "7301234567890123457",
            "https://www.tiktok.com/@simba/photo/7301234567890123457",
        ),
        (
            "https://www.tiktok.com/tag/BongoFleva",
            "hashtag",
            "bongofleva",
            "https://www.tiktok.com/tag/bongofleva",
        ),
        ("https://vm.tiktok.com/ZMabc123/", "video", "", "https://vm.tiktok.com/ZMabc123/"),
    ],
)
def test_parse_target(raw: str, kind: str, value: str, url: str) -> None:
    assert tiktok.parse_target(raw) == tiktok.Target(kind, value, url)


@pytest.mark.parametrize(
    "raw", ["not a user!", "https://www.tiktok.com/", "https://www.tiktok.com/@x/video/abc"]
)
def test_parse_target_rejects_junk(raw: str) -> None:
    with pytest.raises(ScrapeError):
        tiktok.parse_target(raw)


# --- data shapes ------------------------------------------------------------------


def video(vid: str, created: int, comments: int | str, **extra: Any) -> dict[str, Any]:
    return {
        "id": vid,
        "desc": f"video {vid}",
        "createTime": created,
        "author": {"uniqueId": "Mama", "nickname": "Secret Name"},
        "stats": {"commentCount": comments if isinstance(comments, int) else 0},
        "statsV2": {"commentCount": str(comments)},
        **extra,
    }


def comment(cid: str, text: str, uid: str, reply_to: str = "0", **extra: Any) -> dict[str, Any]:
    return {
        "cid": cid,
        "text": text,
        "create_time": MAR,
        "digg_count": 3,
        "reply_id": reply_to,
        "user": {"uid": uid, "unique_id": f"user{uid}", "nickname": "Secret Name"},
        "aweme_id": "1",
        **extra,
    }


def test_find_videos_in_page_json() -> None:
    page = {
        "__DEFAULT_SCOPE__": {
            "webapp.video-detail": {"itemInfo": {"itemStruct": video("1", MAR, 4)}},
            "webapp.user-detail": {"userInfo": {"user": {"id": "99", "uniqueId": "mama"}}},
        }
    }
    found = tiktok.find_videos(page)
    assert [v["id"] for v in found] == ["1"]  # the user object is not a video


def test_media_item() -> None:
    item = tiktok.media_item(video("1", MAR, "1200"))
    assert item == {
        "id": "tt:1",
        "caption": "video 1",
        "media_type": "VIDEO",
        "permalink": "https://www.tiktok.com/@mama/video/1",
        "timestamp": datetime.fromtimestamp(MAR, UTC).isoformat(),
        "comments_count": 1200,  # statsV2 string wins over the capped int
    }
    photo = tiktok.media_item(video("2", MAR, 0, imagePost={"images": []}))
    assert (photo["media_type"], photo["permalink"]) == (
        "IMAGE",
        "https://www.tiktok.com/@mama/photo/2",
    )


def test_find_comments_flattens_reply_previews() -> None:
    payload = {
        "comments": [
            comment("11", "wewe ni mjinga", "7", reply_comment=[comment("111", "acha", "8")]),
            {"no": "cid"},
        ],
        "has_more": 1,
    }
    found = tiktok.find_comments(payload)
    assert [c["cid"] for c in found] == ["11", "111"]
    assert tiktok.comment_item(found[1])["parent_id"] == "tt:11"
    assert tiktok.comment_item(found[0])["parent_id"] is None
    assert tiktok.find_comments({"status_code": 0}) == []


# --- scraper ----------------------------------------------------------------------


class FakeTikTok:
    """A tiktok.Site with canned pages: grids load two videos per scroll, comments come in
    the batches a real page would load them in."""

    def __init__(self) -> None:
        self.grids: dict[str, list[dict[str, Any]]] = {
            "https://www.tiktok.com/@mama": [
                video("1", MAR, 3),
                video("2", MAR, 0),
                video("3", JAN, 9),
            ],
            "https://www.tiktok.com/tag/udaku": [video("4", MAR, 1)],
        }
        self.videos = {v["id"]: v for grid in self.grids.values() for v in grid}
        self.comments: dict[str, list[list[dict[str, Any]]]] = {
            "1": [
                [comment("11", "wewe ni mjinga", "7", reply_comment=[comment("111", "acha", "8")])],
                [
                    comment("112", "nyie wajinga", "7", reply_to="11"),
                    comment("111", "acha", "8", reply_to="11"),
                ],
                [comment("12", "safi sana", "9")],
            ],
            "3": [[comment("31", "habari", "5")]],
            "4": [[comment("41", "malaya wewe", "6")]],
        }
        self.opened: list[str] = []
        self.scrolls = 0
        self.current: str | None = None

    def list_videos(self, url: str, enough: Callable[[tiktok.Videos], bool]) -> tiktok.Videos:
        grid = self.grids.get(url, [])
        loaded: list[dict[str, Any]] = []
        while len(loaded) < len(grid) and not enough(loaded):
            self.scrolls += 1
            loaded = grid[: len(loaded) + 2]
        return json.loads(json.dumps(loaded))

    def open_video(self, url: str) -> dict[str, Any] | None:
        self.opened.append(url)
        vid = url.rstrip("/").split("/")[-1]
        self.current = vid if vid in self.videos else None
        return json.loads(json.dumps(self.videos[vid])) if self.current else None

    def read_comments(self, on_comments: tiktok.OnComments) -> None:
        for batch in self.comments.get(self.current or "", []):
            flat = tiktok.find_comments({"comments": json.loads(json.dumps(batch))})
            if not on_comments(flat):
                return


def scraper(db: Session, site: FakeTikTok, **kw: Any) -> tiktok.TikTokScraper:
    return tiktok.TikTokScraper(db, site, NO_WAIT, **kw)


def test_profile_scrape(db: Session) -> None:
    site = FakeTikTok()
    stats = scraper(db, site, category="Udaku").scrape("@mama")

    assert (stats.posts_seen, stats.comments_seen, stats.comments_inserted) == (3, 5, 5)
    account = db.scalars(select(Account)).one()
    assert (account.ig_user_id, account.username, account.category) == (
        "scrape:tt:mama",
        "mama",
        "udaku",
    )
    comments = {c.ig_comment_id: c for c in db.scalars(select(Comment))}
    assert set(comments) == {"tt:11", "tt:111", "tt:112", "tt:12", "tt:31"}
    reply = comments["tt:112"]
    assert reply.parent_ig_comment_id == "tt:11"
    assert reply.author_hash == hash_author("id:tt:7")
    assert reply.commented_at == datetime.fromtimestamp(MAR, UTC)
    for c in comments.values():  # no commenter identity in stored payloads
        assert "Secret" not in json.dumps(c.raw_json) and "user" not in (c.raw_json or {})
    post = db.scalars(select(Media).where(Media.ig_media_id == "tt:1")).one()
    assert (post.caption, post.permalink, post.media_type, post.comments_count) == (
        "video 1",
        "https://www.tiktok.com/@mama/video/1",
        "VIDEO",
        3,
    )
    # the video without comments is stored but its comments are not read
    assert "https://www.tiktok.com/@mama/video/2" in site.opened


def test_rerun_skips_unchanged_videos(db: Session) -> None:
    scraper(db, FakeTikTok()).scrape("mama")
    site = FakeTikTok()
    stats = scraper(db, site).scrape("mama")
    assert (stats.posts_skipped, stats.posts_seen, site.opened) == (3, 0, [])
    assert scraper(db, FakeTikTok(), refresh=True).scrape("mama").posts_seen == 3


def test_since_and_max_posts(db: Session) -> None:
    site = FakeTikTok()
    stats = scraper(db, site, since=date(2026, 2, 1)).scrape("@mama")
    assert stats.posts_seen == 2  # the January video is left out
    stats = scraper(db, FakeTikTok(), max_posts=1, refresh=True).scrape("@mama")
    assert stats.posts_seen == 1


def test_max_comments_per_video(db: Session) -> None:
    stats = scraper(db, FakeTikTok(), max_comments=2).scrape("https://www.tiktok.com/@mama/video/1")
    assert stats.comments_seen == 2


def test_video_url_files_under_its_author(db: Session) -> None:
    site = FakeTikTok()
    stats = scraper(db, site).scrape("https://www.tiktok.com/@whoever/video/4")
    assert (stats.posts_seen, stats.comments_inserted) == (1, 1)
    assert site.opened == ["https://www.tiktok.com/@whoever/video/4"]  # opened once
    assert db.scalars(select(Account.ig_user_id)).one() == "scrape:tt:mama"


def test_hashtag(db: Session) -> None:
    stats = scraper(db, FakeTikTok()).scrape("#udaku")
    assert (stats.posts_seen, stats.comments_inserted) == (1, 1)
    assert db.scalars(select(Account.ig_user_id)).one() == "scrape:tt:#udaku"


def test_missing_targets(db: Session) -> None:
    with pytest.raises(ScrapeError, match="no videos visible"):
        scraper(db, FakeTikTok()).scrape("@nobody")
    with pytest.raises(ScrapeError, match="not found"):
        scraper(db, FakeTikTok()).scrape("https://www.tiktok.com/@mama/video/999")


def test_stores_only_hits(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from collector.config import get_settings

    monkeypatch.setenv("STORE_ONLY_HITS", "true")
    get_settings.cache_clear()
    stats = scraper(db, FakeTikTok()).scrape("@mama")
    assert stats.comments_seen == 5 and stats.comments_kept == 2
    texts = set(db.scalars(select(Comment.text_raw)))
    assert texts == {"wewe ni mjinga", "nyie wajinga"}


# --- browser login ------------------------------------------------------------------


class FakeContext:
    def __init__(self, cookies: list[dict[str, str]]) -> None:
        self._cookies = cookies

    def cookies(self) -> list[dict[str, str]]:
        return self._cookies


def test_login_cookie_is_matched_by_site() -> None:
    instagram_only = FakeContext([{"name": "sessionid", "domain": ".instagram.com"}])
    assert logged_in(instagram_only, "instagram")  # type: ignore[arg-type]
    assert not logged_in(instagram_only, "tiktok")  # type: ignore[arg-type]
    both = FakeContext(
        [
            {"name": "sessionid", "domain": ".instagram.com"},
            {"name": "sessionid", "domain": ".tiktok.com"},
        ]
    )
    assert logged_in(both, "tiktok")  # type: ignore[arg-type]


# --- login is optional --------------------------------------------------------------


@pytest.fixture
def no_browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """run_scrape with a fake, logged-out browser; records the targets scraped."""
    from contextlib import contextmanager

    import collector.scrape.run as run

    scraped: list[str] = []

    @contextmanager
    def open_browser(_profile: Any) -> Any:
        yield object()

    class Scraper:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def scrape(self, target: str) -> Any:
            scraped.append(target)
            from collector.scrape.common import ScrapeStats

            return ScrapeStats()

    monkeypatch.setattr(run, "open_browser", open_browser)
    monkeypatch.setattr(run, "logged_in", lambda context, platform: False)
    monkeypatch.setattr(run, "first_page", lambda context: object())
    monkeypatch.setattr(run, "TikTokSite", lambda page, pacer: object())
    monkeypatch.setattr(tiktok, "TikTokScraper", Scraper)
    return scraped


def test_tiktok_scrapes_without_login(db: Session, no_browser: list[str]) -> None:
    from collector.scrape.run import ScrapeOptions, run_scrape

    run = run_scrape(db, "tiktok", ["@mama"], ScrapeOptions(delay=0))
    assert run.blocked is None and no_browser == ["@mama"]


@pytest.mark.parametrize("platform", ["instagram", "facebook"])
def test_other_platforms_still_need_login(
    db: Session, no_browser: list[str], platform: str
) -> None:
    from collector.scrape.run import ScrapeOptions, run_scrape

    run = run_scrape(db, platform, ["mama"], ScrapeOptions(delay=0))
    assert run.blocked and "log in first" in run.blocked and no_browser == []


# --- refused videos ---------------------------------------------------------------


class RefusingTikTok(FakeTikTok):
    def __init__(self, refuse: set[str]) -> None:
        super().__init__()
        self.refuse = refuse

    def open_video(self, url: str) -> dict[str, Any] | None:
        if url.rstrip("/").split("/")[-1] in self.refuse:
            self.opened.append(url)
            raise ScrapeError("TikTok refused to open it (net::ERR_HTTP_RESPONSE_CODE_FAILURE)")
        return super().open_video(url)


def test_a_refused_video_is_skipped(db: Session) -> None:
    stats = scraper(db, RefusingTikTok({"1"})).scrape("@mama")
    assert (stats.posts_seen, stats.posts_failed) == (2, 1)
    assert "1 failed to open" in str(stats)
    assert db.scalars(select(Media.ig_media_id).order_by(Media.ig_media_id)).all() == [
        "tt:2",
        "tt:3",
    ]


def test_three_refusals_in_a_row_stop_the_run(db: Session) -> None:
    from collector.scrape.common import BlockedError

    with pytest.raises(BlockedError, match="refused 3 videos in a row"):
        scraper(db, RefusingTikTok({"1", "2", "3"})).scrape("@mama")


def test_a_browser_error_skips_only_its_target(
    db: Session, no_browser: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from collector.scrape.common import ScrapeStats
    from collector.scrape.run import ScrapeOptions, run_scrape

    class Flaky:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def scrape(self, target: str) -> ScrapeStats:
            if target == "@broken":
                raise RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE\nCall log")
            return ScrapeStats(posts_seen=1)

    monkeypatch.setattr(tiktok, "TikTokScraper", Flaky)
    run = run_scrape(db, "tiktok", ["@broken", "@mama"], ScrapeOptions(delay=0))
    assert run.blocked is None
    assert [str(r) for r in run.results][0] == (
        "@broken: skipped: Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE"
    )
    assert run.results[1].stats is not None
