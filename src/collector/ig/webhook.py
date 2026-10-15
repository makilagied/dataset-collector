"""Webhook verification, signature check and ingestion for `comments` / `live_comments`.

Payload shapes checked October 2026 against
https://developers.facebook.com/docs/instagram-platform/webhooks (and /examples):
- Business Login: entry[] = {id, time, field, value{id, from, text, media, parent_id?}}
- Facebook Login: entry[] = {id, time, changes[{field, value{comment_id, ...}}]}
entry.id is the Instagram professional account ID (accounts.ig_user_id).
"""

import hashlib
import hmac
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from collector.ig.backfill import upsert_comment
from collector.models import Account, Media
from collector.storage_filter import should_store

log = logging.getLogger(__name__)

FIELDS = {"comments", "live_comments"}


def verify_signature(body: bytes, header: str | None, secrets: Iterable[str]) -> bool:
    """Check X-Hub-Signature-256 (`sha256=<hex>`) against any configured app secret."""
    if not header or not header.startswith("sha256="):
        return False
    received = header.removeprefix("sha256=")
    for secret in secrets:
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(received, expected):
            return True
    return False


@dataclass(frozen=True)
class CommentEvent:
    ig_user_id: str
    field: str
    time: datetime | None
    value: dict[str, Any]

    @property
    def comment_id(self) -> str | None:
        cid = self.value.get("id") or self.value.get("comment_id")
        return str(cid) if cid else None

    @property
    def media_id(self) -> str | None:
        media = self.value.get("media") or {}
        mid = media.get("id") if isinstance(media, dict) else None
        return str(mid) if mid else None


def parse_events(payload: dict[str, Any]) -> Iterator[CommentEvent]:
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        # Facebook Login wraps changes in a list; Business Login puts field/value on the entry.
        changes = entry.get("changes")
        if not isinstance(changes, list):
            changes = [entry]
        ts = entry.get("time")
        when = datetime.fromtimestamp(ts, UTC) if isinstance(ts, int | float) else None
        for change in changes:
            if not isinstance(change, dict) or change.get("field") not in FIELDS:
                continue
            value = change.get("value")
            if isinstance(value, dict):
                yield CommentEvent(str(entry.get("id")), change["field"], when, value)


@dataclass
class IngestStats:
    inserted: int = 0
    duplicates: int = 0
    skipped: int = 0
    dropped: int = 0  # not a hit, with STORE_ONLY_HITS
    reasons: list[str] = field(default_factory=list)


def _media_row_id(session: Session, account: Account, ig_media_id: str) -> int:
    """Media may be newer than the last backfill: create a stub row the backfill fills later."""
    stmt = (
        insert(Media)
        .values(ig_media_id=ig_media_id, account_id=account.id)
        .on_conflict_do_nothing(index_elements=[Media.ig_media_id])
    )
    session.execute(stmt)
    media_id: int = session.scalars(select(Media.id).where(Media.ig_media_id == ig_media_id)).one()
    return media_id


def ingest(session: Session, payload: dict[str, Any]) -> IngestStats:
    stats = IngestStats()
    accounts: dict[str, Account | None] = {}
    for event in parse_events(payload):
        if event.ig_user_id not in accounts:
            accounts[event.ig_user_id] = session.scalars(
                select(Account).where(
                    Account.ig_user_id == event.ig_user_id, Account.active.is_(True)
                )
            ).first()
        account = accounts[event.ig_user_id]
        if account is None:  # only collect from accounts that completed OAuth
            stats.skipped += 1
            stats.reasons.append(f"unknown account {event.ig_user_id}")
            continue
        if not event.comment_id or not event.media_id:
            stats.skipped += 1
            stats.reasons.append("missing comment or media id")
            continue

        item = {
            "id": event.comment_id,
            "text": event.value.get("text"),
            "from": event.value.get("from"),
            "parent_id": event.value.get("parent_id"),
            "timestamp": event.time.isoformat() if event.time else None,
            "media": event.value.get("media"),
            "webhook_field": event.field,
        }
        if not should_store(item.get("text") or ""):
            stats.dropped += 1
            continue
        media_id = _media_row_id(session, account, event.media_id)
        if upsert_comment(session, media_id, item, "webhook", refresh=False):
            stats.inserted += 1
        else:
            stats.duplicates += 1
    session.commit()
    return stats
