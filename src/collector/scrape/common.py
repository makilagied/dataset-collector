"""Shared pieces of the browser scrapers: comment filter, storage and pacing.

A scraped profile, page or hashtag is stored as an Account whose ig_user_id starts with
"scrape:" (no token, no consent_ref), so process/sample/export treat its comments like any
other account's. Commenter identities go through upsert_comment and are HMAC-hashed there,
exactly as for the API backfill; raw platform payloads are never stored.
"""

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from collector.ig.backfill import upsert_comment, upsert_media
from collector.models import Account, Media
from collector.storage_filter import prepare

SCRAPE_PREFIX = "scrape:"
PLATFORMS = ("instagram", "facebook")
_SHORT = {"instagram": "ig", "facebook": "fb"}


class ScrapeError(Exception):
    """A target that can't be scraped (not found, private, unexpected page)."""


class BlockedError(ScrapeError):
    """The platform wants a login, a checkpoint, or is rate limiting. Stop the whole run."""


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

    def __str__(self) -> str:
        return (
            f"posts: {self.posts_seen} scraped, {self.posts_skipped} skipped (unchanged) | "
            f"comments: {self.comments_seen} seen, {self.comments_kept} kept, "
            f"{self.comments_inserted} new"
        )


class Pacer:
    """Randomized pause between requests, so a run looks like a person reading."""

    def __init__(self, delay: float, sleep: Callable[[float], None] = time.sleep):
        self.delay = delay
        self.sleep = sleep

    def wait(self, factor: float = 1.0) -> None:
        if self.delay > 0:
            self.sleep(random.uniform(0.5, 1.5) * self.delay * factor)


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
    ):
        self.session = session
        self.account = account
        self.stats = stats
        self.refresh = refresh
        self.progress = progress

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
        stored = prepare(item)
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
