"""Backfill an account: media -> comments (with inline replies) -> extra reply pages.

Resumable: each listing saves its next-page cursor in sync_state after every committed page.
A run marker records when the current run started, so media whose comments were finished
earlier in an interrupted run are skipped on resume. A completed run clears its state.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from collector.ig.client import GraphClient
from collector.models import Account, Comment, Media, SyncState
from collector.security import hash_author

log = logging.getLogger(__name__)

MEDIA_FIELDS = "id,caption,media_type,permalink,timestamp,comments_count"
COMMENT_FIELDS = "id,text,timestamp,from,username,like_count,hidden,parent_id"
# Field expansion returns the first page of replies inline, saving one call per comment.
COMMENTS_WITH_REPLIES = f"{COMMENT_FIELDS},replies{{{COMMENT_FIELDS}}}"
PAGE_LIMIT = 50  # documented maximum for comments

# Keys that identify a commenter. Never persisted.
IDENTITY_KEYS = {"from", "username", "user"}


@dataclass
class BackfillStats:
    media_seen: int = 0
    media_skipped: int = 0
    comments_seen: int = 0
    comments_inserted: int = 0


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def author_identifier(item: dict[str, Any]) -> str:
    sender = item.get("from")
    if isinstance(sender, dict) and sender.get("id"):
        return f"id:{sender['id']}"
    for key in ("username", "user"):
        if item.get(key):
            return f"{key}:{item[key]}"
    if isinstance(sender, dict) and sender.get("username"):
        return f"username:{sender['username']}"
    return "unknown"


def sanitize(item: dict[str, Any]) -> dict[str, Any]:
    """Copy of a comment payload with identity fields and nested replies removed."""
    return {k: v for k, v in item.items() if k not in IDENTITY_KEYS and k != "replies"}


# --- sync_state helpers -------------------------------------------------------


def _get_state(session: Session, key: str) -> dict[str, Any] | None:
    row = session.get(SyncState, key)
    return row.value if row else None


def _set_state(session: Session, key: str, value: dict[str, Any]) -> None:
    stmt = insert(SyncState).values(key=key, value=value)
    session.execute(
        stmt.on_conflict_do_update(index_elements=[SyncState.key], set_={"value": value})
    )


def _clear_state(session: Session, prefix: str) -> None:
    session.execute(delete(SyncState).where(SyncState.key.startswith(prefix)))


# --- upserts ------------------------------------------------------------------


def upsert_media(session: Session, account_id: int, item: dict[str, Any]) -> None:
    values = {
        "caption": item.get("caption"),
        "media_type": item.get("media_type"),
        "permalink": item.get("permalink"),
        "posted_at": parse_ts(item.get("timestamp")),
        "comments_count": item.get("comments_count"),
    }
    stmt = insert(Media).values(ig_media_id=str(item["id"]), account_id=account_id, **values)
    session.execute(stmt.on_conflict_do_update(index_elements=[Media.ig_media_id], set_=values))


def upsert_comment(
    session: Session,
    media_id: int,
    item: dict[str, Any],
    source: str,
    parent_id: str | None = None,
    *,
    refresh: bool = True,
) -> bool:
    """Insert one comment. Returns True if a new row was inserted.

    On conflict, `refresh=True` updates text/likes/hidden (backfill has the full record);
    `refresh=False` leaves the existing row alone (webhook payloads are partial).
    """
    mutable = {
        "text_raw": item.get("text") or "",
        "like_count": item.get("like_count"),
        "hidden": item.get("hidden"),
        "raw_json": sanitize(item),
    }
    stmt = insert(Comment).values(
        ig_comment_id=str(item["id"]),
        media_id=media_id,
        parent_ig_comment_id=item.get("parent_id") or parent_id,
        author_hash=hash_author(author_identifier(item)),
        commented_at=parse_ts(item.get("timestamp")),
        source=source,
        **mutable,
    )
    if refresh:
        stmt = stmt.on_conflict_do_update(index_elements=[Comment.ig_comment_id], set_=mutable)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Comment.ig_comment_id])
    # xmax = 0 only for freshly inserted rows (Postgres); do-nothing conflicts return no row.
    inserted = session.execute(stmt.returning(literal_column("xmax = 0"))).scalar_one_or_none()
    return bool(inserted)


# --- backfill -----------------------------------------------------------------


class Backfill:
    def __init__(
        self, session: Session, client: GraphClient, account: Account, since: date | None = None
    ):
        self.session = session
        self.client = client
        self.account = account
        self.since = since
        self.prefix = f"backfill:{account.id}:"
        self.stats = BackfillStats()

    def run(self) -> BackfillStats:
        run_key = self.prefix + "run"
        run = _get_state(self.session, run_key)
        if run is None:
            run = {"started_at": datetime.now(UTC).isoformat()}
            _set_state(self.session, run_key, run)
            self.session.commit()
        else:
            log.info(
                "resuming backfill for account %s started %s", self.account.id, run["started_at"]
            )
        started_at = datetime.fromisoformat(run["started_at"])

        if not run.get("media_done"):
            self._sync_media()
            run["media_done"] = True
            _set_state(self.session, run_key, run)
            self.session.commit()

        media_rows = self.session.scalars(
            select(Media).where(Media.account_id == self.account.id).order_by(Media.id)
        ).all()
        for media in media_rows:
            if self.since and media.posted_at and media.posted_at.date() < self.since:
                continue
            if media.last_synced_at and media.last_synced_at >= started_at:
                self.stats.media_skipped += 1  # finished earlier in this (interrupted) run
                continue
            self._sync_comments(media)

        _clear_state(self.session, self.prefix)
        self.session.commit()
        return self.stats

    def _sync_media(self) -> None:
        key = self.prefix + "media"
        resume = (_get_state(self.session, key) or {}).get("next")
        params = {"fields": MEDIA_FIELDS, "limit": 50}
        for page in self.client.pages("me/media", params, resume_url=resume):
            reached_since = False
            for item in page.data:
                posted = parse_ts(item.get("timestamp"))
                if self.since and posted and posted.date() < self.since:
                    reached_since = True  # media is newest-first; the rest is older
                    continue
                upsert_media(self.session, self.account.id, item)
                self.stats.media_seen += 1
            if page.next_url and not reached_since:
                _set_state(self.session, key, {"next": page.next_url})
            self.session.commit()
            if reached_since:
                break

    def _sync_comments(self, media: Media) -> None:
        key = f"{self.prefix}comments:{media.ig_media_id}"
        resume = (_get_state(self.session, key) or {}).get("next")
        params = {"fields": COMMENTS_WITH_REPLIES, "limit": PAGE_LIMIT}
        for page in self.client.pages(f"{media.ig_media_id}/comments", params, resume_url=resume):
            for item in page.data:
                self._store(media.id, item)
                replies = item.get("replies") or {}
                for reply in replies.get("data") or []:
                    self._store(media.id, reply, parent_id=str(item["id"]))
                if (replies.get("paging") or {}).get("next"):
                    self._sync_more_replies(media.id, str(item["id"]))
            if page.next_url:
                _set_state(self.session, key, {"next": page.next_url})
            self.session.commit()
        media.last_synced_at = datetime.now(UTC)
        self.session.execute(delete(SyncState).where(SyncState.key == key))
        self.session.commit()

    def _sync_more_replies(self, media_id: int, comment_id: str) -> None:
        # The inline page can't be resumed by cursor, so re-list replies via the edge.
        params = {"fields": COMMENT_FIELDS, "limit": PAGE_LIMIT}
        for page in self.client.pages(f"{comment_id}/replies", params):
            for reply in page.data:
                self._store(media_id, reply, parent_id=comment_id)

    def _store(self, media_id: int, item: dict[str, Any], parent_id: str | None = None) -> None:
        self.stats.comments_seen += 1
        if upsert_comment(self.session, media_id, item, "backfill", parent_id):
            self.stats.comments_inserted += 1


def backfill_account(
    session: Session, client: GraphClient, account: Account, since: date | None = None
) -> BackfillStats:
    return Backfill(session, client, account, since).run()
