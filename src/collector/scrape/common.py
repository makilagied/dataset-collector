"""Shared pieces of the browser scrapers: comment filter, storage and pacing.

A scraped profile, page or hashtag is stored as an Account whose ig_user_id starts with
"scrape:" (no token, no consent_ref), so process/sample/export treat its comments like any
other account's. Commenter identities go through upsert_comment and are HMAC-hashed there,
exactly as for the API backfill; raw platform payloads are never stored.
"""

import logging
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from collector import net
from collector.ig.backfill import upsert_comment, upsert_media
from collector.models import Account, Media
from collector.storage_filter import prepare

log = logging.getLogger(__name__)

SCRAPE_PREFIX = "scrape:"
PLATFORMS = ("instagram", "facebook", "tiktok", "x")
_SHORT = {"instagram": "ig", "facebook": "fb", "tiktok": "tt", "x": "x"}
PLATFORM_NAMES = {"ig": "Instagram", "fb": "Facebook", "tt": "TikTok", "x": "X"}
PLATFORM_LABELS = {p: PLATFORM_NAMES[_SHORT[p]] for p in PLATFORMS}
# TikTok shows public videos and comments without an account; logging in is optional.
# X shows logged-out visitors a post but not its replies.
LOGIN_REQUIRED = frozenset({"instagram", "facebook", "x"})


class ScrapeError(Exception):
    """A target that can't be scraped (not found, private, unexpected page)."""


class BlockedError(ScrapeError):
    """The platform wants a login, a checkpoint, or is rate limiting. Stop the whole run."""


class RateLimitedError(BlockedError):
    """The platform is throttling us (HTTP 429): it goes away if we stop for a while."""


def is_scraped(account: Account) -> bool:
    return account.ig_user_id.startswith(SCRAPE_PREFIX)


def ts_iso(unix: Any) -> str | None:
    """Unix seconds -> ISO 8601 (the format upsert_media/upsert_comment parse)."""
    if not isinstance(unix, int | float) or unix <= 0:
        return None
    return datetime.fromtimestamp(unix, UTC).isoformat()


@dataclass
class ScrapeStats:
    posts_seen: int = 0
    posts_skipped: int = 0
    comments_seen: int = 0
    comments_kept: int = 0
    comments_inserted: int = 0
    posts_failed: int = 0  # the site would not open them

    def __str__(self) -> str:
        failed = f", {self.posts_failed} failed to open" if self.posts_failed else ""
        return (
            f"posts: {self.posts_seen} scraped, {self.posts_skipped} skipped (unchanged)"
            f"{failed} | comments: {self.comments_seen} seen, {self.comments_kept} kept, "
            f"{self.comments_inserted} new"
        )


class Pacer:
    """Randomized pause between requests, so a run looks like a person reading, and at most
    `max_per_hour` requests in any rolling hour (0 = no cap)."""

    def __init__(
        self,
        delay: float,
        sleep: Callable[[float], None] = time.sleep,
        max_per_hour: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.delay = delay
        self.sleep = sleep
        self.max_per_hour = max_per_hour
        self.clock = clock
        self._recent: deque[float] = deque()

    def wait(self, factor: float = 1.0) -> None:
        if self.delay > 0:
            self.sleep(random.uniform(0.5, 1.5) * self.delay * factor)
        if not self.max_per_hour:
            return
        now = self.clock()
        while self._recent and self._recent[0] <= now - 3600:
            self._recent.popleft()
        if len(self._recent) >= self.max_per_hour:
            pause = self._recent[0] + 3600 - now
            log.info("hourly request cap (%d) reached, pausing %.0fs", self.max_per_hour, pause)
            net.notify(f"{self.max_per_hour} requests this hour: pausing {pause / 60:.0f} min")
            self.sleep(pause)
            self._recent.popleft()
        self._recent.append(self.clock())


# Pauses after a 429, longer each time it repeats; give up after this much waiting in a row.
COOLDOWN_STEPS_S = (15 * 60, 30 * 60, 60 * 60, 120 * 60)
COOLDOWN_MAX_S = 6 * 3600


class Cooldown:
    """Waits out a platform's rate limit instead of failing: on RateLimitedError pause (15,
    30, 60, then every 120 minutes) and send the same request again, so the scrape carries
    on where it stopped. Shared across jobs (see `cooldown_for`): the next job starts only
    once a pause it inherited is over."""

    def __init__(
        self,
        platform: str,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.platform = platform
        self.sleep = sleep
        self.clock = clock
        self.until = 0.0  # clock() time before which no request should be sent

    def _pause(self, seconds: float, why: str) -> None:
        resume = datetime.now() + timedelta(seconds=seconds)
        log.warning("%s: %s; pausing until %s", self.platform, why, f"{resume:%H:%M}")
        net.notify(f"{self.platform}: {why}. Pausing until {resume:%H:%M}, then carrying on…")
        self.sleep(seconds)

    def call[T](self, fn: Callable[[], T]) -> T:
        left = self.until - self.clock()
        if left > 0:
            self._pause(left, "still rate limited from the last run")
        waited, step = 0.0, 0
        while True:
            try:
                result = fn()
            except RateLimitedError:
                if waited >= COOLDOWN_MAX_S:
                    self.until = self.clock() + COOLDOWN_STEPS_S[-1]
                    raise
                pause = COOLDOWN_STEPS_S[min(step, len(COOLDOWN_STEPS_S) - 1)]
                self.until = self.clock() + pause
                step += 1
                self._pause(pause, f"rate limited (HTTP 429, pause {step})")
                waited += pause
                continue
            if step:
                net.notify(f"{self.platform}: rate limit lifted, carrying on…")
            return result


_cooldowns: dict[str, Cooldown] = {}


def cooldown_for(platform: str) -> Cooldown:
    return _cooldowns.setdefault(platform, Cooldown(platform))


def ensure_account(
    session: Session, platform: str, handle: str, category: str | None = None
) -> Account:
    """Upsert the Account row standing in for a scraped profile / page / hashtag."""
    ig_user_id = f"{SCRAPE_PREFIX}{_SHORT[platform]}:{handle}"
    stmt = insert(Account).values(
        ig_user_id=ig_user_id, username=handle, access_token_enc="", active=True
    )
    session.execute(stmt.on_conflict_do_nothing(index_elements=[Account.ig_user_id]))
    account = session.scalars(select(Account).where(Account.ig_user_id == ig_user_id)).one()
    if category:
        account.category = category.strip().lower()
    session.commit()
    return account


class Store:
    """Writes one target's posts and comments, applying the filter and counting."""

    def __init__(
        self,
        session: Session,
        account: Account,
        stats: ScrapeStats,
        refresh: bool = False,
        progress: Callable[[str], None] | None = None,
        only_hits: bool | None = None,
    ):
        self.session = session
        self.account = account
        self.stats = stats
        self.refresh = refresh
        self.progress = progress
        self.only_hits = only_hits  # None: follow STORE_ONLY_HITS

    def unchanged(self, media_id: str, comments_count: int | None) -> bool:
        """True if this post was fully scraped before and its comment count hasn't moved.

        Without a count (Facebook), any earlier completed scrape counts as unchanged.
        """
        if self.refresh:
            return False
        row = self.session.scalars(select(Media).where(Media.ig_media_id == media_id)).first()
        if row is None or row.last_synced_at is None:
            return False
        return comments_count is None or row.comments_count == comments_count

    def post(self, item: dict[str, Any]) -> Media:
        upsert_media(self.session, self.account.id, item)
        self.stats.posts_seen += 1
        return self.session.scalars(select(Media).where(Media.ig_media_id == item["id"])).one()

    def comment(self, media: Media, item: dict[str, Any]) -> None:
        self.stats.comments_seen += 1
        stored = prepare(item, self.only_hits)
        if stored is None:
            return
        self.stats.comments_kept += 1
        if upsert_comment(self.session, media.id, stored, "scrape"):
            self.stats.comments_inserted += 1

    def finish(self, media: Media) -> None:
        media.last_synced_at = datetime.now(UTC)
        self.session.commit()
        if self.progress:
            self.progress(f"{self.account.username}: {self.stats}")
