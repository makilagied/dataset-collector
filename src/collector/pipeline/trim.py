"""Bring stored comments in line with today's storage rules (see storage_filter).

trim_long_comments: a long comment with a hit word keeps only the excerpt around it; its
features are dropped so the next `process` run redoes them on the excerpt. A long comment
with no hit word is deleted.

prune_non_hits: with STORE_ONLY_HITS (or always=True, the admin UI's button), delete
comments that are not lexicon hits, e.g. after a word was added to the allowlist ("mninga")
or after a scrape that stored everything.
"""

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from collector.config import get_settings
from collector.models import Comment, CommentDuplicate, CommentFeatures, Sample
from collector.pipeline.hitwords import forget_comments
from collector.storage_filter import MAX_TEXT, excerpt, is_hit


@dataclass
class TrimStats:
    trimmed: int = 0
    deleted: int = 0


def trim_long_comments(session: Session, *, dry_run: bool = False) -> TrimStats:
    stats = TrimStats()
    rows = session.scalars(
        select(Comment).where(func.length(Comment.text_raw) > MAX_TEXT).order_by(Comment.id)
    ).all()
    deleted: list[int] = []
    for comment in rows:
        short = excerpt(comment.text_raw)
        if short is None:
            deleted.append(comment.id)
            continue
        stats.trimmed += 1
        if dry_run:
            continue
        forget_comments(session, [comment.id])
        session.execute(delete(CommentFeatures).where(CommentFeatures.comment_id == comment.id))
        raw = dict(comment.raw_json or {})
        raw.update(text=short, original_text_length=len(comment.text_raw))
        comment.text_raw = short
        comment.raw_json = raw
        comment.hit_key = None  # its hit words may have changed: run dedupe again
    stats.deleted = len(deleted)
    if deleted and not dry_run:
        delete_comments(session, deleted)
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return stats


def delete_comments(session: Session, ids: list[int]) -> None:
    """Delete comments with everything hanging off them, taking their words out of the
    hit word counts."""
    forget_comments(session, ids)
    session.execute(delete(Sample).where(Sample.comment_id.in_(ids)))
    session.execute(delete(CommentDuplicate).where(CommentDuplicate.comment_id.in_(ids)))
    session.execute(delete(CommentFeatures).where(CommentFeatures.comment_id.in_(ids)))
    session.execute(delete(Comment).where(Comment.id.in_(ids)))


@dataclass
class PruneStats:
    deleted: int = 0  # stored comments
    occurrences: int = 0  # including the repeats folded into them


def prune_non_hits(session: Session, *, dry_run: bool = False, always: bool = False) -> PruneStats:
    stats = PruneStats()
    if not always and not get_settings().store_only_hits:
        return stats
    gone = [
        (cid, n)
        for cid, text, n in session.execute(
            select(Comment.id, Comment.text_raw, Comment.occurrences).order_by(Comment.id)
        )
        if not is_hit(text or "")
    ]
    stats.deleted = len(gone)
    stats.occurrences = sum(n for _, n in gone)
    if gone and not dry_run:
        delete_comments(session, [cid for cid, _ in gone])
        session.commit()
    return stats
