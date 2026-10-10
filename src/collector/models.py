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
    username: Mapped[str | None] = mapped_column(String)  # account owner, who consented
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
    source: Mapped[str] = mapped_column(String)  # 'backfill' | 'webhook'
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    pipeline_version: Mapped[str] = mapped_column(String)


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
