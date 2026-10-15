from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    ig_user_id: Mapped[str] = mapped_column(String, unique=True)
    # Account owner, who consented; for scraped sources (ig_user_id 'scrape:...') the
    # profile, page or #hashtag scraped.
    username: Mapped[str | None] = mapped_column(String)
    consent_ref: Mapped[str | None] = mapped_column(String)
    category: Mapped[str | None] = mapped_column(String)  # e.g. udaku, football, media, music
    access_token_enc: Mapped[str] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Media(Base):
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(primary_key=True)
    ig_media_id: Mapped[str] = mapped_column(String, unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    caption: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String)
    permalink: Mapped[str | None] = mapped_column(String)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comments_count: Mapped[int | None] = mapped_column(Integer)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (
        Index("ix_comments_media_id", "media_id"),
        Index("ix_comments_commented_at", "commented_at"),
        # One stored comment per set of hit words (see pipeline.dedupe).
        Index(
            "ux_comments_hit_key", "hit_key", unique=True, postgresql_where=text("hit_key <> ''")
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ig_comment_id: Mapped[str] = mapped_column(String, unique=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id"))
    parent_ig_comment_id: Mapped[str | None] = mapped_column(String)
    author_hash: Mapped[str] = mapped_column(String)
    text_raw: Mapped[str] = mapped_column(Text)
    like_count: Mapped[int | None] = mapped_column(Integer)
    hidden: Mapped[bool | None] = mapped_column(Boolean)
    commented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String)  # 'backfill' | 'webhook' | 'scrape'
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Its distinct hit words, sorted and joined; NULL until deduplicated (DEDUPE_HITS).
    hit_key: Mapped[str | None] = mapped_column(Text)
    # This comment plus every later comment with the same hit words that was not stored.
    occurrences: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))


class CommentDuplicate(Base):
    """A comment that was not stored because a stored comment has the same hit words.
    Remembered so syncing the same post again does not count it twice."""

    __tablename__ = "comment_duplicates"

    ig_comment_id: Mapped[str] = mapped_column(String, primary_key=True)
    comment_id: Mapped[int] = mapped_column(ForeignKey("comments.id"), index=True)
    source: Mapped[str] = mapped_column(String)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CommentFeatures(Base):
    __tablename__ = "comment_features"
    __table_args__ = (
        Index("ix_comment_features_roots", "matched_roots", postgresql_using="gin"),
        # Duplicate detection looks rows up by text; hash it since comments can be long.
        Index("ix_comment_features_text_norm_md5", text("md5(text_norm)")),
    )

    comment_id: Mapped[int] = mapped_column(ForeignKey("comments.id"), primary_key=True)
    text_norm: Mapped[str] = mapped_column(Text)
    # Kept in the DB but left out of sampling: empty | emoji_only | link_only |
    # mention_only | duplicate. NULL means eligible.
    exclude_reason: Mapped[str | None] = mapped_column(String)
    lang: Mapped[str | None] = mapped_column(String)
    lang_conf: Mapped[float | None] = mapped_column(Float)
    matched_roots: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    match_methods: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    match_score: Mapped[float | None] = mapped_column(Float)
    # The highlighted hit words, lowercased and distinct ("m@l@y@", "nitakutomba").
    hit_words: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    # The roots behind them: "wajinga" and "ujinga" are both "jinga".
    hit_roots: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    # The comment's occurrences when its words were counted into hit_words / hit_roots.
    hit_weight: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    pipeline_version: Mapped[str] = mapped_column(String)


class HitWord(Base):
    """Every distinct highlighted spelling of a hit word ("wajinga", "m@l@y@"), with how many
    comments contain it. Kept in step with comment_features.hit_words by pipeline.hitwords.
    """

    __tablename__ = "hit_words"

    word: Mapped[str] = mapped_column(Text, primary_key=True)
    root: Mapped[str] = mapped_column(String)
    count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    comment_id: Mapped[int] = mapped_column(ForeignKey("comments.id"), unique=True)
    batch_name: Mapped[str] = mapped_column(String)
    stratum: Mapped[str] = mapped_column(String)  # 'lexicon' | 'model' | 'random'
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    """Background task started from the admin UI (backfill, process, ...)."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String, default="queued")  # queued|running|done|failed
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncState(Base):
    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)


class HitRoot(Base):
    """Every lexicon root that has been a hit, counting all its spellings as one word: the
    number of comments containing any form of it ("mjinga", "wajinga", "ujinga" -> "jinga").
    Kept in step with comment_features.hit_roots by pipeline.hitwords.
    """

    __tablename__ = "hit_roots"

    root: Mapped[str] = mapped_column(String, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
